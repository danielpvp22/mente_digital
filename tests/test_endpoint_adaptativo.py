"""
Endpointing adaptativo do VAD (consultoria TTFT #3).

Fala curta encerra com a janela curta (o 1,2s fixo era pago por TODO turno de voz);
fala longa mantém a margem cheia. O detector de corte-precoce loga a retomada logo
após um endpoint curto — é o número que calibra o default. Sem WebSocket real: a
LiveSession recebe stubs e o relógio é manipulado pelos campos de estado.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

import ws as ws_mod
from config import settings
from state import AppContext
from ws import LiveSession, janela_endpoint


# ---------- a função pura ----------

def test_fala_curta_usa_janela_curta(monkeypatch):
    monkeypatch.setattr(settings, "vad_adaptativo", True)
    assert janela_endpoint(1.0) == settings.vad_silence_curta_seconds


def test_fala_longa_mantem_margem_cheia(monkeypatch):
    monkeypatch.setattr(settings, "vad_adaptativo", True)
    assert janela_endpoint(settings.vad_fala_curta_seconds + 0.1) == settings.vad_silence_seconds


def test_desligado_volta_ao_teto_fixo(monkeypatch):
    monkeypatch.setattr(settings, "vad_adaptativo", False)
    assert janela_endpoint(0.5) == settings.vad_silence_seconds


# ---------- a fiação na LiveSession ----------

class _FakeWs:
    async def send_json(self, data):
        return None


class _FakeStt:
    def __init__(self):
        self.chamadas = 0

    async def transcribe(self, audio):
        self.chamadas += 1
        return "uma pergunta qualquer de teste"


class _FakeAgent:
    def __init__(self):
        self.recebido = None

    async def pipeline_resposta(self, texto, send, mem, stt_ms=None, vad_ms=None):
        self.recebido = {"texto": texto, "stt_ms": stt_ms, "vad_ms": vad_ms}


def _sessao(monkeypatch):
    ctx = AppContext(settings=settings)
    ctx.llama = SimpleNamespace(ready=True)
    ctx.stt = _FakeStt()
    ctx.agent = _FakeAgent()
    sess = LiveSession(ctx, _FakeWs())

    async def _dump_noop(*a, **k):
        return None

    # o dump escreve arquivo real (chat_dump) — fora do escopo deste teste
    monkeypatch.setattr(ws_mod, "append_chat_dump", _dump_noop)
    return ctx, sess


async def test_fala_curta_dispara_no_silencio_curto_e_reporta_vad_ms(monkeypatch):
    monkeypatch.setattr(settings, "vad_adaptativo", True)
    ctx, sess = _sessao(monkeypatch)
    agora = time.time()
    sess.is_recording = True
    sess.audio_buffer = [np.zeros(160, dtype=np.float32)] * (settings.vad_min_frames + 5)
    sess._fala_inicio = agora - 1.0          # fala de ~0,1s (curta)
    sess.last_audio_time = agora - 0.9       # 0,9s de silêncio > janela curta (0,7)

    await sess._check_silence()
    assert sess.pipeline_task is not None
    await sess.pipeline_task                 # _start_pipeline agenda; o teste aguarda

    assert ctx.stt.chamadas == 1
    # o waterfall recebe a janela que foi de fato PAGA neste turno
    assert ctx.agent.recebido["vad_ms"] == round(settings.vad_silence_curta_seconds * 1000)


async def test_fala_longa_nao_dispara_antes_do_teto(monkeypatch):
    monkeypatch.setattr(settings, "vad_adaptativo", True)
    ctx, sess = _sessao(monkeypatch)
    agora = time.time()
    sess.is_recording = True
    sess.audio_buffer = [np.zeros(160, dtype=np.float32)] * (settings.vad_min_frames + 5)
    sess._fala_inicio = agora - 10.0         # fala longa (raciocínio)
    sess.last_audio_time = agora - 0.9       # 0,9s <= teto (1,2s) -> ainda esperando

    await sess._check_silence()

    assert sess.is_recording is True         # não encerrou: margem cheia p/ fala longa
    assert ctx.stt.chamadas == 0


def test_retomada_apos_endpoint_curto_e_logada(monkeypatch):
    monkeypatch.setattr(settings, "vad_adaptativo", True)
    ctx, sess = _sessao(monkeypatch)
    avisos = []
    monkeypatch.setattr(ws_mod.telemetry, "warn", lambda mod, msg: avisos.append((mod, msg)))

    # último endpoint foi CURTO e recém-aconteceu; a fala recomeça em seguida
    sess._fim_fala_ts = time.time() - 0.3
    sess._janela_usada = settings.vad_silence_curta_seconds
    pcm = (np.ones(320, dtype=np.int16) * 8000).tobytes()   # frame com voz (rms alto)

    sess._on_audio(pcm)

    assert any("corte precoce" in msg for _, msg in avisos)


def test_retomada_apos_endpoint_cheio_nao_alarma(monkeypatch):
    # Janela cheia = o comportamento histórico; retomar depois dela não é sinal de corte.
    monkeypatch.setattr(settings, "vad_adaptativo", True)
    ctx, sess = _sessao(monkeypatch)
    avisos = []
    monkeypatch.setattr(ws_mod.telemetry, "warn", lambda mod, msg: avisos.append(msg))

    sess._fim_fala_ts = time.time() - 0.3
    sess._janela_usada = settings.vad_silence_seconds        # endpoint com o teto cheio
    sess._on_audio((np.ones(320, dtype=np.int16) * 8000).tobytes())

    assert avisos == []
