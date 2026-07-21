"""
Varredura de threads × afinidade de CCD para o Whisper na CPU (consultoria TTFT #11).

Por quê: o 7950X3D tem DOIS CCDs (0 = 3D V-Cache, 1 = clock mais alto) e o scheduler
do Windows espalha as threads do CTranslate2 entre eles — `whisper_cpu_threads=8`
hoje nem escolhe onde roda. Cache grande e clock alto puxam o GEMM int8 para lados
diferentes; a resposta certa é EMPÍRICA, por isso este bench. É o plano B barato do
spike Whisper→GPU (#4): se o Whisper for para a GPU, isto morre sem dó.

O áudio de teste é FALA REAL: a frase fixa abaixo é sintetizada pelo PRÓPRIO Piper
do projeto (não ruído — Whisper em ruído devolve vazio e o timing mente) e
reamostrada para 16 kHz.

Uso (env llama-omni, servidor PARADO — o bench compete pelos mesmos núcleos):
    python scripts/bench_stt_threads.py [--threads 4,8,12,16] [--reps 3] [--sem-afinidade]

Aplicar o vencedor: MENTE_WHISPER_CPU_THREADS=<n> no .env. Afinidade de CCD, se
valer a pena, via `start /affinity <mask>` no atalho de boot (o processo inteiro).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import io
import statistics
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from audio import SttService, TtsService  # noqa: E402
from config import settings  # noqa: E402

FRASE = (
    "Mestre, adiciona leite e ovos na lista de compras e me lembra de revisar "
    "o relatório de latência amanhã às oito da manhã, por favor."
)


def _audio_de_teste() -> "np.ndarray":
    """Sintetiza a FRASE com o Piper do projeto e devolve float32 mono 16 kHz."""
    tts = TtsService()
    tts.load()
    if not tts.ready:
        raise SystemExit("Piper indisponível — aponte MENTE_CAMINHO_VOZ_PIPER (precisa de fala real).")
    b64 = asyncio.run(tts.synth_base64(FRASE))
    if not b64:
        raise SystemExit("Síntese do Piper devolveu vazio.")
    with wave.open(io.BytesIO(base64.b64decode(b64))) as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    if sr != 16000:  # Whisper espera 16 kHz; interp linear basta para bench
        n_out = int(len(audio) * 16000 / sr)
        audio = np.interp(
            np.linspace(0, len(audio) - 1, n_out), np.arange(len(audio)), audio
        ).astype(np.float32)
    print(f"áudio de teste: {len(audio) / 16000:.1f}s de fala sintetizada (sr origem {sr})")
    return audio


def _mascaras_ccd() -> list[tuple[str, int | None]]:
    """(rótulo, máscara de afinidade). No 7950X3D (32 CPUs lógicas): CCD0 = 0-15
    (V-Cache), CCD1 = 16-31 (clock). Fora desse formato, só 'todos'."""
    n = ctypes.windll.kernel32.GetActiveProcessorCount(0xFFFF) if sys.platform == "win32" else 0
    if sys.platform != "win32" or n < 32:
        return [("todos", None)]
    metade = n // 2
    return [
        ("todos", None),
        ("ccd0-vcache", (1 << metade) - 1),
        ("ccd1-clock", ((1 << metade) - 1) << metade),
    ]


def _aplicar_afinidade(mask: int | None) -> None:
    if sys.platform != "win32":
        return
    k32 = ctypes.windll.kernel32
    proc = k32.GetCurrentProcess()
    if mask is None:
        pm = ctypes.c_size_t()
        sm = ctypes.c_size_t()
        k32.GetProcessAffinityMask(proc, ctypes.byref(pm), ctypes.byref(sm))
        k32.SetProcessAffinityMask(proc, sm.value)      # tudo que o sistema permite
    else:
        k32.SetProcessAffinityMask(proc, ctypes.c_size_t(mask))


def main() -> int:
    ap = argparse.ArgumentParser(description="Varredura threads x CCD do Whisper-CPU (#11)")
    ap.add_argument("--threads", default="4,8,12,16")
    ap.add_argument("--reps", type=int, default=3, help="transcrições medidas por config (fora o warm-up)")
    ap.add_argument("--sem-afinidade", action="store_true", help="varre só threads")
    args = ap.parse_args()

    audio = _audio_de_teste()
    dur = len(audio) / 16000
    threads = [int(t) for t in args.threads.split(",")]
    afinidades = [("todos", None)] if args.sem_afinidade else _mascaras_ccd()

    print(f"\n{'config':<24} {'stt_ms (mediana)':<18} {'x tempo real'}")
    melhores: list[tuple[float, str]] = []
    for rotulo, mask in afinidades:
        _aplicar_afinidade(mask)
        for n in threads:
            settings.whisper_cpu_threads = n            # o load lê daqui
            stt = SttService()
            stt.load()                                   # instância NOVA por config
            if not stt.ready:
                print(f"{rotulo}/t{n}: whisper indisponível — abortando")
                return 1
            asyncio.run(stt.transcribe(audio))          # warm-up (aloca buffers)
            tempos = []
            for _ in range(args.reps):
                t0 = time.perf_counter()
                texto = asyncio.run(stt.transcribe(audio))
                tempos.append((time.perf_counter() - t0) * 1000)
            if not texto.strip():
                print(f"{rotulo}/t{n}: transcrição vazia — resultado descartado")
                continue
            med = statistics.median(tempos)
            config = f"{rotulo} / {n} threads"
            print(f"{config:<24} {med:>10.0f} ms      {med / 1000 / dur:.2f}x")
            melhores.append((med, config))
            del stt
    _aplicar_afinidade(None)

    if melhores:
        med, config = min(melhores)
        print(f"\nVencedor: {config} ({med:.0f} ms; baseline de produção ~0,8x tempo real).")
        print("Aplique MENTE_WHISPER_CPU_THREADS no .env; afinidade via `start /affinity`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
