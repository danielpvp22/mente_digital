"""Atomiza a fila de capítulos SEM subir o servidor inteiro.

Por que existe (pergunta do dono, 2026-07-25): rodar `main.py` para uma ingestão
em lote carrega Whisper e o motor de voz junto — ~3 GB de VRAM que a atomização
não usa, mais o WebSocket, o scheduler e o dance de cuDNN entre ctranslate2 e
torch. Aqui sobem só as DUAS peças necessárias: o LLM (que atomiza) e os
embeddings/VectorStore (que deduplicam e indexam).

O que se perde: nada da ingestão. O que NÃO se ganha: isto não é mais rápido por
si só — o gargalo é o decode, não a memória livre. O ganho é VRAM sobrando (para
um n_ctx maior, por exemplo) e um processo que faz uma coisa só.

Uso (o servidor deve estar FECHADO — a GPU é uma só):
    python scripts/atomizar_agora.py            # drena a fila até acabar
    python scripts/atomizar_agora.py --caps 5   # só N capítulos e sai
"""
import argparse
import asyncio
import os
import socket
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)   # o pydantic lê o .env relativo ao CWD (ver ocr_agora.py)

from mente_digital.config import settings  # noqa: E402
from mente_digital.etl import EtlProcessor  # noqa: E402
from mente_digital.llm import LlamaManager, preparar_offline  # noqa: E402
from mente_digital.rag import EmbeddingProvider, VectorStore  # noqa: E402
from mente_digital.state import AppContext  # noqa: E402
from mente_digital.telemetry import telemetry  # noqa: E402


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


async def _rodar(max_caps: int) -> int:
    ctx = AppContext(settings=settings)
    ctx.interactive_idle.set()          # processo dedicado: não há conversa aqui
    # Servidor FECHADO (o guard acima garante): VRAM livre para o modelo grande.
    # `preparar_offline` alinha as flags de <think> ao modelo (ver llm.py).
    ctx.llama = LlamaManager(preparar_offline(settings.caminho_modelo_atomizacao))
    ctx.vectorstore = VectorStore(EmbeddingProvider())
    telemetry.track("ATOMIZAR", "Carregando LLM e embeddings (sem voz, sem WS)…")
    await ctx.llama.load()
    await asyncio.to_thread(ctx.vectorstore.load_embeddings)
    await ctx.vectorstore.open()
    etl = EtlProcessor(ctx)
    feitos, t0 = 0, time.perf_counter()
    try:
        while True:
            n = await etl.ingestao_livros()
            if not n:
                break
            feitos += n
            restam = len(list((Path(settings.dir_ingestao) / "pendentes").glob("*.json")))
            print(f"[{time.perf_counter()-t0:7.1f}s] {feitos} capítulo(s) prontos | {restam} na fila",
                  flush=True)
            if max_caps and feitos >= max_caps:
                break
    finally:
        await ctx.llama.unload()
    return feitos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--caps", type=int, default=0, help="para após N capítulos (0 = drena tudo)")
    ap.add_argument("--forcar", action="store_true", help="roda mesmo com o servidor no ar")
    args = ap.parse_args()

    pend = Path(settings.dir_ingestao) / "pendentes"
    if not pend.is_dir() or not any(pend.glob("*.json")):
        print(f"Fila vazia em {pend}.")
        return 0
    if _servidor_no_ar() and not args.forcar:
        print("O servidor está NO AR e ele também atomiza no idle — dois processos "
              "disputariam a mesma GPU.\nFeche o servidor (ou use --forcar).")
        return 3
    n = asyncio.run(_rodar(args.caps))
    print(f"\n{n} capítulo(s) atomizados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
