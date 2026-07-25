"""Baixa os modelos que o repo NÃO versiona (painel 2026-07-24, kit de onboarding).

Antes o setup exigia caçar 3 arquivos em páginas do HuggingFace na mão — o passo
mais frágil do onboarding. Aqui: um comando, com retomada (o hf_hub reusa o que já
baixou) e com a armadilha conhecida do backend Xet desarmada (downloads estagnavam
nesta máquina; HF_HUB_DISABLE_XET=1 resolve — precisa estar no ambiente ANTES do
import de huggingface_hub).

Uso:
    python scripts/baixar_modelos.py           # LLM (GGUF ~4,7 GB) + voz Piper
    python scripts/baixar_modelos.py --xtts    # + XTTS-v2 (voz clonada, ~2 GB)

Whisper e o embedding e5 NÃO estão aqui de propósito: baixam sozinhos no 1º uso
(faster-whisper/sentence-transformers gerenciam o próprio cache).
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # antes do import do hub (ver docstring)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mente_digital.config import DIR_MODELOS  # noqa: E402

LLM_REPO, LLM_ARQ = "Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf"
PIPER_REPO = "rhasspy/piper-voices"
PIPER_ARQS = (
    "pt/pt_BR/cadu/medium/pt_BR-cadu-medium.onnx",
    "pt/pt_BR/cadu/medium/pt_BR-cadu-medium.onnx.json",
)
XTTS_REPO = "coqui/XTTS-v2"
# OCR de livro escaneado (Fase 3): GGUF quantizado + o projetor de visão (mmproj é
# OBRIGATÓRIO — sem ele o modelo não vê a imagem). Q4_K_M por caber com folga na
# 3080 quando o LLM está descarregado. O binário vem do llama.cpp (ver config.py).
OCR_REPO = "sahilchachra/Unlimited-OCR-GGUF"
OCR_ARQS = ("Unlimited-OCR-Q4_K_M.gguf", "mmproj-Unlimited-OCR-F16.gguf")


def _baixar_para(repo: str, arquivo: str, destino: Path) -> bool:
    """Baixa UM arquivo. Devolve False em falha, sem levantar: um arquivo que falha
    NÃO pode abortar os outros — era o defeito visto ao vivo (2026-07-25), em que o
    GGUF de 1,9 GB baixou, o passo seguinte morreu e o mmproj OBRIGATÓRIO ficou
    faltando, com o usuário achando que estava tudo certo."""
    from huggingface_hub import hf_hub_download

    if destino.exists():
        print(f"[ok] já existe: {destino.name}")
        return True
    print(f"[..] baixando {arquivo} de {repo} …")
    try:
        baixado = hf_hub_download(repo_id=repo, filename=arquivo)
        destino.parent.mkdir(parents=True, exist_ok=True)
        # copy2 (não move): o cache do hub segue válido p/ retomadas/reinstalações.
        shutil.copy2(baixado, destino)
    except Exception as exc:
        print(f"[ERRO] {arquivo}: {type(exc).__name__}: {exc}")
        return False
    print(f"[ok] {destino}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xtts", action="store_true",
                        help="baixa também o XTTS-v2 (voz clonada; opt-in como no .env)")
    parser.add_argument("--ocr", action="store_true",
                        help="baixa o OCR de livro escaneado (GGUF Q4_K_M + mmproj, ~2,7 GB)")
    args = parser.parse_args()

    _baixar_para(LLM_REPO, LLM_ARQ, DIR_MODELOS / LLM_ARQ)
    for arq in PIPER_ARQS:
        _baixar_para(PIPER_REPO, arq, DIR_MODELOS / Path(arq).name)

    if args.ocr:
        # Os DOIS são obrigatórios: sem o mmproj (o projetor de visão) o modelo não
        # enxerga a página — por isso o resultado é conferido explicitamente.
        okes = [_baixar_para(OCR_REPO, arq, DIR_MODELOS / arq) for arq in OCR_ARQS]
        if all(okes):
            print("\nOCR baixado (GGUF + mmproj). Falta o binário: pegue um llama.cpp "
                  ">= 2026-03-25 (release pronta serve) e aponte MENTE_OCR_BIN para "
                  "o llama-mtmd-cli.")
        else:
            faltando = [a for a, ok in zip(OCR_ARQS, okes) if not ok]
            print(f"\n[ATENÇÃO] OCR INCOMPLETO — faltou: {', '.join(faltando)}. "
                  "Rode de novo (o que já baixou é reusado do cache).")

    if args.xtts:
        from huggingface_hub import snapshot_download

        destino = DIR_MODELOS / "xtts_v2"
        print(f"[..] baixando snapshot {XTTS_REPO} para {destino} …")
        snapshot_download(repo_id=XTTS_REPO, local_dir=str(destino))
        print(f"[ok] {destino}")

    print("\nPronto. Whisper e embeddings baixam sozinhos no primeiro uso.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
