"""
Sintetiza uma fala de teste em 16 kHz para o teste de voz do app Android.

    python scripts/gerar_fala_teste.py

Por que existe: o emulador do Android não tem microfone de verdade, então o
caminho de voz do app (captura -> quadros de 1024 -> WebSocket -> Whisper) não
tem como ser exercitado apertando o botão. Este script usa o PRÓPRIO Piper do
servidor para produzir a fala, e o teste Kotlin injeta essas amostras como se
tivessem vindo do microfone.

O arquivo sai em `android/app/src/test/resources/fala_16k.wav`, no formato EXATO
que o servidor espera: PCM16 mono 16000 Hz (ws.py:312 lê `np.int16`; o Whisper
assume a taxa nativa e o servidor NÃO reamostra).
"""
from __future__ import annotations

import asyncio
import base64
import io
import sys
import wave
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

FRASE = "quanto é dois mais dois?"
DESTINO = RAIZ / "android" / "app" / "src" / "test" / "resources" / "fala_16k.wav"
TAXA_ALVO = 16000


def reamostrar(amostras, de_hz: int, para_hz: int):
    """Reamostragem linear. Suficiente aqui: o alvo é o Whisper, não fidelidade."""
    if de_hz == para_hz:
        return amostras
    import numpy as np

    n_saida = int(len(amostras) * para_hz / de_hz)
    x_velho = np.linspace(0.0, 1.0, num=len(amostras), endpoint=False)
    x_novo = np.linspace(0.0, 1.0, num=n_saida, endpoint=False)
    return np.interp(x_novo, x_velho, amostras).astype(np.int16)


async def main() -> int:
    import numpy as np

    from mente_digital.audio import build_tts

    tts = build_tts()
    # `ready` e não o retorno de `load()`: o contrato duck-typed dos dois engines
    # é o atributo, e o XTTS devolve None mesmo tendo carregado.
    tts.load()
    if not getattr(tts, "ready", False):
        print("[FALA] TTS não carregou — não dá para gerar a amostra.")
        return 1

    b64 = await tts.synth_base64(FRASE)
    if not b64:
        print("[FALA] o TTS não devolveu áudio.")
        return 1

    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
        taxa = w.getframerate()
        canais = w.getnchannels()
        bruto = w.readframes(w.getnframes())
    amostras = np.frombuffer(bruto, dtype=np.int16)
    if canais > 1:
        amostras = amostras.reshape(-1, canais)[:, 0]
    print(f"[FALA] Piper devolveu {taxa} Hz, {canais} canal(is), {len(amostras)} amostras")

    saida = reamostrar(amostras, taxa, TAXA_ALVO)
    # Um pouco de silêncio no fim: o VAD do servidor precisa VER a fala terminar
    # (vad_silence_seconds) para fechar o turno. Sem isto o teste esperaria para
    # sempre por uma transcrição que nunca vem.
    silencio = np.zeros(int(TAXA_ALVO * 2.0), dtype=np.int16)
    saida = np.concatenate([saida, silencio])

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(DESTINO), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA_ALVO)
        w.writeframes(saida.tobytes())
    dur = len(saida) / TAXA_ALVO
    print(f"[FALA] OK -> {DESTINO.relative_to(RAIZ)} "
          f"({TAXA_ALVO} Hz, {dur:.1f}s, {DESTINO.stat().st_size // 1024} KB)")
    print(f'[FALA] frase: "{FRASE}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
