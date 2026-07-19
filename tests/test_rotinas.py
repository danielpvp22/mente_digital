"""
Rotinas compostas (#10, Onda 3): macro NOMEADA de comandos. "cria a rotina X: <composto>"
salva; "rotina X" expande para o composto e o parse_composto executa os passos.
"""
import os
from datetime import datetime

import mestre
import telemetry
import tools
from agent import Agent
from config import settings
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


# --- parsers ----------------------------------------------------------------
def test_parse_rotina_criar():
    assert mestre.parse_rotina_criar("cria a rotina manhã: adiciona café e faz X") == (
        "manha", "adiciona café e faz X"
    )
    assert mestre.parse_rotina_criar("rotina foco = modo tutor") == ("foco", "modo tutor")
    assert mestre.parse_rotina_criar("rotina manhã") is None       # sem corpo


def test_parse_rotina_rodar():
    assert mestre.parse_rotina_rodar("rotina manhã") == "manha"
    assert mestre.parse_rotina_rodar("executa a rotina foco") == "foco"
    assert mestre.parse_rotina_rodar("rotina manhã: X") is None     # criação, não execução
    assert mestre.parse_rotina_rodar("remove a rotina foco") is None
    assert mestre.parse_rotina_rodar("quais rotinas") is None


def test_rotina_listar_remover_detectores():
    assert mestre.comando_rotinas_listar("quais rotinas eu tenho")
    assert mestre.comando_rotina_remover("remove a rotina foco") == "foco"
    assert mestre.comando_rotina_remover("adiciona pão na lista") is None


# --- integração: criar e executar a macro -----------------------------------
class _FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "rot.db"))
    telemetry.db.init()
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_listas.mkdir(parents=True, exist_ok=True)
    ctx = AppContext(settings=settings)
    ctx.llama, ctx.tts, ctx.vectorstore = FakeLlama(["ok."]), FakeTts(), _FakeVS()
    monkeypatch.setattr("agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    return Agent(ctx), SessionMemory(settings)


def _conteudo(nome):
    caminho = tools._caminho_lista(nome)
    return open(caminho, encoding="utf-8").read().lower() if os.path.exists(caminho) else ""


async def test_rotina_cria_e_executa_os_passos(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta(
        "mestre, cria a rotina manhã: adiciona café na lista e adiciona pão na lista de urgente",
        send, mem,
    )
    assert telemetry.db.rotina_get("manha") is not None
    assert not _conteudo("compras")                    # criar não executa

    await agent.pipeline_resposta("mestre, rotina manhã", send, mem)
    assert "café" in _conteudo("compras") or "cafe" in _conteudo("compras")
    assert "pão" in _conteudo("urgente") or "pao" in _conteudo("urgente")


async def test_rotina_remover(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    await agent.pipeline_resposta("mestre, rotina foco = adiciona água na lista", send, mem)
    assert telemetry.db.rotina_get("foco") is not None
    await agent.pipeline_resposta("mestre, remove a rotina foco", send, mem)
    assert telemetry.db.rotina_get("foco") is None
