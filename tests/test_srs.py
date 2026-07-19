"""
SRS — repetição espaçada (#43, Onda 3): "mestre, revisa isso" cria um card da última
troca; "mestre, revisão" puxa os vencidos; "mostra / acertei / errei" conduzem (Leitner:
acerto avança a caixa, erro volta à primeira). Manual + sob demanda (sem push).

Pura (srs.proximo), detectores (mestre) e integração ponta-a-ponta com db real temporário.
"""
from datetime import datetime

import mestre
import srs
import telemetry
from agent import Agent
from config import settings
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


# --- Leitner puro -----------------------------------------------------------
def test_srs_proximo_acerto_avanca_com_teto():
    ints = [1, 3, 7]
    assert srs.proximo(0, True, ints) == (1, 3)
    assert srs.proximo(1, True, ints) == (2, 7)
    assert srs.proximo(2, True, ints) == (2, 7)     # teto na última caixa


def test_srs_proximo_erro_volta_a_primeira():
    assert srs.proximo(2, False, [1, 3, 7]) == (0, 1)


def test_srs_proximo_sem_intervalos_e_seguro():
    assert srs.proximo(3, True, []) == (0, 1)


# --- detectores (recebem o comando já SEM a palavra-mestre) ------------------
def test_srs_marcar_detecta():
    for c in ("revisa isso", "marca pra revisar", "quero revisar isso", "coloca na revisao"):
        assert mestre.comando_srs_marcar(c), c


def test_srs_iniciar_detecta():
    for c in ("revisão", "vamos revisar", "hora de revisar", "quero revisar"):
        assert mestre.comando_srs_iniciar(c), c


def test_srs_sub_comandos():
    assert mestre.comando_srs_mostrar("mostra")
    assert mestre.comando_srs_acertei("acertei")
    assert mestre.comando_srs_errei("errei")
    assert mestre.comando_srs_parar("chega de revisão")
    assert mestre.comando_srs_sub("acertei") and mestre.comando_srs_sub("mostra")
    assert not mestre.comando_srs_sub("adiciona pão na lista")   # fora de revisão, palavra comum


# --- integração ponta-a-ponta (db real temporário) --------------------------
class _FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "srs.db"))
    telemetry.db.init()
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["ok."])
    ctx.tts = FakeTts()
    ctx.vectorstore = _FakeVS()
    monkeypatch.setattr("agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    return Agent(ctx), SessionMemory(settings)


async def test_srs_fluxo_completo_acerto(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    # uma troca de conhecimento na sessão, e "revisa isso"
    mem.registrar_turno("o que é entropia?", "Entropia é uma medida de desordem.")
    await agent.pipeline_resposta("mestre, revisa isso", send, mem)
    assert telemetry.db.srs_contar_vencidos(datetime.now().isoformat()) == 1   # card criado, vencido

    # "revisão" abre a sessão com o card
    await agent.pipeline_resposta("mestre, revisão", send, mem)
    assert mem.revisao is not None
    assert mem.revisao["atual"]["frente"] == "o que é entropia?"
    assert mem.revisao["revelado"] is False

    # "mostra" revela o verso
    await agent.pipeline_resposta("mestre, mostra", send, mem)
    assert mem.revisao["revelado"] is True

    # "acertei" reagenda (empurra pra frente) e encerra a fila
    await agent.pipeline_resposta("mestre, acertei", send, mem)
    assert mem.revisao is None
    assert telemetry.db.srs_contar_vencidos(datetime.now().isoformat()) == 0   # não vence mais hoje


async def test_srs_erro_mantem_vencido_para_hoje(monkeypatch, tmp_path):
    # erro volta à caixa 0 (intervalo mínimo); com o menor intervalo >= 1 dia, sai do "hoje".
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    mem.registrar_turno("capital da França?", "Paris.")
    await agent.pipeline_resposta("mestre, revisa isso", send, mem)
    await agent.pipeline_resposta("mestre, revisão", send, mem)
    await agent.pipeline_resposta("mestre, errei", send, mem)
    assert mem.revisao is None
    # reagendado para +1 dia (caixa 0), então não está mais vencido AGORA
    assert telemetry.db.srs_contar_vencidos(datetime.now().isoformat()) == 0


async def test_srs_revisao_vazia(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    await agent.pipeline_resposta("mestre, revisão", send, mem)   # nada marcado
    assert mem.revisao is None                                    # não abre sessão


async def test_srs_comando_novo_abandona_revisao(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    mem.registrar_turno("o que é RAM?", "Memória volátil.")
    await agent.pipeline_resposta("mestre, revisa isso", send, mem)
    await agent.pipeline_resposta("mestre, revisão", send, mem)
    assert mem.revisao is not None
    # um comando NÃO-relacionado abandona a revisão (não prende o usuário)
    await agent.pipeline_resposta("mestre, adiciona pão na lista", send, mem)
    assert mem.revisao is None
