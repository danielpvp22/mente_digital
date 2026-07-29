"""TURNO DIGITADO NÃO FALA (2026-07-29).

O `_falar` sintetiza DENTRO do laço de tokens (`await synth_base64`), então o TTS
está no caminho crítico: num turno digitado o texto parava na tela enquanto o XTTS
gerava áudio que ninguém ia ouvir. Medido em 19 turnos reais de teste por texto:
`tts_total` em ~95% do relógio (36,7 s totais com 35,2 s de síntese).

O portão é um ContextVar porque a fala converge para dois primitivos mas é acionada
de 40+ lugares, e o `Agent` é um singleton compartilhado por todas as sessões.
"""
import pytest

from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory, turno_falado

from conftest import FakeLlama, FakeTts, make_send


def _agent():
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama([])
    ctx.tts = FakeTts()
    return Agent(ctx)


# --- o portão ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_turno_mudo_nao_sintetiza():
    agent = _agent()
    send, _ = make_send()
    token = turno_falado.set(False)
    try:
        await agent._falar(send, ["Uma frase qualquer."])
    finally:
        turno_falado.reset(token)

    assert agent.ctx.tts.chamadas == []


@pytest.mark.asyncio
async def test_turno_falado_sintetiza():
    agent = _agent()
    send, _ = make_send()
    token = turno_falado.set(True)
    try:
        await agent._falar(send, ["Uma frase qualquer."])
    finally:
        turno_falado.reset(token)

    assert agent.ctx.tts.chamadas == ["Uma frase qualquer."]


@pytest.mark.asyncio
async def test_fora_de_turno_o_default_e_FALAR():
    """Push proativo do scheduler (alarme, briefing) roda fora de qualquer turno —
    silenciá-lo por default transformaria o alarme numa notificação muda."""
    agent = _agent()
    send, _ = make_send()

    await agent._falar(send, ["Seu lembrete das oito."])

    assert agent.ctx.tts.chamadas == ["Seu lembrete das oito."]


@pytest.mark.asyncio
async def test_filler_da_web_mostra_o_texto_mesmo_mudo():
    """O filler AVISA que a busca começou. Em turno digitado ele se lê; se o portão
    engolisse o texto junto com o áudio, a espera da web ficaria sem explicação."""
    agent = _agent()
    send, enviados = make_send()
    token = turno_falado.set(False)
    try:
        await agent._falar_status(send, "Procurando na web.")
    finally:
        turno_falado.reset(token)

    assert agent.ctx.tts.chamadas == []
    assert any(e.get("tipo") == "token" for e in enviados)
    assert not any(e.get("tipo") == "audio" for e in enviados)


# --- quem decide o valor -----------------------------------------------------
async def _falado_no_pipeline(monkeypatch, stt_ms, digitado_fala=False):
    agent = _agent()
    monkeypatch.setattr(settings, "falar_turno_digitado", digitado_fala)
    visto = {}

    async def _capturar(*args, **kwargs):
        visto["falado"] = turno_falado.get()

    monkeypatch.setattr(agent, "_pipeline", _capturar)
    send, _ = make_send()
    await agent.pipeline_resposta("uma pergunta", send, SessionMemory(settings),
                                  stt_ms=stt_ms)
    return visto["falado"]


@pytest.mark.asyncio
async def test_voz_fala_e_digitado_nao(monkeypatch):
    """`stt_ms` só é preenchido quando o turno veio de TRANSCRIÇÃO (ws._check_silence)
    — é o sinal que já existia e que ninguém tinha ligado à síntese."""
    assert await _falado_no_pipeline(monkeypatch, stt_ms=120) is True
    assert await _falado_no_pipeline(monkeypatch, stt_ms=None) is False


@pytest.mark.asyncio
async def test_flag_devolve_a_voz_ao_turno_digitado(monkeypatch):
    """Botão de volta: quem quiser ouvir enquanto digita não perde a funcionalidade."""
    assert await _falado_no_pipeline(
        monkeypatch, stt_ms=None, digitado_fala=True) is True
