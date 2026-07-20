"""
"Mestre, fonte?" (painel 2026-07): proveniência falada da última resposta — detecção
pura no mestre.py, fontes acumuladas na SessionMemory (só RAM), fala por TEMPLATE.
"""
from __future__ import annotations

import mestre
from agent import Agent
from config import settings
from state import AppContext, SessionMemory

from conftest import FakeTts, make_send


# -- detecção (pura) -----------------------------------------------------------
def test_comando_fonte_detecta_variacoes():
    for c in ["fonte?", "Fonte", "qual a fonte", "de onde tirou isso",
              "de onde você tirou isso?", "quais as fontes", "cita a fonte"]:
        assert mestre.comando_fonte(c) is True, c


def test_pergunta_de_conhecimento_nao_e_comando_fonte():
    # Frase EXATA, não substring: isto é pergunta, vai pro pipeline normal.
    for c in ["qual a fonte de energia do esp32", "fontes de proteína vegetal",
              "o que é fonte chaveada", ""]:
        assert mestre.comando_fonte(c) is False, c


# -- ciclo de vida na sessão ---------------------------------------------------
def test_fontes_nao_atravessam_conversas():
    mem = SessionMemory(settings)
    mem.ultimas_fontes = ["nota:Cerebro/Equihash.md", "web:exemplo.com"]
    mem.nova_conversa("c2")
    assert mem.ultimas_fontes == []
    mem.ultimas_fontes = ["memoria"]
    mem.carregar_conversa("c3", [("q", "a")])
    assert mem.ultimas_fontes == []


# -- fala por template ---------------------------------------------------------
def _agent():
    ctx = AppContext(settings=settings)
    ctx.tts = FakeTts()
    return Agent(ctx)


async def test_falar_fontes_mistura_notas_e_web():
    agent = _agent()
    mem = SessionMemory(settings)
    mem.ultimas_fontes = [
        "memoria",
        "nota:D:/vault/Equihash_e_o_ASIC.md",
        "nota:D:/vault/Pools_de_mineracao.md",
        "web:pt.wikipedia.org",
    ]
    send, enviados = make_send()

    fala, rota = await agent._falar_fontes(send, mem)

    assert rota == "mestre:fonte"
    assert "memória fresca" in fala
    assert "Equihash e o ASIC" in fala          # stem legível, sem .md nem underscore
    assert "pt.wikipedia.org" in fala
    assert ".md" not in fala
    assert any(m.get("tipo") == "token" for m in enviados)   # foi emitida pra tela


async def test_falar_fontes_sem_registro():
    agent = _agent()
    mem = SessionMemory(settings)
    send, _ = make_send()

    fala, _ = await agent._falar_fontes(send, mem)

    assert "Não tenho registro" in fala


async def test_falar_fontes_web_generica():
    # Resposta que veio de cache/snippets: sem domínio rastreado -> "da web".
    agent = _agent()
    mem = SessionMemory(settings)
    mem.ultimas_fontes = ["web:"]
    send, _ = make_send()

    fala, _ = await agent._falar_fontes(send, mem)

    assert "da web" in fala
