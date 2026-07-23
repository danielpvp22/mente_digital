"""
Meia-duplex anti-eco + comando de parada (correção da contaminação da sessão live).

Enquanto a resposta TOCA, o que o mic capta NÃO abre turno novo (mata o eco/ruído que
virava pergunta-fantasma "e aí"/"obrigado"/"buponte" intercalada); o ÚNICO efeito é o
comando "pare", que corta a fala por uma cadeia LEVE (regex, sem tocar no LLM/agente).
Fora da fala dela, o fluxo é o normal. Sem WebSocket/GPU — LiveSession com stubs.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

from mente_digital import ws as ws_mod
from mente_digital.config import settings
from mente_digital.state import AppContext
from mente_digital.ws import LiveSession


class _FakeWs:
    def __init__(self):
        self.enviados: list[dict] = []

    async def send_json(self, data):
        self.enviados.append(data)


class _FakeStt:
    def __init__(self, texto: str):
        self.texto = texto
        self.chamadas = 0

    async def transcribe(self, audio):
        self.chamadas += 1
        return self.texto


class _FakeAgent:
    def __init__(self):
        self.chamadas = 0

    async def pipeline_resposta(self, texto, send, mem, stt_ms=None, vad_ms=None):
        self.chamadas += 1


class _FakeTts:
    def __init__(self):
        self.cancelado = 0

    def cancel(self):
        self.cancelado += 1


class _TaskVivo:
    """Pipeline 'vivo' (done=False) e cancelável — stub do asyncio.Task."""
    def __init__(self):
        self._cancelado = False

    def done(self):
        return self._cancelado

    def cancel(self):
        self._cancelado = True


def _sessao(monkeypatch, texto: str):
    ctx = AppContext(settings=settings)
    ctx.llama = SimpleNamespace(ready=True)
    ctx.stt = _FakeStt(texto)
    ctx.agent = _FakeAgent()
    ctx.tts = _FakeTts()
    sess = LiveSession(ctx, _FakeWs())

    async def _dump_noop(*a, **k):
        return None

    monkeypatch.setattr(ws_mod, "append_chat_dump", _dump_noop)
    # Estado que faz o _check_silence encerrar a fala e transcrever agora.
    agora = time.time()
    sess.is_recording = True
    sess.audio_buffer = [np.zeros(160, dtype=np.float32)] * (settings.vad_min_frames + 5)
    sess._fala_inicio = agora - 2.0          # fala curta (~1s)
    sess.last_audio_time = agora - 1.0       # silêncio (1s) > janela curta (0,7) -> encerra
    return ctx, sess


async def test_eco_durante_a_fala_nao_abre_turno(monkeypatch):
    monkeypatch.setattr(settings, "parada_habilitada", True)
    ctx, sess = _sessao(monkeypatch, "E aí")     # fantasma que o mic captou do eco/ruído
    sess.pipeline_task = _TaskVivo()             # resposta em voo -> _tts_tocando True
    await sess._check_silence()
    assert ctx.agent.chamadas == 0               # NÃO virou pergunta (descartado)
    assert not sess.pipeline_task._cancelado     # não é parada -> não corta a fala em voo


async def test_pare_durante_a_fala_corta_por_caminho_leve(monkeypatch):
    monkeypatch.setattr(settings, "parada_habilitada", True)
    ctx, sess = _sessao(monkeypatch, "pare")
    tarefa = _TaskVivo()
    sess.pipeline_task = tarefa
    await sess._check_silence()
    assert ctx.agent.chamadas == 0                    # o LLM/agente NEM é chamado (cadeia leve)
    assert tarefa._cancelado                          # pipeline cortado
    assert ctx.tts.cancelado >= 1                     # síntese cortada
    assert {"tipo": "barge_in"} in sess.ws.enviados   # front instruído a esvaziar a fila


async def test_cauda_de_audio_recente_ainda_bloqueia(monkeypatch):
    # Pipeline já fechou, mas o áudio saiu há pouco: o front ainda toca a cauda -> a janela
    # de guarda mantém o anti-eco ligado (o "obrigado" fantasma não vira turno).
    monkeypatch.setattr(settings, "parada_habilitada", True)
    ctx, sess = _sessao(monkeypatch, "obrigado")
    sess.pipeline_task = None
    sess._ultimo_audio_ts = time.time()          # áudio saiu agora -> cauda tocando
    await sess._check_silence()
    assert ctx.agent.chamadas == 0


async def test_fora_da_fala_processa_normal(monkeypatch):
    monkeypatch.setattr(settings, "parada_habilitada", True)
    ctx, sess = _sessao(monkeypatch, "qual a capital da França")
    sess.pipeline_task = None
    sess._ultimo_audio_ts = 0.0                  # sem cauda de áudio -> não está tocando
    await sess._check_silence()
    assert sess.pipeline_task is not None
    await sess.pipeline_task                      # _start_pipeline agenda; deixa rodar
    assert ctx.agent.chamadas == 1               # pergunta real -> pipeline normal


async def test_desligado_processa_tudo_mesmo_durante_a_fala(monkeypatch):
    # Botão off: volta ao comportamento antigo (processa tudo, inclusive eco).
    monkeypatch.setattr(settings, "parada_habilitada", False)
    ctx, sess = _sessao(monkeypatch, "E aí")
    sess.pipeline_task = None                     # sem tarefa viva p/ não confundir a checagem
    sess._ultimo_audio_ts = time.time()          # mesmo com cauda, o gate está desligado
    await sess._check_silence()
    assert sess.pipeline_task is not None
    await sess.pipeline_task
    assert ctx.agent.chamadas == 1
