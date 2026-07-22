"""
Cofre de confirmação (#25) + Confirmação redundante (#15).

Ações destrutivas E não-desfazíveis (hoje só `cancelar_lembrete` — o undo #8 não
recria um lembrete) NÃO rodam de imediato: ficam pendentes até um "mestre, confirma".
"mestre, não" aborta; qualquer outro comando abandona a pendência (não prende o
usuário). As ações que o undo cobre (add/remove) NÃO são gateadas — seria a confirmação
REDUNDANTE (#15). Botão `confirmacao_habilitada` desliga tudo.

Sem GPU/rede: LLM/TTS fakes; db.cancelar_agendamento espionado; vault em tmp_path.
"""
import os

from mente_digital import tools
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["ok."])
    ctx.tts = FakeTts()
    ctx.vectorstore = FakeVS()
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.registrar_auditoria", lambda *a, **k: None)
    # Espiona os cancelamentos REAIS (a ferramenta chama tools.db.cancelar_agendamento).
    cancelados = []
    monkeypatch.setattr("mente_digital.tools.db.cancelar_agendamento",
                        lambda ag_id: (cancelados.append(ag_id), True)[1])
    # Vault temporário p/ o caso "adiciona pão" (supersede) escrever de verdade.
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_listas.mkdir(parents=True, exist_ok=True)
    return Agent(ctx), SessionMemory(settings), cancelados


def _fala(enviados):
    return "".join(m["texto"] for m in enviados if m.get("tipo") == "token")


async def test_cancelar_pede_confirmacao_antes(monkeypatch, tmp_path):
    agent, mem, cancelados = _agent(monkeypatch, tmp_path)
    send, enviados = make_send()

    await agent.pipeline_resposta("mestre, cancela o lembrete 3", send, mem)

    assert cancelados == []                          # NÃO cancelou ainda
    assert mem.confirmacao_pendente is not None      # ficou pendente
    assert "confirma" in _fala(enviados).lower()


async def test_confirma_executa(monkeypatch, tmp_path):
    agent, mem, cancelados = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, cancela o lembrete 3", send, mem)
    await agent.pipeline_resposta("mestre, confirma", send, mem)

    assert cancelados == [3]                          # AGORA cancelou
    assert mem.confirmacao_pendente is None           # pendência consumida


async def test_aborta_nao_executa(monkeypatch, tmp_path):
    agent, mem, cancelados = _agent(monkeypatch, tmp_path)
    send, enviados = make_send()

    await agent.pipeline_resposta("mestre, cancela o lembrete 3", send, mem)
    send2, enviados2 = make_send()
    await agent.pipeline_resposta("mestre, não", send2, mem)

    assert cancelados == []                            # nunca cancelou
    assert mem.confirmacao_pendente is None            # abortado
    assert "não vou" in _fala(enviados2).lower()


async def test_outro_comando_abandona_pendencia(monkeypatch, tmp_path):
    # #15: não prende o usuário — um comando novo supera a confirmação pendente.
    agent, mem, cancelados = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, cancela o lembrete 3", send, mem)
    await agent.pipeline_resposta("mestre, adiciona pão na lista", send, mem)

    assert mem.confirmacao_pendente is None            # pendência abandonada
    assert cancelados == []                            # o cancel não vazou
    caminho = tools._caminho_lista("compras")
    assert os.path.exists(caminho) and "pão" in open(caminho, encoding="utf-8").read()


async def test_add_remove_nao_pedem_confirmacao(monkeypatch, tmp_path):
    # #15: ações que o undo cobre NÃO são gateadas (confirmação seria redundante).
    agent, mem, _ = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona leite na lista", send, mem)
    assert mem.confirmacao_pendente is None            # rodou direto, sem pendência


async def test_botao_desligado_executa_direto(monkeypatch, tmp_path):
    monkeypatch.setattr("mente_digital.agent.settings.confirmacao_habilitada", False)
    agent, mem, cancelados = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, cancela o lembrete 3", send, mem)

    assert cancelados == [3]                            # sem gate: cancelou na hora
    assert mem.confirmacao_pendente is None
