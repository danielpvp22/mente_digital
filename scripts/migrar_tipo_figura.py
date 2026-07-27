"""Migração v3 -> v4: grava `tipo` (figura/texto) SEM recomputar embedding.

O mecanismo oficial de migração de metadado do projeto é bumpar `_META_VERSAO` e
deixar o `sync()` re-passar o vault. Aqui isso custaria ~45 min de CPU recomputando
33 mil embeddings — para um campo que é função PURA do caminho do arquivo
(`rag._e_figura`), idêntico ao que a re-passada produziria.

Então este script faz o atalho seguro: lê os metadados existentes, acrescenta
`tipo` e carimba `meta_v=4`, e escreve de volta com `collection.update` (que só
toca metadado; o vetor fica onde está). O carimbo é essencial — sem ele o `sync()`
seguinte veria meta_v<4 e re-passaria tudo mesmo assim.

Só vale porque v3->v4 acrescenta UM campo derivado do path. Migração que mude o
texto indexado ou o embedding TEM de passar pelo reindex de verdade.

    python scripts/migrar_tipo_figura.py --dry-run
    python scripts/migrar_tipo_figura.py
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.chdir(RAIZ)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

from mente_digital import rag  # noqa: E402
from mente_digital.config import settings  # noqa: E402

LOTE = 2000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="so relata")
    args = ap.parse_args()

    import chromadb

    cli = chromadb.PersistentClient(path=settings.diretorio_banco_vetorial)
    cols = cli.list_collections()
    if not cols:
        print("nenhuma coleção no banco vetorial — rode o reindexar.py antes.")
        return 1
    nome = cols[0].name if hasattr(cols[0], "name") else str(cols[0])
    col = cli.get_collection(nome)
    print(f"coleção '{nome}': {col.count()} chunks")

    dump = rag.dump_paginado(col, ["metadatas"])
    ids = dump.get("ids") or []
    metas = dump.get("metadatas") or []
    print(f"lidos {len(ids)} ids / {len(metas)} metadados")

    ids_mudar, metas_novos = [], []
    contagem: Counter = Counter()
    for cid, md in zip(ids, metas):
        md = dict(md or {})
        tipo = "figura" if rag._e_figura(str(md.get("source", ""))) else "texto"
        contagem[tipo] += 1
        if md.get("tipo") == tipo and int(md.get("meta_v", 0) or 0) >= rag._META_VERSAO:
            continue                     # já migrado: idempotente
        md["tipo"] = tipo
        md["meta_v"] = rag._META_VERSAO
        ids_mudar.append(cid)
        metas_novos.append(md)

    print(f"classificação: figura={contagem['figura']} texto={contagem['texto']}")
    print(f"a atualizar: {len(ids_mudar)} chunk(s)")
    if args.dry_run or not ids_mudar:
        return 0

    for i in range(0, len(ids_mudar), LOTE):
        col.update(ids=ids_mudar[i:i + LOTE], metadatas=metas_novos[i:i + LOTE])
        print(f"  {min(i + LOTE, len(ids_mudar))}/{len(ids_mudar)}")

    confere = rag.dump_paginado(col, ["metadatas"])
    depois = Counter(str((m or {}).get("tipo")) for m in confere.get("metadatas") or [])
    print(f"\nconferência pós-migração: {dict(depois)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
