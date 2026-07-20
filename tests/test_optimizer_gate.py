"""
Gate léxico do QueryOptimizer (painel 2026-07, fase a): pergunta auto-contida não
paga a chamada LLM do extrator — limpar_query resolve; pergunta que referencia o
turno anterior ainda usa o LLM (pronome precisa do histórico).
"""
from __future__ import annotations

from collections import deque

from agent import QueryOptimizer, referencia_contexto
from config import settings


class _SpyCollect:
    def __init__(self, resposta: str = "kubernetes"):
        self.calls = 0
        self._resposta = resposta

    async def collect(self, prompt, **kw):
        self.calls += 1
        return self._resposta


def _historico():
    return deque([("o que é kubernetes?", "É um orquestrador de contêineres.")])


async def test_auto_contida_pula_o_llm(monkeypatch):
    monkeypatch.setattr(settings, "optimizer_gate", True)
    pergunta = "como funciona a fotossíntese nas plantas"
    assert referencia_contexto(pergunta) is False   # premissa do teste
    spy = _SpyCollect()
    opt = QueryOptimizer(spy)

    termos = await opt.optimize(pergunta, _historico())

    assert spy.calls == 0                            # nenhuma decodificação paga
    assert "fotossintese" in termos or "fotossíntese" in termos


async def test_referencia_ao_turno_anterior_usa_o_llm(monkeypatch):
    monkeypatch.setattr(settings, "optimizer_gate", True)
    pergunta = "me explica melhor isso que você falou"
    assert referencia_contexto(pergunta) is True    # premissa do teste
    spy = _SpyCollect()
    opt = QueryOptimizer(spy)

    await opt.optimize(pergunta, _historico())

    assert spy.calls == 1                            # pronome -> extrator LLM


async def test_gate_desligado_volta_ao_llm_sempre(monkeypatch):
    monkeypatch.setattr(settings, "optimizer_gate", False)
    spy = _SpyCollect()
    opt = QueryOptimizer(spy)

    await opt.optimize("como funciona a fotossíntese nas plantas", _historico())

    assert spy.calls == 1                            # comportamento histórico


async def test_sem_historico_gate_ainda_vale(monkeypatch):
    monkeypatch.setattr(settings, "optimizer_gate", True)
    spy = _SpyCollect()
    opt = QueryOptimizer(spy)

    termos = await opt.optimize("qual a capital da austrália mesmo", deque())

    assert spy.calls == 0
    assert termos                                    # limpar_query devolveu algo