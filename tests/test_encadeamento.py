"""
Encadeamento falado (#12): "mestre, faz X e faz Y" executa VÁRIAS ações num turno.
O parse_composto divide nas fronteiras de ação e roda cada parte; se uma parte não
resolve, o TODO defere ao LLM (nunca faz metade).

Interação com o cofre (#25): num composto com ação destrutiva, as SEGURAS rodam já e a
destrutiva fica aguardando "confirma".

Sem GPU/rede: LLM/TTS fakes; db.cancelar_agendamento espionado; vault em tmp_path.
"""
import os

import tools
from agent import Agent
from config import settings
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["ok."])
    ctx.tts = FakeTts()
    ctx.vectorstore = FakeVS()
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.registrar_auditoria", lambda *a, **k: None)
    cancelados = []
    monkeypatch.setattr("tools.db.cancelar_agendamento",
                        lambda ag_id: (cancelados.append(ag_id), True)[1])
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_listas.mkdir(parents=True, exist_ok=True)
    return Agent(ctx), SessionMemory(settings), cancelados


def _lista(nome):
    caminho = tools._caminho_lista(nome)
    return open(caminho, encoding="utf-8").read() if os.path.exists(caminho) else ""


async def test_duas_adicoes_em_listas_diferentes(monkeypatch, tmp_path):
    agent, mem, _ = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta(
        "mestre, adiciona pão na lista e adiciona leite na lista de tarefas", send, mem
    )

    assert "pão" in _lista("compras")
    assert "leite" in _lista("tarefas")


async def test_composto_com_destrutiva_executa_segura_e_confirma_resto(monkeypatch, tmp_path):
    agent, mem, cancelados = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona pão na lista e cancela o lembrete 3", send, mem)
    assert "pão" in _lista("compras")            # a ação segura rodou já
    assert cancelados == []                       # a destrutiva ficou pendente
    assert mem.confirmacao_pendente is not None

    await agent.pipeline_resposta("mestre, confirma", send, mem)
    assert cancelados == [3]                       # confirmada -> executou


async def test_composto_mal_formado_defere_nao_faz_metade(monkeypatch, tmp_path):
    # 2ª parte tem mensagem ("comprar") -> parse_composto devolve None -> cai no roteador
    # LLM (FakeLlama não devolve tool) -> comando desconhecido; a 1ª parte NÃO é executada.
    agent, mem, _ = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta(
        "mestre, adiciona pão na lista e me lembra de comprar leite amanhã", send, mem
    )
    assert "pão" not in _lista("compras")          # não fez metade
