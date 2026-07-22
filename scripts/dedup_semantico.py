"""
Dedup semântico RETROATIVO da base legada (painel 2026-07, ETL/Dados).

O dedup de escrita (`agent._ja_no_banco`) só protege átomos salvos DEPOIS que ele
existiu; os ~12,7k átomos do import passaram por dedup POR LOTE, e os clones
cross-run escaparam. O `limpar_vault.py` só remove clone EXATO (md5) — os
near-dups semânticos ficam, e hoje são remediados no SINTOMA a cada query
(rag_dedup_near_jaccard). Este script ataca a CAUSA, offline:

- Usa os embeddings JÁ armazenados no Chroma (zero LLM, zero re-embed, zero rede):
  all-pairs por BLOCOS numpy (~40 MB), não 12,8k queries HNSW.
- Um chunk tem GÊMEO se existe chunk de OUTRA fonte com distância de cosseno
  < limiar (default: settings.dedup_dist_max, o mesmo 0.08 conservador da escrita).
- Uma FONTE só é candidata se TODOS os chunks dela têm gêmeo (nota multi-chunk com
  conteúdo próprio nunca é movida por ter UM parágrafo parecido).
- No cluster (union-find fonte↔fonte dos gêmeos), a MAIS ANTIGA fica (colhido_ts
  mínimo dos chunks; fallback mtime do arquivo) — e a mais antiga NUNCA é movida,
  candidata ou não.
- NÃO deleta: MOVE para `_lixo_purgado/dedup_semantico/` (FORA do vault — a lição
  medida do limpar_vault: dentro, o sync re-indexa o lixo). A purga de órfãos do
  próximo `sync()` limpa o índice sozinha.

USO (env llama-omni, com o SERVIDOR PARADO — o sqlite do Chroma é single-writer):
    python scripts/dedup_semantico.py               # DRY-RUN: só mede e reporta
    python scripts/dedup_semantico.py --executar    # move os arquivos de verdade
    python scripts/dedup_semantico.py --limiar 0.05 # limiar mais rígido
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from mente_digital.config import settings  # noqa: E402

LIXO = os.path.join(os.path.dirname(settings.caminho_obsidian), "_lixo_purgado", "dedup_semantico")
_BLOCO = 1024  # linhas por bloco do all-pairs (1024 x N floats ~= 50 MB em N=13k)


def _carregar():
    """Lê embeddings+metadatas direto do chromadb (sem langchain — só leitura)."""
    import chromadb

    client = chromadb.PersistentClient(path=settings.diretorio_banco_vetorial)
    cols = client.list_collections()
    if not cols:
        raise SystemExit("Nenhuma coleção no banco vetorial — nada a deduplicar.")
    nome = cols[0].name if hasattr(cols[0], "name") else str(cols[0])
    col = client.get_collection(nome)
    dump = col.get(include=["embeddings", "metadatas"])
    embs = np.asarray(dump["embeddings"], dtype="float32")
    metas = dump["metadatas"] or []
    return embs, metas


def _fonte_e_idade(metas) -> tuple[list, dict]:
    """Por chunk: a fonte; por fonte: a idade (colhido_ts mínimo; fallback mtime)."""
    fontes_chunk: list = []
    idade: dict = {}
    for md in metas:
        src = str((md or {}).get("source") or "")
        fontes_chunk.append(src)
        if not src:
            continue
        ts = (md or {}).get("colhido_ts")
        if ts is not None:
            try:
                ts = float(ts)
                idade[src] = min(idade.get(src, ts), ts)
            except (TypeError, ValueError):
                pass
    for src in set(fontes_chunk):
        if src and src not in idade:
            try:
                idade[src] = os.path.getmtime(src)
            except OSError:
                idade[src] = float("inf")   # sem arquivo nem carimbo: nunca "a mais antiga"
    return fontes_chunk, idade


def _gemeos_por_chunk(embs: np.ndarray, fontes_chunk: list, limiar: float):
    """Para cada chunk, o melhor gêmeo CROSS-FONTE (dist cosseno < limiar) — em
    blocos numpy. Devolve: twin[i] = índice do gêmeo (ou -1) e a distância."""
    n = embs.shape[0]
    normas = np.linalg.norm(embs, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    A = embs / normas
    fontes_arr = np.array(fontes_chunk)
    twin = np.full(n, -1, dtype="int64")
    twin_dist = np.full(n, np.inf, dtype="float32")
    for ini in range(0, n, _BLOCO):
        fim = min(ini + _BLOCO, n)
        sims = A[ini:fim] @ A.T                      # (bloco, n)
        # invalida pares da MESMA fonte (e o próprio chunk): gêmeo é de outra nota
        mesmas = fontes_arr[ini:fim, None] == fontes_arr[None, :]
        sims[mesmas] = -1.0
        melhor = np.argmax(sims, axis=1)
        dist = 1.0 - sims[np.arange(fim - ini), melhor]
        ok = dist < limiar
        twin[ini:fim][ok] = melhor[ok]
        twin_dist[ini:fim][ok] = dist[ok]
    return twin, twin_dist


class _UF:
    def __init__(self):
        self.pai: dict = {}

    def find(self, x):
        self.pai.setdefault(x, x)
        while self.pai[x] != x:
            self.pai[x] = self.pai[self.pai[x]]
            x = self.pai[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.pai[ra] = rb


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--executar", action="store_true", help="move de verdade (default: dry-run)")
    ap.add_argument("--limiar", type=float, default=settings.dedup_dist_max)
    args = ap.parse_args()

    embs, metas = _carregar()
    fontes_chunk, idade = _fonte_e_idade(metas)
    print(f"Base: {embs.shape[0]} chunks / {len(set(f for f in fontes_chunk if f))} fontes "
          f"(limiar dist < {args.limiar})")

    twin, twin_dist = _gemeos_por_chunk(embs, fontes_chunk, args.limiar)

    # Fonte candidata = TODOS os chunks dela têm gêmeo cross-fonte.
    chunks_da_fonte: dict = defaultdict(list)
    for i, src in enumerate(fontes_chunk):
        if src:
            chunks_da_fonte[src].append(i)
    candidatas = {
        src for src, idxs in chunks_da_fonte.items()
        if all(twin[i] >= 0 for i in idxs)
    }
    print(f"Candidatas (todos os chunks com gêmeo): {len(candidatas)}")

    # Clusters: candidata unida à fonte de cada gêmeo dela.
    uf = _UF()
    for src in candidatas:
        for i in chunks_da_fonte[src]:
            uf.union(src, fontes_chunk[twin[i]])
    clusters: dict = defaultdict(set)
    for src in candidatas:
        clusters[uf.find(src)].add(src)
        clusters[uf.find(src)].add(uf.find(src))

    mover: list = []
    for raiz, membros in clusters.items():
        mais_antiga = min(membros, key=lambda s: idade.get(s, float("inf")))
        for src in sorted(membros):
            if src != mais_antiga and src in candidatas:
                mover.append((src, mais_antiga))

    print(f"Clusters de duplicata: {len(clusters)} | fontes a MOVER: {len(mover)}")
    for src, fica in mover[:15]:
        print(f"  - {os.path.basename(src)}  (fica: {os.path.basename(fica)})")
    if len(mover) > 15:
        print(f"  ... e mais {len(mover) - 15}")

    if not args.executar:
        print("\nDRY-RUN: nada foi movido. Rode com --executar para aplicar "
              "(com o servidor PARADO; o próximo sync purga o índice sozinho).")
        return

    os.makedirs(LIXO, exist_ok=True)
    movidos = 0
    for src, _fica in mover:
        try:
            if os.path.exists(src):
                shutil.move(src, os.path.join(LIXO, os.path.basename(src)))
                movidos += 1
        except OSError as exc:
            print(f"  [erro] não movi {src}: {exc}")
    print(f"\n{movidos} arquivo(s) movidos para {LIXO}. Rode o servidor: o sync purga o índice.")


if __name__ == "__main__":
    main()
