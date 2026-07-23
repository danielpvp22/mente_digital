"""
Filler CONTÍNUO da web (_responder_web).

O que este arquivo protege: o deep-fetch leva 3-12s, e o filler antigo falava UMA
ponte e esperava — deixando silêncio. Agora o _responder_web fala a 1ª ponte na hora
e, enquanto a busca não volta, emite pontes curtas ADICIONAIS a cada intervalo, até a
busca terminar ou bater o teto (filler_max_pontes). Assim o filler dura ~o tempo real
do fetch, não um valor fixo — e nunca fala ponte inútil quando a busca foi rápida.

Sem GPU/rede: LLM, TTS e web são fakes; o _falar_status é interceptado para CONTAR as
pontes faladas (isolado do áudio da resposta em si).
"""
from mente_digital import prompts
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory

import asyncio

from conftest import FakeLlama, FakeTts, make_send


class WebLenta:
    """WebSearcher falso cuja busca demora `atraso` segundos (controla a duração do
    fetch para exercitar o loop de filler). O prefetch é no-op."""

    def __init__(self, atraso: float) -> None:
        self.atraso = atraso
        self.prefetches = 0
        self.ultimos_dominios: list = []

    async def search(self, termo, consulta=None):
        await asyncio.sleep(self.atraso)
        return "- FONTE WEB: a resposta chegou."

    async def prefetch(self, tema):
        self.prefetches += 1
        return None


def _agent(atraso: float):
    ctx = AppContext(settings=settings)
    # Resposta NÃO-sentinela (o guard anti-sentinela libera e streama normalmente).
    ctx.llama = FakeLlama(["A ", "resposta ", "chegou."])
    ctx.tts = FakeTts()
    ctx.web = WebLenta(atraso)
    return Agent(ctx), SessionMemory(settings)


def _interceptar_pontes(agent):
    """Troca _falar_status por um coletor: devolve a lista que registra cada ponte."""
    pontes: list = []

    async def _rec(send, texto):
        pontes.append(texto)

    agent._falar_status = _rec   # instância vence o método do mixin
    return pontes


async def test_busca_rapida_fala_so_a_primeira_ponte():
    # Fetch instantâneo: a busca volta dentro do 1º intervalo -> nenhuma ponte extra.
    agent, mem = _agent(atraso=0.0)
    pontes = _interceptar_pontes(agent)
    send, _ = make_send()

    await agent._responder_web("dolar", "qual o dolar hoje?", send, mem)

    assert len(pontes) == 1   # só a 1ª ponte (o _msg_web), sem continuação inútil


async def test_busca_lenta_fala_pontes_ate_o_teto(monkeypatch):
    # Fetch bem mais longo que o intervalo -> o loop enche até filler_max_pontes.
    monkeypatch.setattr(settings, "filler_intervalo_s", 0.02)
    monkeypatch.setattr(settings, "filler_max_pontes", 2)
    agent, mem = _agent(atraso=0.5)   # 0.5s >> 2 * 0.02s: o teto (não o tempo) corta
    pontes = _interceptar_pontes(agent)
    send, _ = make_send()

    await agent._responder_web("dolar", "qual o dolar hoje?", send, mem)

    # 1ª ponte (dinâmica, nomeia a query) + exatamente filler_max_pontes de continuação.
    assert len(pontes) == 1 + 2
    assert pontes[1] == prompts.ponte_continuacao(0)
    assert pontes[2] == prompts.ponte_continuacao(1)


async def test_filler_max_pontes_zero_fala_so_a_primeira(monkeypatch):
    # Botão em 0: volta ao comportamento de UMA ponte só (sem continuação).
    monkeypatch.setattr(settings, "filler_intervalo_s", 0.02)
    monkeypatch.setattr(settings, "filler_max_pontes", 0)
    agent, mem = _agent(atraso=0.2)
    pontes = _interceptar_pontes(agent)
    send, _ = make_send()

    await agent._responder_web("dolar", "qual o dolar hoje?", send, mem)

    assert len(pontes) == 1
