"""
Ciclo de vida das tasks de fundo (painel 2026-07): exceção em task de background
vira telemetry.error (não aviso de GC), e o shutdown DRENA as tasks (espera→cancela)
em vez de abandoná-las com escrita em voo.
"""
from __future__ import annotations

import asyncio

import pytest

from mente_digital import telemetry as tel
from mente_digital.config import settings
from mente_digital.state import AppContext


async def test_excecao_de_task_e_reportada(monkeypatch):
    erros = []
    monkeypatch.setattr(
        tel.telemetry, "error", lambda mod, msg, exc=None: erros.append((mod, msg, exc))
    )
    ctx = AppContext(settings=settings)

    async def bomba():
        raise RuntimeError("boom na task de fundo")

    t = ctx.track_task(bomba())
    with pytest.raises(RuntimeError):
        await t
    await asyncio.sleep(0)   # done-callbacks rodam via call_soon: dá um tick ao loop
    assert erros, "a exceção da task deveria ter passado pelo telemetry.error"
    assert "boom" in repr(erros[0][2])
    assert not ctx._bg_tasks   # e a task saiu do set (não vaza referência)


async def test_cancelamento_nao_e_erro(monkeypatch):
    # Barge-in/shutdown cancelam tasks o tempo todo — isso é fluxo normal, não erro.
    erros = []
    monkeypatch.setattr(
        tel.telemetry, "error", lambda mod, msg, exc=None: erros.append(msg)
    )
    ctx = AppContext(settings=settings)

    async def eterna():
        await asyncio.sleep(999)

    t = ctx.track_task(eterna())
    await asyncio.sleep(0)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await asyncio.sleep(0)
    assert erros == []


async def test_drenar_espera_primeiro_e_cancela_retardatarias():
    # A task quase pronta CONCLUI (não é rasgada); só a eterna é cancelada.
    ctx = AppContext(settings=settings)
    fim = []

    async def quase_pronta():
        await asyncio.sleep(0.01)
        fim.append("concluiu")

    async def eterna():
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            fim.append("cancelada")
            raise

    ctx.track_task(quase_pronta())
    ctx.track_task(eterna())
    await asyncio.sleep(0)
    await ctx.drenar_tasks(timeout=0.5)
    assert "concluiu" in fim
    assert "cancelada" in fim


async def test_drenar_sem_tasks_e_noop():
    ctx = AppContext(settings=settings)
    await ctx.drenar_tasks(timeout=0.1)   # não pode travar nem explodir
