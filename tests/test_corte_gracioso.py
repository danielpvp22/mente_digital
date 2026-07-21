"""
Corte gracioso do teto de tokens (teste real 2026-07-21).

Quando o decode bate no max_tokens, a cauda é meia-frase — antes ela ia ESCRITA
pro chat e FALADA no flush do chunker ("...sendo recomendados em"). Agora o texto
é emitido em fronteiras de frase e a cauda truncada é retida em tela, voz e
histórico. Sem teto atingido, nada muda: a cauda sai inteira no fim.
"""
from agent import Agent
from config import settings
from state import AppContext

from conftest import FakeLlama, FakeTts, make_send, textos_de_tokens


def _agent_com_tokens(tokens):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(tokens)
    ctx.tts = FakeTts()
    return Agent(ctx)


async def test_teto_apara_frase_incompleta():
    # 2 tokens com teto 2 = decode TRUNCADO; a 2ª "frase" não fecha.
    tokens = ["Pneus balão amortecem impactos. ", "sendo recomendados em"]
    agent = _agent_com_tokens(tokens)
    send, enviados = make_send()

    resultado = await agent._responder_contexto("ctx", "pergunta", send, max_tokens=2)

    assert resultado == "Pneus balão amortecem impactos."
    assert "recomendados" not in textos_de_tokens(enviados)
    assert all("recomendados" not in fala for fala in agent.ctx.tts.chamadas)


async def test_sem_teto_a_cauda_sai_normal():
    tokens = ["Uma frase. ", "Outra sem ponto final"]
    agent = _agent_com_tokens(tokens)
    send, enviados = make_send()

    resultado = await agent._responder_contexto("ctx", "pergunta", send)

    assert resultado == "Uma frase. Outra sem ponto final"
    assert textos_de_tokens(enviados) == "Uma frase. Outra sem ponto final"
