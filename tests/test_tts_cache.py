"""
Cache de voz (#1): frase repetida não re-sintetiza (TTFA≈0 na 2ª vez).
Usa uma voz Piper falsa que conta as sínteses reais.
"""
from __future__ import annotations

import pytest

from audio import TtsService
from config import settings


class _Cfg:
    sample_rate = 22050


class _Chunk:
    audio_int16_bytes = b"\x00\x01\x02\x03"


class FakeVoice:
    def __init__(self) -> None:
        self.sinteses = 0
        self.config = _Cfg()

    def synthesize(self, texto):
        self.sinteses += 1
        yield _Chunk()


async def test_cache_evita_ressintese():
    tts = TtsService()
    tts._voice = FakeVoice()

    a = await tts.synth_base64("Pronto.")
    b = await tts.synth_base64("Pronto.")      # mesma frase -> cache
    assert a == b and a is not None
    assert tts._voice.sinteses == 1            # só sintetizou uma vez
    assert tts._cache_hits == 1


async def test_cache_normaliza_chave():
    tts = TtsService()
    tts._voice = FakeVoice()
    # A chave é o texto normalizado; markdown/pontuação equivalentes batem no mesmo áudio.
    await tts.synth_base64("Pronto")
    await tts.synth_base64("*Pronto*")
    assert tts._voice.sinteses == 1


async def test_cache_respeita_limite(monkeypatch):
    monkeypatch.setattr(settings, "tts_cache_size", 3)
    tts = TtsService()
    tts._voice = FakeVoice()
    for i in range(5):
        await tts.synth_base64(f"frase numero {i}")
    assert len(tts._cache) <= 3                 # LRU podou os mais antigos


async def test_textos_diferentes_sintetizam():
    tts = TtsService()
    tts._voice = FakeVoice()
    await tts.synth_base64("bom dia")
    await tts.synth_base64("boa noite")
    assert tts._voice.sinteses == 2
