"""
Tutor Socrático (#44, Onda 3): modo de sessão que faz o assistente PERGUNTAR em vez de
entregar a resposta pronta. Injeta a instrução socrática via verbosidade.aplicar_tutor
(reusa a fiação nivel->LLM do #7/#45). Ligado/desligado por comando-mestre.
"""
from mente_digital import mestre
from mente_digital import verbosidade
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


def test_aplicar_tutor_sobrepoe_ou_nao():
    base = verbosidade.classificar("o que é fotossíntese")
    ativo = verbosidade.aplicar_tutor(base, True)
    assert ativo.nome == "tutor"
    assert "SOCRÁTICO" in ativo.instrucao
    assert ativo.max_tokens == settings.max_tokens_resposta       # espaço p/ a pergunta guiada
    assert verbosidade.aplicar_tutor(base, False) is base         # inativo não muda


def test_comando_tutor():
    assert mestre.comando_tutor("modo tutor") is True
    assert mestre.comando_tutor("seja meu tutor") is True
    assert mestre.comando_tutor("sai do tutor") is False
    assert mestre.comando_tutor("chega de tutor") is False
    assert mestre.comando_tutor("o que é RAG") is None


class _FakeVS:
    async def sync(self):
        pass


async def test_tutor_toggle_pela_sessao(monkeypatch, tmp_path):
    monkeypatch.setattr("mente_digital.agent.db.listar_atalhos", lambda *a, **k: {})
    ctx = AppContext(settings=settings)
    ctx.llama, ctx.tts, ctx.vectorstore = FakeLlama(["ok."]), FakeTts(), _FakeVS()
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "d.md"))
    agent, mem = Agent(ctx), SessionMemory(settings)
    send, _ = make_send()

    assert mem.tutor is False
    await agent.pipeline_resposta("mestre, modo tutor", send, mem)
    assert mem.tutor is True
    await agent.pipeline_resposta("mestre, sai do tutor", send, mem)
    assert mem.tutor is False
