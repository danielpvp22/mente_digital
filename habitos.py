"""
Diário de hábitos (#37, Onda 3) — cálculo de SEQUÊNCIA (streak). Puro/testável, sem I/O.

O registro (banco) vive no db/Agent; aqui só a regra da sequência: dias consecutivos
terminando em hoje — ou em ontem, se hoje ainda não foi marcado (não pune a sequência
antes do fim do dia). É "fluxo independente": não toca no pipeline de conhecimento.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Set


def streak(datas: Set[date], hoje: date) -> int:
    """Dias CONSECUTIVOS de hábito terminando em `hoje` (ou em `hoje-1`, se hoje ainda não
    foi marcado). `datas` = conjunto de date em que o hábito foi cumprido. Puro."""
    d = hoje if hoje in datas else hoje - timedelta(days=1)
    n = 0
    while d in datas:
        n += 1
        d -= timedelta(days=1)
    return n
