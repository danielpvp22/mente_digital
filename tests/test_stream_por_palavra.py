"""COMO O TEXTO CHEGA À TELA (2026-07-29, pedido do dono).

Turno FALADO: frases fechadas, para a tela não correr na frente da voz.
Turno DIGITADO: cada pedaço na hora, para quem lê ver a resposta nascendo.

O `_SegurarFraseIncompleta` continua rodando nos dois modos — deixou de ser o canal
de exibição e virou o detector de FIM DE FRASE, que é do que a legenda da figura
precisa (fragmento nunca alcança as 2 palavras em comum).
"""
import pytest

from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.respostas import _FigurasInline
from mente_digital.state import AppContext, turno_falado
from mente_digital.textutils import palavras_chave

from conftest import FakeLlama, FakeTts, make_send, textos_de_tokens

TOKENS = ["O solo ", "argiloso retém água. ", "Outra frase aqui."]
EMBED = "![[Figuras/livro/livro_p0003_f1.webp]]"
CHAVES = palavras_chave("Solo argiloso pesado retém água e restringe as raízes")


def _agent(tokens=TOKENS):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(tokens)
    ctx.tts = FakeTts()
    return Agent(ctx)


async def _rodar(falado, figuras=None, tokens=TOKENS):
    agent = _agent(tokens)
    send, enviados = make_send()
    marca = turno_falado.set(falado)
    try:
        await agent._responder_contexto("ctx", "pergunta", send, figuras=figuras)
    finally:
        turno_falado.reset(marca)
    return enviados


def _envios_de_texto(enviados):
    return [e for e in enviados if e.get("tipo") == "token"]


@pytest.mark.asyncio
async def test_digitado_manda_cada_pedaco_na_hora():
    """Três pedaços do modelo -> três envios. É o 'aparecendo enquanto criado'."""
    enviados = await _rodar(falado=False)

    assert len(_envios_de_texto(enviados)) == 3


@pytest.mark.asyncio
async def test_falado_continua_saindo_em_FRASES():
    """Regressão do live: com voz, a tela acompanha a fala. Os dois primeiros pedaços
    formam UMA frase e saem juntos; o terceiro sai no flush."""
    enviados = await _rodar(falado=True)

    assert len(_envios_de_texto(enviados)) == 2


@pytest.mark.asyncio
async def test_o_TEXTO_final_e_identico_nos_dois_modos():
    """O que muda é o RITMO da entrega, não o conteúdo — e nada pode sair duplicado."""
    digitado = textos_de_tokens(await _rodar(falado=False))
    falado = textos_de_tokens(await _rodar(falado=True))

    assert digitado == falado == "O solo argiloso retém água. Outra frase aqui."


@pytest.mark.asyncio
async def test_figura_ainda_casa_a_frase_no_modo_por_palavra():
    """O risco da mudança: emitindo fragmento, a legenda nunca teria 2 palavras em
    comum. Por isso o detector de frase continua rodando por baixo."""
    fila = _FigurasInline([(EMBED, CHAVES)])

    enviados = await _rodar(falado=False, figuras=fila)

    saida = textos_de_tokens(enviados)
    assert EMBED in saida
    assert saida.index(EMBED) < saida.index("Outra")   # no meio, não no fim
    assert not fila


@pytest.mark.asyncio
async def test_flag_desligada_volta_ao_modo_por_frase(monkeypatch):
    monkeypatch.setattr(settings, "texto_stream_por_palavra", False)

    enviados = await _rodar(falado=False)

    assert len(_envios_de_texto(enviados)) == 2


@pytest.mark.asyncio
async def test_sentinela_nao_vaza_nem_por_palavra():
    """O guard anti-alucinação é ANTERIOR à emissão: nada sai enquanto a resposta
    ainda pode virar 'não tenho informações suficientes'. Streaming por palavra não
    pode furar isso — seria o sentinela aparecendo letra a letra na tela."""
    tokens = ["Não ", "tenho ", "informações ", "suficientes"]

    enviados = await _rodar(falado=False, tokens=tokens)

    assert textos_de_tokens(enviados) == ""


# --- o caminho da WEB (_responder_stream) ------------------------------------
@pytest.mark.asyncio
async def test_resposta_da_web_tambem_streama_por_palavra():
    """A resposta da web chega pela MESMA tela — a regra não pode valer só no local."""
    agent = _agent()
    send, enviados = make_send()
    marca = turno_falado.set(False)
    try:
        await agent._responder_stream("prompt", send)
    finally:
        turno_falado.reset(marca)

    assert len(_envios_de_texto(enviados)) == 3
    assert textos_de_tokens(enviados) == "O solo argiloso retém água. Outra frase aqui."


@pytest.mark.asyncio
async def test_resposta_da_web_falada_sai_em_frases():
    agent = _agent()
    send, enviados = make_send()
    marca = turno_falado.set(True)
    try:
        await agent._responder_stream("prompt", send)
    finally:
        turno_falado.reset(marca)

    assert len(_envios_de_texto(enviados)) == 2
