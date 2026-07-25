"""
Early-stop da cascata (#3): quando LIGADO (default), a cascata PARA na primeira fonte
que responde com confiança. Se a RAM (memória fresca da sessão) já respondeu, o Banco
vetorial nem é consultado — economiza um decode na GPU serializada. Desligado, volta à
fusão RAM+Banco (cada fonte um parágrafo).

Sem GPU/rede: LLM/TTS/web/vectorstore são fakes; o optimizer é fixado; DB neutralizado.
"""
from collections import deque

from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.rag import LocalResult
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class SpyVS:
    """Vectorstore falso que CONTA as buscas — para provar que o early-stop pulou o Banco."""

    def __init__(self, relevante=True):
        self.search_calls = 0
        self.relevante = relevante

    async def search(self, termos, texto_busca=None, economico=False):
        self.search_calls += 1
        # Um contexto qualquer (não-sentinela); `relevante` controla se entra na fusão.
        return LocalResult("átomo do banco sobre o tema", 0.3, self.relevante, [])

    async def sync(self):
        pass


class FakeWeb:
    def __init__(self):
        self.searches = 0

    async def search(self, termo, consulta=None, on_colheita=None):
        self.searches += 1
        return "- FONTE WEB: genérico."

    async def prefetch(self, tema):
        return "- CONTEXTO AMPLO."


class FakeOptimizer:
    """Fixa os termos de busca (senão o optimizer chamaria o LLM)."""

    def __init__(self, termos):
        self._termos = termos

    async def optimize(self, texto, history):
        return self._termos


def _agent(monkeypatch, tmp_path, vs):
    ctx = AppContext(settings=settings)
    # Resposta real (não é prefixo do sentinela) -> a passada produz um parágrafo.
    ctx.llama = FakeLlama(["Arquitetura ", "é ", "sobre ", "camadas."])
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = vs
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = FakeOptimizer("arquitetura software")   # termos fixos
    mem = SessionMemory(settings)
    # RAM relevante ao tema: casa as keywords de "arquitetura software".
    mem.conhecimento_sessao = deque([("arquitetura software", "SOLID, camadas, baixo acoplamento")])
    return agent, mem


async def test_early_stop_pula_banco_quando_ram_responde(monkeypatch, tmp_path):
    monkeypatch.setattr("mente_digital.agent.settings.early_stop_cascata", True)
    vs = SpyVS()
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert vs.search_calls == 0            # Banco NÃO foi consultado (RAM já respondeu)
    assert agent.ctx.web.searches == 0     # web tampouco


async def test_fusao_completa_quando_desligado(monkeypatch, tmp_path):
    monkeypatch.setattr("mente_digital.agent.settings.early_stop_cascata", False)
    vs = SpyVS()
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert vs.search_calls == 1            # sem early-stop, o Banco roda (fusão)


async def test_sem_ram_o_banco_roda_normalmente(monkeypatch, tmp_path):
    # Early-stop LIGADO, mas a RAM não respondeu -> o Banco tem que rodar (não pula à toa).
    monkeypatch.setattr("mente_digital.agent.settings.early_stop_cascata", True)
    vs = SpyVS()
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    mem.conhecimento_sessao = deque()      # sem RAM relevante
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert vs.search_calls == 1            # cascata seguiu para o Banco
