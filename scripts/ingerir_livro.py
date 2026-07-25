"""Extrai um PDF DIGITAL em jobs de capítulo para a ingestão em idle (Fase 1).

Uso:
    python scripts/ingerir_livro.py caminho/do/livro.pdf [--titulo "Nome do Livro"]
                                    [--paginas-por-bloco 12]

O script SÓ extrai e enfileira (segundos, sem GPU): o texto vira jobs JSON em
dados/ingestao/pendentes/ e o SCHEDULER os atomiza no idle, um capítulo por
passada, sem nunca competir com uma conversa aberta. Com TOC no PDF, os capítulos
são reais; sem TOC, janelas de N páginas (proveniência de página segue exata).

Livro ESCANEADO (páginas sem texto selecionável) é detectado e recusado — ele é o
caso da Fase 3 (worker OCR Unlimited-OCR GGUF em venv própria), que vai gerar
ESTES MESMOS jobs; o resto do pipeline não muda.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mente_digital import livro as livro_mod  # noqa: E402
from mente_digital.config import settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", help="caminho do PDF digital")
    ap.add_argument("--titulo", help="nome do livro (default: nome do arquivo)")
    ap.add_argument("--paginas-por-bloco", type=int, default=12,
                    help="tamanho do bloco quando o PDF não tem sumário (TOC)")
    args = ap.parse_args()

    try:
        import fitz  # PyMuPDF — import tardio: só este script precisa dele
    except ImportError:
        print("PyMuPDF não instalado. Rode: pip install pymupdf")
        return 2

    caminho = Path(args.pdf)
    if not caminho.is_file():
        print(f"Arquivo não encontrado: {caminho}")
        return 2

    doc = fitz.open(caminho)
    paginas = [p.get_text("text") for p in doc]
    toc = doc.get_toc() or []
    doc.close()

    if livro_mod.parece_escaneado([len(t) for t in paginas]):
        print("Este PDF parece ESCANEADO (sem texto selecionável). A Fase 1 cobre só "
              "PDF digital — o worker OCR (Fase 3) vai cuidar deste. Nada foi enfileirado.")
        return 3

    titulo = args.titulo or caminho.stem
    jobs = livro_mod.montar_jobs(titulo, paginas, toc, args.paginas_por_bloco)
    if not jobs:
        print("Nenhum texto extraível encontrado no PDF.")
        return 3

    destino = Path(settings.dir_ingestao) / "pendentes"
    destino.mkdir(parents=True, exist_ok=True)
    base = livro_mod.slug(titulo)
    for j in jobs:
        nome = f"{base}__{j['capitulo']:03d}.json"
        (destino / nome).write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")

    modo = "TOC real" if len([1 for n, _, _ in toc if n == 1]) >= 2 else \
        f"janelas de {args.paginas_por_bloco} páginas (PDF sem TOC)"
    print(f"'{titulo}': {len(jobs)} capítulo(s) enfileirados ({modo}) em {destino}.")
    print("O scheduler atomiza no idle (servidor rodando, sem conversa aberta).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
