"""
Calibra RAG_SCORE_CONFIDENT para a escala de distância do embedding ATUAL (Etapa 3c).

Ao trocar o modelo de embedding, a escala de distância muda (o e5 comprime tudo numa
banda alta e estreita), então o threshold do gate herdado vira lixo. Este probe roda a
BUSCA DE PRODUÇÃO (VectorStore.search, mesmo caminho da pergunta real) sobre:

  - perguntas REAIS (do histórico) -> devem casar átomos (melhor_dist BAIXO);
  - perguntas CONTROLE (fora do vault) -> ruído (melhor_dist ALTO).

e escolhe o threshold que melhor SEPARA os dois (máximo de Youden's J = TPR - FPR):
abaixo dele, "confiante" (Cache Hit sem aterramento léxico); acima, escala.

    python eval/calibrar_gate.py                 # depois do reindex
    python eval/calibrar_gate.py --n-reais 60
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np  # noqa: E402

from mente_digital import textutils  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.rag import EmbeddingProvider, VectorStore  # noqa: E402
from mente_digital.telemetry import db  # noqa: E402

# Perguntas claramente FORA das temáticas do vault (YOLO/TensorRT/RAG/mineração/
# DuckDB/bike/culinária/negócios/Gemini) — servem de piso de ruído.
CONTROLE = [
    "qual a capital da Austrália",
    "quem escreveu a Odisseia",
    "qual a fórmula química do sal de cozinha",
    "quantos corações tem um polvo",
    "qual a distância da Terra ao Sol",
    "quem foi o primeiro homem a pisar na Lua",
    "qual o maior deserto do mundo",
    "como se diz bom dia em alemão",
    "qual a velocidade da luz no vácuo",
    "quando terminou a Segunda Guerra Mundial",
    "qual o rio mais longo do mundo",
    "quem pintou o teto da Capela Sistina",
]


def perguntas_reais(n: int) -> List[str]:
    hist = db.get_history(400) or []
    vistas, out = set(), []
    for t in hist:
        q = (t.get("q") or "").strip()
        chave = textutils.normaliza(q)
        if len(q) < 12 or chave in vistas:
            continue
        vistas.add(chave)
        out.append(q)
        if len(out) >= n:
            break
    return out


async def melhores(vs: VectorStore, perguntas: List[str]) -> List[Tuple[str, Optional[float]]]:
    out = []
    for q in perguntas:
        termos = textutils.limpar_query(q) or q
        local = await vs.search(termos, texto_busca=q)
        out.append((q, local.melhor_dist))
    return out


def _pcts(vals: List[float]) -> str:
    if not vals:
        return "(vazio)"
    a = np.array(vals)
    return (f"n={len(vals)} min={a.min():.3f} p25={np.percentile(a,25):.3f} "
            f"p50={np.percentile(a,50):.3f} p75={np.percentile(a,75):.3f} "
            f"p90={np.percentile(a,90):.3f} max={a.max():.3f}")


def melhor_threshold(reais: List[float], controle: List[float]) -> Tuple[float, float, float, float]:
    """Threshold que maximiza Youden's J (TPR - FPR). Real=positivo (deve ficar <= t),
    controle=negativo. Devolve (t, tpr, fpr, j)."""
    cand = sorted(set(reais + controle))
    melhor = (0.0, 0.0, 0.0, -1.0)
    R, C = np.array(reais), np.array(controle)
    for t in cand:
        tpr = float((R <= t).mean()) if len(R) else 0.0
        fpr = float((C <= t).mean()) if len(C) else 0.0
        j = tpr - fpr
        if j > melhor[3]:
            melhor = (t, tpr, fpr, j)
    return melhor


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n-reais", type=int, default=50)
    a = p.parse_args()

    print(f"modelo = {settings.embedding_model}  prefixos q={settings.embedding_query_prefix!r} "
          f"p={settings.embedding_passage_prefix!r}")
    emb = EmbeddingProvider()
    await asyncio.to_thread(emb.load)
    vs = VectorStore(emb)
    await vs.open()
    if not vs.ready:
        print("ERRO: VectorStore não abriu — reindexou?")
        return

    reais_q = perguntas_reais(a.n_reais)
    print(f"\nRodando {len(reais_q)} perguntas reais + {len(CONTROLE)} controle pela busca...")
    reais = [(q, d) for q, d in await melhores(vs, reais_q) if d is not None]
    controle = await melhores(vs, CONTROLE)

    rd = [d for _, d in reais]
    cd = [d for _, d in controle if d is not None]

    print("\n=== melhor_dist (distância de cosseno; MENOR = mais perto) ===")
    print(f"REAIS    : {_pcts(rd)}")
    print(f"CONTROLE : {_pcts(cd)}")
    print("\n--- controle (piso de ruído, ordenado) ---")
    for q, d in sorted(controle, key=lambda x: (x[1] is None, x[1])):
        print(f"  {d if d is None else f'{d:.3f}'}  {q}")

    t, tpr, fpr, j = melhor_threshold(rd, cd)
    print("\n=== SUGESTÃO ===")
    print(f"RAG_SCORE_CONFIDENT ~= {t:.3f}  (J={j:.2f}: cobre {tpr*100:.0f}% das reais, "
          f"rejeita {(1-fpr)*100:.0f}% do controle)")
    print("Ajuste fino depois com o log [LOCAL] melhor_dist em uso real (ver CALIBRACAO.md).")


if __name__ == "__main__":
    asyncio.run(main())
