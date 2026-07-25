"""Aciona o OCR de livro escaneado NA HORA, sem esperar o idle do scheduler.

DÁ PARA RODAR DIRETO NO VS CODE (botão Run / F5), sem argumento nenhum: sem
parâmetros ele processa a fila `dados/livros/aguardando_ocr/`. Duas armadilhas do
Run do VS Code já estão desarmadas aqui: (1) o `os.chdir` para a raiz do projeto
ANTES de importar a config — senão o `.env` (lido relativo ao cwd) não carregaria e
o MENTE_OCR_BIN chegaria vazio; (2) mensagem clara se o interpretador selecionado
não for o da env do projeto (o `python` do PATH no Windows costuma ser o atalho
falso da Microsoft Store). Há um `.vscode/launch.json` pronto com o interpretador
certo — escolha "OCR agora" na aba Run and Debug.

Uso pela linha de comando:
    python scripts/ocr_agora.py                      # processa a fila aguardando_ocr/
    python scripts/ocr_agora.py "C:\\livros\\x.pdf"   # um arquivo específico
    python scripts/ocr_agora.py --lote 5             # páginas por bloco de progresso
    python scripts/ocr_agora.py --forcar             # ignora o aviso de servidor no ar

Roda o MESMO código do worker idle (EtlProcessor.ocr_livro) — o que muda é só quem
escolhe a hora. Ao terminar o livro, os capítulos entram na fila de jobs e a
ATOMIZAÇÃO continua sendo trabalho do idle (é ela que precisa do LLM na GPU).

VRAM: se o servidor estiver no ar, ele provavelmente tem LLM/XTTS/Whisper na placa
e o OCR sobe ~3 GB próprios — numa 3080 isso é OOM na certa. Por isso o script
DETECTA o servidor e recusa rodar, a menos que você passe --forcar. Fechar o
servidor (ou deixar o OCR para o idle, que descarrega tudo sozinho) é o caminho
seguro.
"""
import argparse
import asyncio
import os
import shutil
import socket
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
# ANTES de importar a config: o pydantic-settings lê o `.env` relativo ao CWD, e o
# Run do VS Code nem sempre parte da raiz do projeto — sem isto o MENTE_OCR_BIN
# viria vazio e o script diria "OCR não configurado" com tudo configurado.
os.chdir(RAIZ)

try:
    from mente_digital import ocr as ocr_mod  # noqa: E402
    from mente_digital.config import settings  # noqa: E402
    from mente_digital.etl import EtlProcessor  # noqa: E402
    from mente_digital.state import AppContext  # noqa: E402
except ImportError as _exc:   # interpretador errado é o tropeço nº 1 aqui
    print(f"Não consegui importar o projeto ({_exc}).\n"
          "Provavelmente o Python selecionado não é o da env do projeto.\n"
          "No VS Code: Ctrl+Shift+P > 'Python: Select Interpreter' > llama-omni.\n"
          r"No terminal: C:\ProgramData\miniconda3\envs\llama-omni\python.exe "
          "scripts/ocr_agora.py")
    raise SystemExit(2)


def _servidor_no_ar() -> bool:
    """True se algo atende na porta do app (provável servidor com a GPU ocupada)."""
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


def _preparar_alvo(caminho: Path) -> Path:
    """Um PDF de fora entra na fila por CÓPIA — o arquivo original do dono fica
    onde está (o pipeline MOVE o que processa, e mover o original surpreenderia)."""
    fila = Path(settings.dir_livros) / "aguardando_ocr"
    fila.mkdir(parents=True, exist_ok=True)
    if caminho.parent.resolve() == fila.resolve():
        return caminho
    destino = fila / caminho.name
    if not destino.exists():
        shutil.copy2(caminho, destino)
    return destino


async def _rodar(alvos: list, lote: int) -> int:
    ctx = AppContext(settings=settings)
    ctx.interactive_idle.set()      # manual: não há conversa concorrendo neste processo
    etl = EtlProcessor(ctx)
    total = 0
    for pdf in alvos:
        print(f"\n=== {pdf.name}")
        while pdf.exists():
            feitas = await etl.ocr_livro(pdf)
            total += feitas
            if feitas == 0:
                break           # OCR indisponível, erro, ou nada mais a fazer
        print(f"--- {pdf.name}: {'concluído' if not pdf.exists() else 'parou (ver log)'}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", nargs="?", help="PDF específico (default: a fila aguardando_ocr/)")
    ap.add_argument("--lote", type=int, default=5,
                    help="páginas por bloco (progresso mais frequente; default 5)")
    ap.add_argument("--forcar", action="store_true",
                    help="roda mesmo com o servidor no ar (risco de OOM de VRAM)")
    args = ap.parse_args()

    ok, motivo = ocr_mod.disponibilidade(
        settings.ocr_bin, settings.caminho_modelo_ocr, settings.caminho_mmproj_ocr)
    if not ok:
        print(f"OCR não está pronto: {motivo}")
        return 2

    if _servidor_no_ar() and not args.forcar:
        print("O servidor parece estar NO AR (porta ocupada) — ele mantém LLM/voz na "
              "VRAM e o OCR precisa de ~3 GB livres.\nFeche o servidor e rode de novo, "
              "deixe o idle cuidar disso, ou use --forcar se souber o que está fazendo.")
        return 3

    if args.pdf:
        origem = Path(args.pdf)
        if not origem.is_file():
            print(f"Arquivo não encontrado: {origem}")
            return 2
        alvos = [_preparar_alvo(origem)]
    else:
        fila = Path(settings.dir_livros) / "aguardando_ocr"
        alvos = sorted(p for p in fila.glob("*.pdf")) if fila.is_dir() else []
        if not alvos:
            print(f"Nada em {fila}. Largue o PDF escaneado lá (ou passe o caminho).")
            return 0

    # O lote do idle vira o passo de PROGRESSO aqui: o laço repete até o livro acabar.
    settings.ocr_paginas_por_ciclo = max(1, args.lote)
    total = asyncio.run(_rodar(alvos, args.lote))
    print(f"\n{total} página(s) transcrita(s). Os capítulos prontos estão na fila de "
          "jobs — a atomização acontece no próximo idle do servidor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
