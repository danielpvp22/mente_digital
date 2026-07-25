"""
Rede de segurança anti-alucinação (agent._responder_contexto).

Enquanto os tokens iniciais forem prefixo do sentinela "Não tenho informações
suficientes", o áudio/texto fica RETIDO. Se o sentinela se confirmar, devolve None
(o pipeline escala pra web) e NADA é falado. Se divergir, libera o buffer e segue
em streaming. Este é o ponto mais fácil de quebrar numa refatoração — daí o teste.

Exercitado com um LLM falso (tokens controlados) e um TTS no-op: sem GPU, sem rede.
"""
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext

from conftest import FakeLlama, FakeTts, make_send, textos_de_tokens


def _agent_com_tokens(tokens):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(tokens)
    ctx.tts = FakeTts()
    return Agent(ctx)


async def test_sentinela_confirmado_nao_fala_e_retorna_none():
    tokens = ["Não ", "tenho ", "informações ", "suficientes"]
    agent = _agent_com_tokens(tokens)
    send, enviados = make_send()

    resultado = await agent._responder_contexto("ctx", "pergunta", send)

    assert resultado is None                    # escala pra web
    assert textos_de_tokens(enviados) == ""     # sentinela nunca é "falado"
    assert agent.ctx.tts.chamadas == []         # nenhum áudio sintetizado


async def test_resposta_real_e_transmitida_em_streaming():
    tokens = ["Python ", "é ", "uma ", "linguagem."]
    agent = _agent_com_tokens(tokens)
    send, enviados = make_send()

    resultado = await agent._responder_contexto("ctx", "pergunta", send)

    assert resultado == "Python é uma linguagem."
    assert textos_de_tokens(enviados) == "Python é uma linguagem."


async def test_prefixo_do_sentinela_que_diverge_libera_o_buffer():
    # começa igual ao sentinela ("Não tenho...") mas diverge em "dúvida"
    tokens = ["Não ", "tenho ", "dúvida ", "de ", "que ", "Python ", "é ", "ótimo."]
    agent = _agent_com_tokens(tokens)
    send, enviados = make_send()

    resultado = await agent._responder_contexto("ctx", "pergunta", send)

    assert resultado is not None                          # NÃO é o sentinela
    assert resultado == "Não tenho dúvida de que Python é ótimo."
    # o buffer retido ("Não tenho dúvida ") é liberado, não perdido
    assert textos_de_tokens(enviados).startswith("Não tenho dúvida")


# -- FUZZY (teste real 2026-07-21): o 2507 parafraseia o sentinela --------------
from mente_digital.prompts import abre_como_sentinela, parece_sentinela  # noqa: E402


def test_parece_sentinela_pega_parafrases():
    for t in [
        "nao tenho informacoes suficientes",
        "nao ha atomos que confirmem isso",
        "nao encontrei dados suficientes sobre o tema",
        "nao tenho essa informacao registrada",
        "tensorrt acelera yolo, mas nao ha atomos que confirmem",
    ]:
        assert parece_sentinela(t), t


def test_parece_sentinela_ignora_resposta_real():
    for t in [
        "nao tenho duvida de que python e otimo",
        "o ceu e azul pela dispersao rayleigh",
        "nao ha problema em usar fp16",
        "nao. o correto e usar cosseno",
    ]:
        assert not parece_sentinela(t), t


def test_abertura_inocente_nao_e_retida():
    assert not abre_como_sentinela("python e uma linguagem")


async def test_parafrase_do_sentinela_escala_sem_falar():
    tokens = ["Não ", "há ", "átomos ", "que ", "confirmem ", "isso."]
    agent = _agent_com_tokens(tokens)
    send, enviados = make_send()

    resultado = await agent._responder_contexto("ctx", "pergunta", send)

    assert resultado is None                    # variante do sentinela -> escala
    assert textos_de_tokens(enviados) == ""
    assert agent.ctx.tts.chamadas == []


async def test_nao_seco_e_resposta_real_nao_engolida():
    # "Não." abre igual ao sentinela e o stream acaba — é resposta legítima, libera.
    tokens = ["Não."]
    agent = _agent_com_tokens(tokens)
    send, enviados = make_send()

    resultado = await agent._responder_contexto("ctx", "pergunta", send)

    assert resultado == "Não."
    assert textos_de_tokens(enviados) == "Não."
