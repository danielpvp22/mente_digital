"""
Modo Confidencial (#5): "mestre, modo sigiloso" faz o turno viver só na RAM —
sem dump, sem SQLite, sem fila de ETL. O follow-up (chat_history) segue funcionando.

Sem GPU/rede: LLM, TTS, vectorstore e web são fakes.
"""
from agent import Agent
from config import settings
from rag import NENHUM, LocalResult
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class FakeWeb:
    def __init__(self):
        self.prefetches = 0

    async def search(self, termo, consulta=None):
        return "- FONTE WEB: conteúdo genérico sobre o tema."

    async def prefetch(self, tema):
        self.prefetches += 1
        return "- CONTEXTO AMPLO."


class FakeVSVazio:
    async def search(self, termos, texto_busca=None):
        return LocalResult(NENHUM, None, False)

    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Arquitetura ", "é ", "importante."])
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = FakeVSVazio()
    salvos = []
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: salvos.append(a))
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    # Hermético: não deixar o pipeline tocar o DB real do projeto (lacunas etc.).
    monkeypatch.setattr("agent.db.save_lacuna", lambda *a, **k: None)
    dump = tmp_path / "dump.md"
    monkeypatch.setattr("agent.settings.arquivo_chat_dump", str(dump))
    return Agent(ctx), SessionMemory(settings), dump, salvos


async def test_modo_sigiloso_nao_persiste_mas_mantem_ram(monkeypatch, tmp_path):
    agent, mem, dump, salvos = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    # Liga o modo pela palavra-mestre.
    await agent.pipeline_resposta("mestre, modo sigiloso", send, mem)
    assert mem.confidencial is True
    salvos.clear()  # o próprio meta-comando não conta

    # Uma pergunta normal (não-efêmera) em modo sigiloso.
    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert salvos == []                       # nada no SQLite
    assert list(mem.fila_etl) == []           # nada pra ETL/atomização
    assert agent.ctx.web.prefetches == 0      # nem a curiosidade do pre-fetch
    assert (not dump.exists()) or dump.read_text(encoding="utf-8") == ""  # dump limpo
    assert len(mem.chat_history) >= 1         # RAM preservada -> follow-up funciona


async def test_desligar_volta_a_persistir(monkeypatch, tmp_path):
    agent, mem, dump, salvos = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, modo sigiloso", send, mem)
    await agent.pipeline_resposta("mestre, modo normal", send, mem)
    assert mem.confidencial is False
    salvos.clear()

    await agent.pipeline_resposta("me fale sobre redes neurais", send, mem)
    assert len(salvos) == 1                    # voltou a gravar no SQLite
