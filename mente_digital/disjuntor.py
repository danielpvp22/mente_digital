"""Disjuntor da busca web (#31) — circuit breaker anti-shadowban. Puro, clock
injetado, sem IO.

POR QUE existe: quando o DDG começa a recusar (rate-limit) e o código insiste,
o risco real é o SHADOWBAN — o provedor passa a bloquear o IP por muito mais
tempo. A defesa é a mesma de um circuit breaker clássico: depois de algumas
falhas seguidas, ABRE por uma janela de cooldown (~15 min) e para de bater na
porta; enquanto aberto, a busca nem chega à rede. Sucesso zera o contador.

Só conta FALHA de canal (rede/rate-limit). "Zero resultados com sucesso" é uma
resposta legítima (nada encontrado), não uma falha — o chamador não deve
registrá-la aqui, senão uma busca sem hits abriria o disjuntor à toa.
"""
from __future__ import annotations

import time
from typing import Callable


class Disjuntor:
    def __init__(
        self,
        limite_falhas: int,
        cooldown_seg: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limite = max(1, limite_falhas)
        self._cooldown = cooldown_seg
        self._clock = clock          # monotônico injetável (teste determinístico)
        self._falhas = 0
        self._aberto_ate = 0.0       # instante (do clock) até o qual fica aberto

    def registrar_falha(self) -> None:
        self._falhas += 1
        if self._falhas >= self._limite:
            self._aberto_ate = self._clock() + self._cooldown

    def registrar_sucesso(self) -> None:
        self._falhas = 0
        self._aberto_ate = 0.0

    def aberto(self) -> bool:
        """True enquanto na janela de cooldown — a busca deve pular a rede."""
        return self._clock() < self._aberto_ate

    def segundos_restantes(self) -> float:
        return max(0.0, self._aberto_ate - self._clock())
