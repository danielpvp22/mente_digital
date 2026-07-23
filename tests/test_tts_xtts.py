"""
XTTS-v2 (engine de voz GPU alternativo) — testes SEM GPU/coqui/modelo.

Exercita as partes puras/mockáveis do backend: degradação fail-soft, modelagem de texto
(verbalizar sem o filtro _ALLOWED do Piper), a montagem do WAV a partir do streaming (com
um modelo falso que faz yield de numpy) e a fábrica build_tts — provando que o caminho
Piper (default) nunca importa coqui e que o import do coqui é tardio.
"""
from __future__ import annotations

import base64
import io
import sys
import wave

import numpy as np

from mente_digital.audio import TtsService, build_tts
from mente_digital.config import Settings, settings
from mente_digital.tts_xtts import XttsService, _to_int16


class _FakeXttsModel:
    """Modelo XTTS falso: inference_stream faz yield de dois chunks numpy float32."""

    def inference_stream(self, texto, lang, gpt, spk, **kw):
        yield np.array([0.0, 0.5, -0.5], dtype="float32")
        yield np.array([0.25, -0.25], dtype="float32")


# -- degradação: sem load(), o engine não fala (mas não quebra) ----------------
async def test_sem_load_ready_false_e_synth_none():
    x = XttsService()
    assert x.ready is False
    assert await x.synth_base64("olá") is None


async def test_texto_vazio_retorna_none():
    x = XttsService()
    x._model = _FakeXttsModel()
    assert await x.synth_base64("   ") is None


# -- modelagem de texto: verbaliza números, tira Markdown, PRESERVA acentos/símbolos --
def test_preparar_verbaliza_numeros():
    x = XttsService()
    assert "três vírgula cinco" in x._preparar("custa 3,5")


def test_preparar_tira_markdown_mas_preserva_acentos_e_simbolos():
    x = XttsService()
    out = x._preparar("**80°C** no café, 50%")
    # Diferente do Piper: NÃO passa pelo _ALLOWED, então ° % e acentos sobrevivem
    # (o XTTS os foneiza). Só a marcação Markdown some.
    assert "*" not in out
    assert "café" in out
    assert "oitenta graus célsius" in out
    assert "cinquenta por cento" in out


# -- montagem do WAV a partir do streaming (modelo falso, sem torch) -----------
async def test_synth_monta_wav_valido():
    x = XttsService()
    x._model = _FakeXttsModel()
    x._sample_rate = 24000
    b64 = await x.synth_base64("olá mundo")
    assert b64
    raw = base64.b64decode(b64)
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 24000
        assert w.getnframes() == 5   # 3 + 2 amostras dos dois chunks


def test_to_int16_de_numpy():
    # 1.0 -> 32767; -1.0 -> -32767; clamp em ±1. Sem torch (numpy puro).
    b = _to_int16(np.array([0.0, 1.0, -1.0, 2.0], dtype="float32"))
    vals = np.frombuffer(b, dtype="<i2")
    assert list(vals) == [0, 32767, -32767, 32767]


# -- fábrica: default Piper; xtts só sob flag, sem importar coqui ---------------
def test_build_tts_default_e_piper(monkeypatch):
    monkeypatch.setattr(settings, "tts_engine", "piper")
    assert isinstance(build_tts(), TtsService)


def test_build_tts_xtts_nao_importa_coqui(monkeypatch):
    monkeypatch.setattr(settings, "tts_engine", "xtts")
    eng = build_tts()
    assert type(eng).__name__ == "XttsService"
    # Construir o engine NÃO pode ter importado o coqui (import é tardio, só no load()).
    assert "TTS" not in sys.modules


# -- default seguro no código (ignora o .env local) ----------------------------
def test_engine_default_e_piper():
    assert Settings(_env_file=None).tts_engine == "piper"
