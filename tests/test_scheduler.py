"""
Testes do SchedulerService: disparo de lembrete, entrega pendente (sem ouvinte) e
reprogramação de recorrência. Usa um SQLite temporário e fakes (sem GPU/rede).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

import telemetry
from conftest import FakeTts
from scheduler import SchedulerService


class FakeSession:
    def __init__(self) -> None:
        self.recebidos: list[dict] = []

    async def safe_send(self, data: dict) -> bool:
        self.recebidos.append(data)
        return True


class FakeCtx:
    def __init__(self, sessoes) -> None:
        self.sessoes = set(sessoes)
        self.tts = FakeTts()
        self.web = None
        self.llama = None
        self.interactive_idle = asyncio.Event()
        self.interactive_idle.set()


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    """Aponta o singleton `db` para um SQLite temporário e cria o schema."""
    caminho = str(tmp_path / "sched_test.db")
    monkeypatch.setattr(telemetry.db, "path", caminho)
    telemetry.db.init()
    return telemetry.db


async def test_lembrete_dispara_e_conclui(db_tmp):
    sessao = FakeSession()
    ctx = FakeCtx([sessao])
    sched = SchedulerService(ctx)

    passado = (datetime.now() - timedelta(minutes=1)).isoformat()
    ag_id = db_tmp.criar_agendamento("lembrete", "tomar remédio", passado, None, None, None)

    await sched.tick()

    # A sessão recebeu a bolha proativa com a mensagem.
    proativos = [m for m in sessao.recebidos if m.get("tipo") == "proativo"]
    assert proativos and "tomar remédio" in proativos[0]["texto"]
    # E o agendamento único foi concluído (não dispara de novo).
    assert db_tmp.listar_agendamentos() == []


async def test_sem_ouvinte_vira_pendente_e_entrega_depois(db_tmp):
    ctx = FakeCtx([])  # ninguém conectado
    sched = SchedulerService(ctx)
    passado = (datetime.now() - timedelta(minutes=1)).isoformat()
    ag_id = db_tmp.criar_agendamento("lembrete", "beber água", passado, None, None, None)

    await sched.tick()
    # Sem sessão: não pode ter concluído; ficou pendente de entrega.
    pendentes = db_tmp.get_agendamentos_pendentes()
    assert len(pendentes) == 1 and pendentes[0]["id"] == ag_id

    # Chega uma conexão e pedimos a entrega dos pendentes.
    sessao = FakeSession()
    ctx.sessoes.add(sessao)
    await sched.entregar_pendentes()

    assert any("beber água" in m.get("texto", "") for m in sessao.recebidos)
    assert db_tmp.get_agendamentos_pendentes() == []


async def test_recorrente_reprograma_proximo(db_tmp):
    sessao = FakeSession()
    ctx = FakeCtx([sessao])
    sched = SchedulerService(ctx)

    disparo = (datetime.now() - timedelta(minutes=1)).isoformat()
    ag_id = db_tmp.criar_agendamento("lembrete", "alongar", disparo, "diario", None, None)

    await sched.tick()

    ativos = db_tmp.listar_agendamentos()
    assert len(ativos) == 1 and ativos[0]["id"] == ag_id
    # Reprogramado para o futuro (não dispara de novo no mesmo tick).
    prox = datetime.fromisoformat(ativos[0]["proximo_disparo"])
    assert prox > datetime.now()


async def test_cancelar_remove_dos_vencidos(db_tmp):
    ctx = FakeCtx([FakeSession()])
    sched = SchedulerService(ctx)
    passado = (datetime.now() - timedelta(minutes=1)).isoformat()
    ag_id = db_tmp.criar_agendamento("lembrete", "não me lembre", passado, None, None, None)

    assert db_tmp.cancelar_agendamento(ag_id) is True
    await sched.tick()
    # Cancelado antes do tick: ninguém foi notificado.
    assert all(m.get("tipo") != "proativo" for s in ctx.sessoes for m in s.recebidos)
