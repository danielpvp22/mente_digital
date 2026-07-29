"""
Reindex COMPLETO do vault com o embedding configurado no .env (Etapa 3c).

Standalone: carrega só os embeddings + Chroma (sem LLM, sem servidor web). Use depois
de trocar MENTE_EMBEDDING_MODEL e APAGAR banco_vetorial_cerebro/, para reconstruir o
índice do zero com o novo modelo. Como o banco fica vazio, VectorStore.sync() reprocessa
TODAS as notas do vault (glob **/*.md).

    python scripts/reindexar.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from mente_digital.config import settings  # noqa: E402
from mente_digital.rag import (  # noqa: E402
    EmbeddingProvider, VectorStore, preparar_embedding_offline,
)


async def main() -> None:
    # GPU aqui é de graça: este script não sobe o LLM e roda com o servidor fechado,
    # então não há com quem disputar VRAM (medido: 8,3GB livres). Ao vivo o e5 na GPU
    # deixaria 474 MiB de folga no pico — por isso o device é OUTRO, não o mesmo.
    # Medido (400 notas tocadas): 98s -> 77s. É menos que os 14,7x do embedding
    # porque a passada incremental é dominada por varrer as ~24k notas, não por
    # embedar; num rebuild completo, aí sim os ~25k chunks mandam.
    preparar_embedding_offline()

    print(f"modelo   = {settings.embedding_model}")
    print(f"prefixos = query='{settings.embedding_query_prefix}' "
          f"passage='{settings.embedding_passage_prefix}'")
    print(f"device   = {settings.embedding_device}")
    print(f"vault    = {settings.caminho_obsidian}")
    print(f"banco    = {settings.diretorio_banco_vetorial}\n")

    os.makedirs(settings.diretorio_banco_vetorial, exist_ok=True)

    emb = EmbeddingProvider()
    await asyncio.to_thread(emb.load)
    if emb.instance is None:
        print("ERRO: embeddings não carregaram.")
        return

    vs = VectorStore(emb)
    await vs.open()
    if not vs.ready:
        print("ERRO: Chroma não abriu.")
        return

    t0 = time.perf_counter()
    await vs.sync()   # banco vazio -> reindexa TODO o vault
    dt = time.perf_counter() - t0

    try:
        n = vs._store._collection.count()
    except Exception:
        n = "?"
    print(f"\nReindex OK em {dt:.0f}s — {n} chunks indexados com '{settings.embedding_model}'.")


if __name__ == "__main__":
    asyncio.run(main())
