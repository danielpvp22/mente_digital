"""
Spike Whisper→GPU com aborto automático (consultoria TTFT #4): se o load em cuda
falhar (OOM/fragmentação — a VRAM opera com ~1,3GB livres), o STT cai para CPU/int8
em vez de morrer. O faster-whisper/torch são stubs em sys.modules: o teste exercita
só a DECISÃO de fallback, sem pesos nem CUDA.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from mente_digital import rag
from mente_digital.audio import SttService
from mente_digital.config import settings


class _StubWhisper:
    instancias = []

    def __init__(self, model, device=None, compute_type=None, download_root=None,
                 cpu_threads=None):
        _StubWhisper.instancias.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("CUDA out of memory (simulado)")


def _prepara(monkeypatch):
    _StubWhisper.instancias = []
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _StubWhisper
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_available=lambda: True)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(rag, "resolve_device", lambda pref, ok: pref)
    monkeypatch.setattr(settings, "whisper_device", "cuda")
    monkeypatch.setattr(settings, "whisper_compute_type", "int8")


def test_oom_na_gpu_cai_para_cpu_int8(monkeypatch):
    _prepara(monkeypatch)
    monkeypatch.setattr(settings, "whisper_gpu_fallback_cpu", True)

    stt = SttService()
    stt.load()

    assert stt.ready                          # o STT sobreviveu ao spike falhado
    assert _StubWhisper.instancias == [("cuda", "int8"), ("cpu", "int8")]


def test_sem_fallback_a_falha_e_visivel(monkeypatch):
    # Botão off: quem desligou o paraquedas quer VER a falha (telemetry.error), não
    # um fallback silencioso — o load termina sem modelo.
    _prepara(monkeypatch)
    monkeypatch.setattr(settings, "whisper_gpu_fallback_cpu", False)

    stt = SttService()
    stt.load()

    assert not stt.ready
    assert _StubWhisper.instancias == [("cuda", "int8")]


def test_cpu_nao_passa_pelo_fallback(monkeypatch):
    # Produção de hoje (device=cpu): caminho intacto, uma única instância.
    _prepara(monkeypatch)
    monkeypatch.setattr(settings, "whisper_device", "cpu")

    stt = SttService()
    stt.load()

    assert stt.ready
    assert _StubWhisper.instancias == [("cpu", "int8")]
