"""
Barge-in do SERVIDOR (teste real 2507): o dono falando por cima da resposta corta a
fala dela. Guard anti-eco: só RMS ALTO (barge_rms_threshold) SUSTENTADO por
barge_min_frames consecutivos derruba o pipeline — um pico curto (eco do próprio TTS)
zera o contador sem cortar. Sem WebSocket/GPU: só a lógica pura do _on_audio.
"""
import numpy as np

from config import settings
from state import AppContext
from ws import LiveSession


class _Ws:
    async def accept(self): ...
    async def send_json(self, _): ...


class _Llama:
    ready = True   # evita o ensure_loaded no 1º frame de fala


class _Task:
    """Pipeline falso: 'vivo' (done=False) e registra o cancel."""
    def __init__(self):
        self.cancelled = False

    def done(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


def _frame(amplitude: int) -> bytes:
    # 160 amostras int16 constantes = 10ms a 16kHz; rms = amplitude/32768.
    return np.full(160, amplitude, dtype=np.int16).tobytes()


_ALTO = _frame(3000)    # rms ~0.092  > barge_rms_threshold (0.02)
_BAIXO = _frame(40)     # rms ~0.001  < vad_rms_threshold (0.005)


def _sessao(monkeypatch, min_frames=3):
    monkeypatch.setattr(settings, "barge_in_servidor", True)
    monkeypatch.setattr(settings, "barge_min_frames", min_frames)
    ctx = AppContext(settings=settings)
    ctx.llama = _Llama()
    s = LiveSession(ctx, _Ws())
    return s


def test_fala_sustentada_corta_a_resposta(monkeypatch):
    s = _sessao(monkeypatch, min_frames=3)
    s.pipeline_task = _Task()
    for _ in range(3):
        s._on_audio(_ALTO)
    assert s.pipeline_task.cancelled is True      # 3 frames altos seguidos -> barge-in


def test_pico_curto_de_eco_nao_corta(monkeypatch):
    # 2 frames altos (abaixo do mínimo de 3) e um baixo ZERA o contador — eco não corta.
    s = _sessao(monkeypatch, min_frames=3)
    s.pipeline_task = _Task()
    s._on_audio(_ALTO)
    s._on_audio(_ALTO)
    s._on_audio(_BAIXO)                            # reseta
    s._on_audio(_ALTO)
    assert s.pipeline_task.cancelled is False


def test_sem_resposta_em_voo_nao_faz_nada(monkeypatch):
    s = _sessao(monkeypatch, min_frames=3)
    s.pipeline_task = None                         # nada tocando
    for _ in range(5):
        s._on_audio(_ALTO)                         # não deve estourar
    assert s.pipeline_task is None


def test_desligado_nao_corta(monkeypatch):
    s = _sessao(monkeypatch, min_frames=3)
    monkeypatch.setattr(settings, "barge_in_servidor", False)
    s.pipeline_task = _Task()
    for _ in range(5):
        s._on_audio(_ALTO)
    assert s.pipeline_task.cancelled is False      # botão off: nunca corta


def test_fala_baixa_nao_corta(monkeypatch):
    # Voz fraca / eco atenuado (abaixo do barge_rms_threshold) nunca acumula -> não corta.
    s = _sessao(monkeypatch, min_frames=3)
    s.pipeline_task = _Task()
    for _ in range(10):
        s._on_audio(_frame(300))                   # rms ~0.009: passa o VAD, NÃO o barge
    assert s.pipeline_task.cancelled is False
