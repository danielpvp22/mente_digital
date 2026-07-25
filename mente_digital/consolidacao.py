"""Consolidação de átomos redundantes — Fase 2 (2026-07-25): a parte PURA.

O problema ("3000 relatórios de um assunto"): a base auto-colhida acumula átomos
quase-idênticos sobre o mesmo fato — o dedup do ingest (0.01) só barra o clone
exato, não a mesma ideia refraseada. Aqui os índices são agrupados por distância
de cosseno; o EtlProcessor funde cada grupo num átomo canônico NO IDLE.

As ressalvas do dono são invariantes do design (a fusão LLM-sobre-LLM perde
nuance e cristaliza erro se for frouxa): limiar ALTO e configurável, grupos
ancorados no representante (sem corrente A~B~C com A distante de C), mínimo de
membros, e — no chamador — originais ARQUIVADOS, nunca deletados.

Puro no padrão agenda.py/livro.py: vetores entram, índices saem; numpy só (sem
GPU, sem IO). A ESCALA é a do embedding em uso (e5): recalibrar
MENTE_CONSOLIDACAO_DIST_MAX sempre que trocar o modelo de embedding — a lição do
dedup 0.08→0.01 (escala MiniLM marcaria 75% da base e5 como duplicata).
"""
from __future__ import annotations

from typing import List, Sequence

import numpy as np


def agrupar_redundantes(embeddings: Sequence[Sequence[float]], dist_max: float,
                        min_grupo: int) -> List[List[int]]:
    """Agrupa índices cuja distância de cosseno ao REPRESENTANTE (primeiro livre)
    é <= dist_max. Ancorar no representante — e não em qualquer membro — impede a
    corrente A~B~C em que C já derivou de assunto em relação a A (single-link é o
    jeito clássico de uma consolidação "comer" a base inteira aos poucos).

    Devolve só grupos com >= min_grupo membros, maiores primeiro (o chamador capa
    por ciclo, então os piores acúmulos são tratados antes)."""
    if len(embeddings) == 0:
        return []
    m = np.asarray(embeddings, dtype=np.float32)
    normas = np.linalg.norm(m, axis=1, keepdims=True)
    normas[normas == 0.0] = 1.0
    m = m / normas
    n = m.shape[0]
    livre = np.ones(n, dtype=bool)
    grupos: List[List[int]] = []
    for i in range(n):
        if not livre[i]:
            continue
        dist = 1.0 - (m @ m[i])
        membros = [int(j) for j in np.flatnonzero(livre & (dist <= dist_max))]
        for j in membros:
            livre[j] = False
        if len(membros) >= min_grupo:
            grupos.append(membros)
    grupos.sort(key=len, reverse=True)
    return grupos
