"""
Roteamento de pergunta DEFINICIONAL para a web (Part A, Onda 3): "o que é X" / "quem
foi Y" / "me explica Z" pulam a cascata local e vão DIRETO pra web — como o
talvez_tempo_real. Conserta o sintoma "pergunta geral puxa nota pessoal" (o Tarkov).
Pergunta PESSOAL (meu/eu/nosso) é excluída e segue o pipeline local.

Prova o WIRING no _pipeline (não só a função pura tools.pergunta_definicional): conta
as buscas no Banco vs na web. Sem GPU/rede: tudo fake, como test_early_stop.
"""
from collections import deque

from agent import Agent
from config import settings
from rag import LocalResult
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class SpyVS:
    """Vectorstore falso que CONTA as buscas — para provar se o Banco foi (ou não) consultado."""

    def __init__(self):
        self.search_calls = 0

    async def search(self, termos, texto_busca=None):
        self.search_calls += 1
        return LocalResult("átomo do banco sobre o tema", 0.3, True, [])

    async def sync(self):
        pass


class FakeWeb:
    def __init__(self):
        self.searches = 0

    async def search(self, termo, consulta=None):
        self.searches += 1
        return "- FONTE WEB: definição geral."

    async def prefetch(self, tema):
        return "- CONTEXTO AMPLO."


class FakeOptimizer:
    def __init__(self, termos):
        self._termos = termos

    async def optimize(self, texto, history):
        return self._termos


def _agent(monkeypatch, tmp_path, vs):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Resposta ", "real ", "aqui."])
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = vs
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr("agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = FakeOptimizer("rag conceito")
    mem = SessionMemory(settings)
    mem.conhecimento_sessao = deque()   # sem RAM: isola a decisão local-vs-web
    return agent, mem


async def test_definicional_vai_direto_pra_web(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.settings.rotear_definicional_web", True)
    vs = SpyVS()
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("o que é RAG?", send, mem)

    assert vs.search_calls == 0            # Banco NÃO foi consultado (pulou o local)
    assert agent.ctx.web.searches == 1     # foi direto pra web


async def test_definicional_desligado_cai_na_cascata(monkeypatch, tmp_path):
    # Botão off: volta ao comportamento antigo (cascata local normal).
    monkeypatch.setattr("agent.settings.rotear_definicional_web", False)
    vs = SpyVS()
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("o que é RAG?", send, mem)

    assert vs.search_calls == 1            # sem o roteamento, o Banco roda


async def test_pergunta_pessoal_nao_vai_pra_web(monkeypatch, tmp_path):
    # "reservando local pra pessoais": marcador de posse mantém na cascata local.
    monkeypatch.setattr("agent.settings.rotear_definicional_web", True)
    vs = SpyVS()
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("o que é o meu projeto?", send, mem)

    assert vs.search_calls == 1            # pessoal -> Banco local, não pula pra web
    assert agent.ctx.web.searches == 0
