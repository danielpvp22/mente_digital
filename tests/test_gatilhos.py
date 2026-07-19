"""
Gatilhos condicionais internos (#11, Onda 3): "quando eu adicionar X na lista, <ação>".
Reage a um EVENTO do app (v1: lista_add) executando uma ação armazenada. Distinto do
watcher (web + só avisa). A ação do gatilho roda direto — não re-emite evento (sem loop).
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

AGORA = datetime(2026, 1, 1, 9, 0, 0)


# --- parsers puros ----------------------------------------------------------
def test_separar_gatilho():
    assert mestre.separar_gatilho("quando eu adicionar leite na lista, me lembra de X") == (
        "eu adicionar leite na lista", "me lembra de X"
    )
    assert mestre.separar_gatilho("adiciona leite na lista") is None      # não começa por 'quando'
    assert mestre.separar_gatilho("quando eu adicionar leite") is None    # sem separador


def test_evento_da_condicao():
    assert mestre.evento_da_condicao("eu adicionar leite na lista", AGORA) == ("lista_add", "leite")
    assert mestre.evento_da_condicao("o dolar subir", AGORA) is None      # não é evento conhecido


def test_parse_gatilho_reconhece_e_ignora():
    assert mestre.parse_gatilho("quando eu adicionar leite na lista, avisa", AGORA) == (
        "lista_add", "leite", "avisa"
    )
    # condição não-reconhecida -> None (cai no fluxo normal: watcher/roteador)
    assert mestre.parse_gatilho("quando o dolar subir, me avise", AGORA) is None


def test_gatilho_listar_remover_detectores():
    assert mestre.comando_gatilhos_listar("quais gatilhos eu tenho")
    assert not mestre.comando_gatilhos_listar("quais compromissos")
    assert mestre.comando_gatilho_remover("remove o gatilho 3") == 3
    assert mestre.comando_gatilho_remover("apaga gatilho 7") == 7
    assert mestre.comando_gatilho_remover("remove o item da lista") is None


# --- integração: criar e disparar -------------------------------------------
class _FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "g.db"))
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


async def test_gatilho_dispara_acao_ao_casar_o_evento(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    # cria: "quando eu adicionar leite na lista, adiciona pão na lista de urgente"
    await agent.pipeline_resposta(
        "mestre, quando eu adicionar leite na lista, adiciona pão na lista de urgente", send, mem
    )
    assert len(telemetry.db.gatilhos_listar()) == 1
    assert "pão" not in _conteudo("urgente")          # ainda não disparou (só criou)

    # dispara: adicionar leite casa o evento -> a ação adiciona pão em 'urgente'
    await agent.pipeline_resposta("mestre, adiciona leite na lista", send, mem)
    assert "leite" in _conteudo("compras")            # a ação do usuário rodou
    assert "pão" in _conteudo("urgente") or "pao" in _conteudo("urgente")   # o gatilho rodou


async def test_gatilho_nao_dispara_com_item_diferente(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    await agent.pipeline_resposta(
        "mestre, quando eu adicionar leite na lista, adiciona pão na lista de urgente", send, mem
    )
    # adicionar OUTRO item não casa o filtro 'leite' -> gatilho fica quieto
    await agent.pipeline_resposta("mestre, adiciona café na lista", send, mem)
    assert "pão" not in _conteudo("urgente") and "pao" not in _conteudo("urgente")


async def test_gatilho_listar_e_remover(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    await agent.pipeline_resposta(
        "mestre, quando eu adicionar leite na lista, adiciona pão na lista de urgente", send, mem
    )
    gid = telemetry.db.gatilhos_listar()[0]["id"]
    await agent.pipeline_resposta(f"mestre, remove o gatilho {gid}", send, mem)
    assert telemetry.db.gatilhos_listar() == []
