"""
SRS (#43, Onda 3) — lógica de repetição espaçada (Leitner). Pura/testável, sem I/O.

O agendamento de fato (datas, banco) vive no Agent/db; aqui só a REGRA de caixas: um
acerto avança o card para a caixa seguinte (intervalo maior), um erro o joga de volta
para a primeira. É o suficiente para fixar na memória de longo prazo sem a complexidade
do SM-2 completo.
"""
from __future__ import annotations

from typing import List, Tuple


def proximo(estagio: int, acertou: bool, intervalos: List[int]) -> Tuple[int, int]:
    """Devolve (novo_estágio, dias até a próxima revisão).

    Acerto AVANÇA a caixa (com teto na última); erro VOLTA à primeira. Sem intervalos
    configurados, cai num fallback seguro de (0, 1 dia) em vez de estourar."""
    if not intervalos:
        return 0, 1
    if acertou:
        novo = min(max(estagio, 0) + 1, len(intervalos) - 1)
    else:
        novo = 0
    return novo, intervalos[novo]
