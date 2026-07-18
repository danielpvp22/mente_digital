"""
Trilha de Auditoria (#27): ações com efeito entram na trilha; leituras não; e o modo
confidencial (#5) não audita. "o que você fez hoje" lê a trilha do dia.
"""
from __future__ import annotations

import pytest

import telemetry
import tools
from agent import Agent
from config import settings
from state import AppContext

from conftest import FakeTts, make_send


class FakeVS:
    async def sync(self):
        return None


@pytest.fixture
def amb(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "t.db"))
    telemetry.db.init()
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_listas.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _agent():
    ctx = AppContext(settings=settings)
    ctx.tts = FakeTts()
    ctx.vectorstore = FakeVS()
    return Agent(ctx)


async def test_acao_entra_na_trilha(amb):
    agent = _agent()
    send, _ = make_send()
    await agent._executar_acoes_rapidas(
        [tools.Decisao("adicionar_item", {"lista": "compras", "item": "pão"})], send, auditar=True
    )
    itens = telemetry.db.get_auditoria()
    assert len(itens) == 1 and "pão" in itens[0]["detalhe"]


async def test_confidencial_nao_audita(amb):
    agent = _agent()
    send, _ = make_send()
    await agent._executar_acoes_rapidas(
        [tools.Decisao("adicionar_item", {"lista": "compras", "item": "segredo"})], send, auditar=False
    )
    assert telemetry.db.get_auditoria() == []


async def test_leitura_nao_audita(amb):
    # status_sistema é leitura (auditavel=False) -> não entra na trilha.
    agent = _agent()
    agent.ctx.llama = type("F", (), {"ready": True})()
    agent.ctx.stt = type("F", (), {"ready": True})()
    send, _ = make_send()
    await agent._executar_acoes_rapidas([tools.Decisao("status_sistema", {})], send, auditar=True)
    assert telemetry.db.get_auditoria() == []


async def test_auditoria_hoje_fala(amb):
    telemetry.db.registrar_auditoria("adicionar_item", "adicionei 'leite' à lista de compras")
    resp = await tools._t_auditoria({}, None)
    assert "Hoje eu" in resp and "leite" in resp


async def test_auditoria_vazia(amb):
    resp = await tools._t_auditoria({}, None)
    assert "nenhuma ação" in resp.lower()
