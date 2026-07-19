"""
Ajuda (/help falável): "mestre, ajuda" / "mestre /ajuda" / "o que você pode fazer" fala
um resumo das capacidades. Checado tarde no fluxo pra não roubar comandos estruturados.
"""
import mestre
from agent import Agent
from config import settings
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


def test_comando_ajuda_detecta():
    for c in ("ajuda", "/ajuda", "o que voce pode fazer", "quais comandos", "help", "me ajuda"):
        assert mestre.comando_ajuda(c), c
    assert not mestre.comando_ajuda("adiciona pão na lista")


class _FakeVS:
    async def sync(self):
        pass


async def test_ajuda_fala_capacidades(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.db.listar_atalhos", lambda *a, **k: {})
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    ctx = AppContext(settings=settings)
    ctx.llama, ctx.tts, ctx.vectorstore = FakeLlama(["ok."]), FakeTts(), _FakeVS()
    monkeypatch.setattr("agent.settings.arquivo_chat_dump", str(tmp_path / "d.md"))
    agent, mem = Agent(ctx), SessionMemory(settings)

    for cmd in ("mestre, ajuda", "mestre /ajuda", "mestre, o que você pode fazer"):
        agent.ctx.tts.chamadas.clear()
        send, _ = make_send()
        await agent.pipeline_resposta(cmd, send, mem)
        falado = " ".join(agent.ctx.tts.chamadas).lower()
        assert "pomodoro" in falado and "lista" in falado, cmd
