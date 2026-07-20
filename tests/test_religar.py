"""
Religar o LLM no primeiro frame de fala (painel 2026-07): a transição silêncio→fala
com o modelo descarregado dispara ensure_loaded() em background — o reload corre em
paralelo com a fala + VAD + STT em vez de ser pago serialmente no TTFT. Sem GPU:
LiveSession com WS/ctx falsos, no padrão do test_wake_mestre.
"""
from __future__ import annotations

import asyncio

import numpy as np

from config import settings
from state import AppContext


class _WsFake:
    def __init__(self):
        self.enviados = []

    async def accept(self):
        ...

    async def send_json(self, data):
        self.enviados.append(data)


class _LlamaFrio:
    """Modelo descarregado: conta os PEDIDOS de religamento (a criação do coro)."""

    def __init__(self, ready: bool = False):
        self.ready = ready
        self.pedidos = 0

    def ensure_loaded(self):
        self.pedidos += 1

        async def _noop():
            ...

        return _noop()

    def preempt(self):
        ...


class _AgentNoop:
    async def pipeline_resposta(self, *a, **k):
        ...


def _sessao(monkeypatch, ready=False, wake=False):
    from ws import LiveSession

    monkeypatch.setattr(settings, "mestre_wake", wake)
    ctx = AppContext(settings=settings)
    ctx.llama = _LlamaFrio(ready=ready)
    ctx.agent = _AgentNoop()
    s = LiveSession(ctx, _WsFake())
    # Sem event loop de servidor no teste: track_task só fecha o coro.
    monkeypatch.setattr(ctx, "track_task", lambda coro: getattr(coro, "close", lambda: None)())
    return s


def _frame_alto() -> bytes:
    # PCM int16 bem acima do vad_rms_threshold.
    return (np.ones(1600, dtype=np.int16) * 8000).tobytes()


def test_transicao_para_fala_religa_uma_vez(monkeypatch):
    s = _sessao(monkeypatch, ready=False)
    s._on_audio(_frame_alto())      # silêncio -> fala: religa
    s._on_audio(_frame_alto())      # já gravando: NÃO religa de novo
    s._on_audio(_frame_alto())
    assert s.ctx.llama.pedidos == 1


def test_modelo_pronto_nao_religa(monkeypatch):
    s = _sessao(monkeypatch, ready=True)
    s._on_audio(_frame_alto())
    assert s.ctx.llama.pedidos == 0


def test_dormente_nao_religa_com_voz_de_fundo(monkeypatch):
    # F3 ligado: sessão dormente ignora fala comum — ruído/TV não deve reancorar VRAM.
    s = _sessao(monkeypatch, ready=False, wake=True)
    assert s.dormente is True
    s._on_audio(_frame_alto())
    assert s.ctx.llama.pedidos == 0


async def test_turno_digitado_religa(monkeypatch, tmp_path):
    s = _sessao(monkeypatch, ready=False)
    monkeypatch.setattr("agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    await s._on_text('{"tipo": "texto", "payload": "oi, tudo bem por aí"}')
    assert s.ctx.llama.pedidos == 1
