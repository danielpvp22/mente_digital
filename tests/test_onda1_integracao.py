"""
Testes de INTEGRAÇÃO/fiação das features da Onda 1 — o ponto onde bugs se escondem:
- #7: o nível de verbosidade REALMENTE chega ao LLM (max_tokens + instrução no system).
- #23: a síntese REALMENTE quebra em vários lotes (map) e faz UM reduce.
- #1: edge-cases do cache de voz (texto vazio, voz ausente).
- #5: nova_conversa reseta o modo confidencial.

Sem GPU/rede: um "SpyLlama" captura os kwargs de cada chamada.
"""
from agent import Agent, LatencyTracker
from audio import TtsService
from config import settings
from state import AppContext, SessionMemory

from conftest import FakeTts, make_send


class SpyLlama:
    """LLM falso que REGISTRA os kwargs de cada stream/collect (para inspecionar a fiação)."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.stream_calls = []
        self.collect_calls = []

    async def stream(self, prompt, **kw):
        self.stream_calls.append(kw)
        for t in self.tokens:
            yield t

    async def collect(self, prompt, **kw):
        self.collect_calls.append(kw)
        return "resumo parcial do tema"


class FakeVSComAtomos:
    def __init__(self, atomos):
        self._atomos = atomos

    async def buscar_conteudos(self, query, k):
        return list(self._atomos)

    async def sync(self):
        pass


# ---------------------------------------------------------------------------
# #7 — o nível de verbosidade chega ao LLM
# ---------------------------------------------------------------------------
async def test_responder_contexto_aplica_verbosidade():
    ctx = AppContext(settings=settings)
    ctx.llama = SpyLlama(["Resposta ", "curta."])   # não é o sentinela -> libera
    ctx.tts = FakeTts()
    agent = Agent(ctx)
    send, _ = make_send()

    r = await agent._responder_contexto(
        "contexto", "pergunta", send, max_tokens=90, instrucao_extra="SEJA BREVE"
    )
    assert r == "Resposta curta."
    kw = ctx.llama.stream_calls[0]
    assert kw["max_tokens"] == 90
    assert "SEJA BREVE" in kw["system_prompt"]


async def test_responder_contexto_sem_nivel_usa_padrao():
    ctx = AppContext(settings=settings)
    ctx.llama = SpyLlama(["Resposta ", "normal."])
    ctx.tts = FakeTts()
    agent = Agent(ctx)
    send, _ = make_send()

    await agent._responder_contexto("ctx", "pergunta", send)
    kw = ctx.llama.stream_calls[0]
    assert kw["max_tokens"] == settings.max_tokens_resposta   # padrão quando não há nível


# ---------------------------------------------------------------------------
# #23 — map-reduce quebra em vários lotes
# ---------------------------------------------------------------------------
async def test_sintese_quebra_em_varios_lotes(monkeypatch):
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    # 6 átomos grandes contra um orçamento de 6000 chars -> 3 lotes (2 átomos cada).
    atomos = ["X" * 3000 for _ in range(6)]
    ctx = AppContext(settings=settings)
    ctx.llama = SpyLlama(["Síntese ", "final."])
    ctx.tts = FakeTts()
    ctx.vectorstore = FakeVSComAtomos(atomos)
    agent = Agent(ctx)
    mem = SessionMemory(settings)
    send, enviados = make_send()

    await agent._sintese_sob_demanda("tema grande", send, mem, LatencyTracker())

    assert len(ctx.llama.collect_calls) == 3   # 3 lotes (map) -> 3 collects
    assert len(ctx.llama.stream_calls) == 1    # 1 reduce (stream)
    assert mem.chat_history                    # registrou o turno


async def test_sintese_um_lote_faz_um_map(monkeypatch):
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    ctx = AppContext(settings=settings)
    ctx.llama = SpyLlama(["ok."])
    ctx.tts = FakeTts()
    ctx.vectorstore = FakeVSComAtomos(["átomo curto sobre o tema"])
    agent = Agent(ctx)
    send, _ = make_send()

    await agent._sintese_sob_demanda("tema", send, SessionMemory(settings), LatencyTracker())
    assert len(ctx.llama.collect_calls) == 1   # 1 lote -> 1 map
    assert len(ctx.llama.stream_calls) == 1    # reduce mesmo com 1 lote (limpa a fala)


# ---------------------------------------------------------------------------
# #1 — edge-cases do cache de voz
# ---------------------------------------------------------------------------
async def test_tts_texto_vazio_nao_cacheia():
    tts = TtsService()
    tts._voice = object()   # existe, mas não deve ser chamado
    assert await tts.synth_base64("   ") is None
    assert len(tts._cache) == 0


async def test_tts_sem_voz_retorna_none():
    tts = TtsService()   # _voice = None
    assert await tts.synth_base64("qualquer coisa") is None


# ---------------------------------------------------------------------------
# #5 — nova conversa reseta o modo confidencial
# ---------------------------------------------------------------------------
def test_nova_conversa_reseta_confidencial():
    mem = SessionMemory(settings)
    mem.confidencial = True
    mem.nova_conversa("conversa-nova")
    assert mem.confidencial is False
