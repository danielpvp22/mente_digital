"""
Rede de segurança anti-alucinação (agent._responder_contexto).

Enquanto os tokens iniciais forem prefixo do sentinela "Não tenho informações
suficientes", o áudio/texto fica RETIDO. Se o sentinela se confirmar, devolve None
(o pipeline escala pra web) e NADA é falado. Se divergir, libera o buffer e segue
em streaming. Este é o ponto mais fácil de quebrar numa refatoração — daí o teste.

Exercitado com um LLM falso (tokens controlados) e um TTS no-op: sem GPU, sem rede.
"""
from agent import Agent
from config import settings
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send, textos_de_tokens


def _agent_com_tokens(tokens):
    ctx = AppContext(settings=settings, memory=SessionMemory(settings))
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
