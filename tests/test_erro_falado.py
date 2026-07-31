"""UX-01 (painel 2026-07-24): erro no pipeline NUNCA vira dead-air.

Antes, uma exceção só ia pro log — o orbe do live ficava preso em "Processando…" e
o handler {"tipo":"erro"} do front era código morto. Trava-se o contrato novo:
exceção => um {"tipo":"erro"} + a frase-template FALADA (token + áudio, sem LLM).
"""
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send, textos_de_tokens


async def test_excecao_no_pipeline_emite_erro_e_frase_falada(monkeypatch):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["nunca ", "chega ", "aqui"])
    ctx.tts = FakeTts()
    agent = Agent(ctx)
    mem = SessionMemory(settings)
    send, enviados = make_send()

    async def _boom(*a, **k):
        raise RuntimeError("boom sintético no retrieval")

    # Explode DENTRO do try do pipeline (estágio de extração/recuperação).
    monkeypatch.setattr(agent, "_otimizar_e_recuperar", _boom)

    # turno de VOZ (stt_ms): este teste afere a FALA, e turno digitado não sintetiza.
    await agent.pipeline_resposta("qual a capital da França?", send, mem, stt_ms=1)

    tipos = [m.get("tipo") for m in enviados]
    assert "erro" in tipos                                   # o front reseta o orbe
    assert "erro aqui do meu lado" in textos_de_tokens(enviados)  # frase na tela
    # E o TTS foi acionado com a frase (o FakeTts da suíte devolve None de
    # propósito — o que se trava aqui é a CHAMADA, não o payload de áudio).
    assert any("erro aqui do meu lado" in c for c in ctx.tts.chamadas)


async def test_llm_que_morre_vira_erro_honesto_nao_nao_encontrei():
    """A falha do worker de inferência (InferenciaFalhou, 2026-07-31) tem de chegar
    AQUI, no handler de topo do turno.

    Antes ela virava stream VAZIA, que o pipeline lê como o sentinela de
    insuficiência: escalava para a web, pagava 3-12s de rede, falhava igual e o
    usuário ouvia "não achei uma resposta clara" — falha técnica disfarçada de
    ausência de conhecimento. Agora é erro honesto e o orbe destrava."""
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["pedaco ", "da ", "resposta"])
    ctx.tts = FakeTts()
    agent = Agent(ctx)
    mem = SessionMemory(settings)
    send, enviados = make_send()

    ctx.llama.armar_falha()      # o próximo decode morre, como o worker real morreu

    await agent.pipeline_resposta("qual a capital da França?", send, mem, stt_ms=1)

    falado = textos_de_tokens(enviados)
    assert "erro" in [m.get("tipo") for m in enviados]
    assert "erro aqui do meu lado" in falado
    assert "não achei" not in falado.lower(), "falha do LLM não pode virar 'não encontrei'"
