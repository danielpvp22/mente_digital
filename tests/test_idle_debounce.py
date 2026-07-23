"""
Debounce do idle de conhecimento (2026-07-23).

O ETL não pode pousar na GPU no instante em que a conversa fecha — senão, ao encadear
conversas, ele fica a 100% disputando o STT/decode do próximo turno. A carência
(`idle_grace_seconds`) segura o disparo; abrir outra conversa ou mandar um turno adia.
Aqui exercitamos a máquina de estados do AppContext SEM GPU/ETL real (fake que só grava).
"""
from __future__ import annotations

import asyncio

from mente_digital.config import Settings
from mente_digital.state import AppContext


class _FakeEtl:
    """ETL falso: run_idle grava os itens recebidos e devolve uma corrotina no-op."""

    def __init__(self) -> None:
        self.calls: list = []

    def run_idle(self, itens):
        self.calls.append(list(itens))

        async def _c():
            return None

        return _c()


def _ctx(grace: float) -> "AppContext":
    ctx = AppContext(settings=Settings(_env_file=None, idle_grace_seconds=grace))
    ctx.etl = _FakeEtl()
    return ctx


# -- carência 0 = comportamento antigo (dispara na hora) -----------------------
async def test_grace_zero_roda_imediato():
    ctx = _ctx(0.0)
    ctx.agendar_idle_conhecimento([("a", "1")])
    await asyncio.sleep(0)                       # deixa a task do run_idle completar
    assert ctx.etl.calls == [[("a", "1")]]


# -- com carência: não roda na hora, roda depois -------------------------------
async def test_grace_adia_e_dispara_depois():
    ctx = _ctx(0.02)
    ctx.agendar_idle_conhecimento([("a", "1")])
    assert ctx.etl.calls == []                   # ainda dentro da carência
    await asyncio.sleep(0.06)
    assert ctx.etl.calls == [[("a", "1")]]


# -- adiar cancela o timer e ACUMULA os itens pro próximo agendamento ----------
async def test_adiar_cancela_e_acumula():
    ctx = _ctx(0.02)
    ctx.agendar_idle_conhecimento([("a", "1")])
    ctx.adiar_idle_conhecimento()                # nova atividade: adia
    await asyncio.sleep(0.06)
    assert ctx.etl.calls == []                   # não rodou (foi adiado)
    ctx.agendar_idle_conhecimento([("b", "2")])  # re-arma, acumulando
    await asyncio.sleep(0.06)
    assert ctx.etl.calls == [[("a", "1"), ("b", "2")]]


# -- não atropela decode interativo em voo (re-arma até a GPU liberar) ----------
async def test_nao_atropela_interativo():
    ctx = _ctx(0.02)
    ctx.interactive_idle.clear()                 # simula inferência interativa em voo
    ctx.agendar_idle_conhecimento([("a", "1")])
    await asyncio.sleep(0.06)
    assert ctx.etl.calls == []                   # re-armou, não rodou
    ctx.interactive_idle.set()                   # GPU livre
    await asyncio.sleep(0.06)
    assert ctx.etl.calls == [[("a", "1")]]


# -- o context manager interativo() adia o idle pendente (integração) ----------
async def test_interativo_adia_idle():
    ctx = _ctx(0.02)
    ctx.agendar_idle_conhecimento([("a", "1")])
    async with ctx.interativo():                 # entrar num turno adia o idle pendente
        await asyncio.sleep(0.06)
    assert ctx.etl.calls == []                   # o idle da conversa anterior não atropelou
