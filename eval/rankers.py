"""
Réguas de recuperação, para comparar contra o cosseno puro (que o teste do HyDE
desqualificou: melhora a distância e piora a resposta — mede "parece", não "responde").

Todas são RÁPIDAS (sem LLM no caminho quente), pré-computadas uma vez e consultadas em
O(termos × docs-com-o-termo). É o requisito do usuário: melhor conteúdo E impressão de
tempo real.

  Densa   — cosseno do embedding (o baseline; vem do Chroma).
  BM25    — esparsa, IDF-ponderada. Ataca a falha conhecida: 'acelera' é comum (IDF
            baixo, pesa pouco), 'TensorRT' é raro (IDF alto, pesa muito). Foi 'acelera'
            que casou a pergunta de TensorRT com um átomo de RESIZE de imagem.
  Híbrida — Reciprocal Rank Fusion (RRF) da densa com a BM25. Combina "significado" com
            "entidade certa" por POSIÇÃO no ranking, sem escalar scores incompatíveis.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Sequence, Tuple

from mente_digital import textutils


def _termos(texto: str) -> List[str]:
    """Tokens significativos (sem stopword) — a mesma régua do aterramento léxico."""
    return [t for t in textutils.tokens(texto) if t not in textutils.STOP]


class BM25:
    """BM25 Okapi sobre o corpus de átomos. Pré-computa IDF e TF; consulta é barata.

    Parâmetros padrão da literatura (k1=1.5, b=0.75): não afinados aqui de propósito —
    afinar sem um conjunto de validação é o mesmo erro do gate calibrado no escuro.
    """

    def __init__(self, corpus: Sequence[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [_termos(d) for d in corpus]
        self.n = len(self.docs)
        self.dl = [len(d) for d in self.docs]
        self.avgdl = (sum(self.dl) / self.n) if self.n else 0.0
        self.tf: List[Dict[str, int]] = []
        df: Dict[str, int] = defaultdict(int)
        for termos in self.docs:
            freq: Dict[str, int] = defaultdict(int)
            for t in termos:
                freq[t] += 1
            self.tf.append(freq)
            for t in freq:
                df[t] += 1
        # IDF do BM25: log(1 + (N - df + 0.5)/(df + 0.5)). Sempre positivo.
        self.idf = {
            t: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()
        }
        # Índice invertido termo -> docs, para não varrer o corpus inteiro por consulta.
        self.postings: Dict[str, List[int]] = defaultdict(list)
        for i, freq in enumerate(self.tf):
            for t in freq:
                self.postings[t].append(i)

    def scores(self, consulta: str) -> Dict[int, float]:
        termos = _termos(consulta)
        out: Dict[int, float] = defaultdict(float)
        for t in termos:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in self.postings[t]:
                tf = self.tf[i][t]
                denom = tf + self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl)
                out[i] += idf * tf * (self.k1 + 1) / denom
        return out

    def topk(self, consulta: str, k: int) -> List[Tuple[int, float]]:
        s = self.scores(consulta)
        return sorted(s.items(), key=lambda kv: (-kv[1], kv[0]))[:k]


def rrf(rankings: Sequence[Sequence[int]], k: int, const: int = 60) -> List[Tuple[int, float]]:
    """Reciprocal Rank Fusion: score = sum 1/(const + posição) em cada ranking.

    Funde por POSIÇÃO, não por score — por isso combina densa (distância 0..2) e BM25
    (score 0..N) sem normalização arbitrária, que é onde fusões ingênuas erram. `const`
    padrão 60 (do paper original), amortece o peso das primeiras posições.
    """
    acc: Dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for pos, doc in enumerate(ranking):
            acc[doc] += 1.0 / (const + pos)
    return sorted(acc.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
