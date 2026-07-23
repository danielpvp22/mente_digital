"""
Filler CONTÍNUO da web (_responder_web).

O que este arquivo protege: o deep-fetch leva 3-12s, e o filler antigo falava UMA
ponte e esperava — deixando silêncio. Agora o _responder_web fala a 1ª ponte na hora
e, enquanto a busca não volta, emite pontes curtas ADICIONAIS a cada intervalo, até a
busca terminar ou bater o teto (filler_max_pontes). Assim o filler dura ~o tempo real
do fetch, não um valor fixo — e nunca fala ponte inútil quando a busca foi rápida.

CARÊNCIA (correção pós-race): desde o race-first-K a web voltou a ~3s (às vezes <1,5s).
A 1ª ponte falada NA HORA passou a atropelar a resposta em web rápida. Agora, ANTES de
qualquer ponte, o _responder_web dá `filler_carencia_s` de silêncio à busca; se ela
terminar na janela, PULA o filler inteiro. Os testes de LOOP abaixo zeram a carência
(monkeypatch) para exercitar o loop isolado; os testes de CARÊNCIA a exercitam direto.

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

    async def search(self, termo, consulta=None, on_colheita=None):
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


# --- Loop do filler (carência DESLIGADA para isolar o loop) --------------------

async def test_busca_rapida_fala_so_a_primeira_ponte(monkeypatch):
    # Carência off + fetch instantâneo: a 1ª ponte sai (comportamento antigo), mas a
    # busca volta no 1º intervalo -> nenhuma ponte extra.
    monkeypatch.setattr(settings, "filler_carencia_s", 0)
    agent, mem = _agent(atraso=0.0)
    pontes = _interceptar_pontes(agent)
    send, _ = make_send()

    await agent._responder_web("dolar", "qual o dolar hoje?", send, mem)

    assert len(pontes) == 1   # só a 1ª ponte (o _msg_web), sem continuação inútil


async def test_busca_lenta_fala_pontes_ate_o_teto(monkeypatch):
    # Fetch bem mais longo que o intervalo -> o loop enche até filler_max_pontes.
    monkeypatch.setattr(settings, "filler_carencia_s", 0)
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
    monkeypatch.setattr(settings, "filler_carencia_s", 0)
    monkeypatch.setattr(settings, "filler_intervalo_s", 0.02)
    monkeypatch.setattr(settings, "filler_max_pontes", 0)
    agent, mem = _agent(atraso=0.2)
    pontes = _interceptar_pontes(agent)
    send, _ = make_send()

    await agent._responder_web("dolar", "qual o dolar hoje?", send, mem)

    assert len(pontes) == 1


# --- Carência (o head-start de silêncio dado à busca) --------------------------

async def test_carencia_pula_filler_quando_web_e_rapida(monkeypatch):
    # A busca volta DENTRO da carência -> não fala ponte nenhuma (nem a 1ª). É a correção:
    # web rápida não é mais atropelada por "vou buscar..." em cima da resposta.
    monkeypatch.setattr(settings, "filler_carencia_s", 0.3)
    agent, mem = _agent(atraso=0.0)   # instantânea: termina bem dentro da carência
    pontes = _interceptar_pontes(agent)
    send, _ = make_send()

    await agent._responder_web("dolar", "qual o dolar hoje?", send, mem)

    assert len(pontes) == 0


async def test_carencia_nao_suprime_filler_quando_web_e_lenta(monkeypatch):
    # A busca ESTOURA a carência -> o filler ainda cobre a espera (1ª ponte + continuação).
    monkeypatch.setattr(settings, "filler_carencia_s", 0.02)
    monkeypatch.setattr(settings, "filler_intervalo_s", 0.02)
    monkeypatch.setattr(settings, "filler_max_pontes", 1)
    agent, mem = _agent(atraso=0.3)   # 0.3s >> carência de 0.02s
    pontes = _interceptar_pontes(agent)
    send, _ = make_send()

    await agent._responder_web("dolar", "qual o dolar hoje?", send, mem)

    assert len(pontes) == 1 + 1   # 1ª ponte + 1 continuação (a carência não a engoliu)
