"""
Atalho de intenção frequente (#2): o app CONTA as intenções-mestre e, quando uma vira
hábito, OFERECE um atalho (uma vez). "mestre, atalho X" nomeia o último comando; depois
"mestre, X" expande de volta.

DB testado num arquivo temporário; o fluxo com LLM/TTS/vault fakes.
"""
import os

from mente_digital import tools
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory
from mente_digital.telemetry import Database

from conftest import FakeLlama, FakeTts, make_send


# ---------------------------------------------------------------------------
# Camada de banco
# ---------------------------------------------------------------------------
def test_db_frequencia_conta_e_marca(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    assert db.registrar_frequencia("diagnostico", "diagnóstico") == (1, False)
    assert db.registrar_frequencia("diagnostico", "diagnóstico") == (2, False)
    n, sug = db.registrar_frequencia("diagnostico", "diagnóstico")
    assert n == 3 and sug is False
    db.marcar_sugerido("diagnostico")
    assert db.registrar_frequencia("diagnostico", "diagnóstico") == (4, True)   # já sugerido


def test_db_atalhos_salva_e_lista(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    db.salvar_atalho("compras", "adiciona pão na lista")
    assert db.listar_atalhos() == {"compras": "adiciona pão na lista"}


# ---------------------------------------------------------------------------
# Fluxo
# ---------------------------------------------------------------------------
def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["ok."])
    ctx.tts = FakeTts()
    ctx.vectorstore = type("VS", (), {"sync": lambda self: _noop()})()
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.registrar_auditoria", lambda *a, **k: None)
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_listas.mkdir(parents=True, exist_ok=True)
    agent = Agent(ctx)
    agent._atalhos = {}   # cache vazio (não toca o DB real)
    return agent, SessionMemory(settings)


async def _noop():
    pass


def _fala(enviados):
    return "".join(m["texto"] for m in enviados if m.get("tipo") == "token")


async def test_sugere_atalho_ao_virar_habito(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    marcados = []
    # 3ª vez, ainda não sugerido -> deve oferecer.
    monkeypatch.setattr("mente_digital.agent.db.registrar_frequencia", lambda a, e: (3, False))
    monkeypatch.setattr("mente_digital.agent.db.marcar_sugerido", lambda a: marcados.append(a))
    send, enviados = make_send()

    await agent.pipeline_resposta("mestre, adiciona pão na lista", send, mem)

    assert "atalho" in _fala(enviados).lower()          # ofereceu
    assert marcados                                      # marcou p/ não repetir


async def test_nao_sugere_se_ja_sugerido(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    monkeypatch.setattr("mente_digital.agent.db.registrar_frequencia", lambda a, e: (9, True))
    monkeypatch.setattr("mente_digital.agent.db.marcar_sugerido", lambda a: None)
    send, enviados = make_send()

    await agent.pipeline_resposta("mestre, adiciona pão na lista", send, mem)
    assert "atalho" not in _fala(enviados).lower()


async def test_cria_e_expande_atalho(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    monkeypatch.setattr("mente_digital.agent.db.registrar_frequencia", lambda a, e: (1, False))
    monkeypatch.setattr("mente_digital.agent.db.marcar_sugerido", lambda a: None)
    salvos = {}
    monkeypatch.setattr("mente_digital.agent.db.salvar_atalho", lambda nome, cmd: salvos.__setitem__(nome, cmd))
    send, _ = make_send()

    # 1) uma ação real -> vira o "último comando".
    await agent.pipeline_resposta("mestre, adiciona leite na lista de tarefas", send, mem)
    assert mem.ultimo_comando_mestre == "adiciona leite na lista de tarefas"

    # 2) nomeia esse comando como atalho "tarefas".
    await agent.pipeline_resposta("mestre, atalho tarefas", send, mem)
    assert salvos.get("tarefas") == "adiciona leite na lista de tarefas"
    assert agent._atalhos.get("tarefas") == "adiciona leite na lista de tarefas"

    # 3) usar o atalho expande e executa o comando original.
    send2, _ = make_send()
    await agent.pipeline_resposta("mestre, tarefas", send2, mem)
    caminho = tools._caminho_lista("tarefas")
    conteudo = open(caminho, encoding="utf-8").read()
    assert conteudo.count("leite") == 2       # a ação rodou 2x (direta + via atalho)


async def test_atalho_sem_comando_recente(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, enviados = make_send()
    await agent.pipeline_resposta("mestre, atalho compras", send, mem)
    assert "não tenho" in _fala(enviados).lower()
