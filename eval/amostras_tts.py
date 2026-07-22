"""
Amostras de prosódia do Piper — calibração DE OUVIDO dos knobs MENTE_TTS_*.

Sintetiza o mesmo parágrafo (com pergunta, exclamação e reticências, para a
entonação aparecer) em várias combinações de SynthesisConfig e grava um WAV por
variante em eval/amostras_tts_out/. Ouça lado a lado e ponha a vencedora no .env.

Uso (env llama-omni):
    python eval/amostras_tts.py
    python eval/amostras_tts.py --texto "outra frase para comparar"

Roda 100% na CPU (Piper), sem tocar a GPU — pode rodar com o servidor de pé.
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mente_digital.config import settings  # noqa: E402

TEXTO_PADRAO = (
    "Oi! Tudo bem por aí? Deixa eu te contar uma coisa... a voz também conversa. "
    "Quando a pontuação chega inteira, a entonação sobe na pergunta, desce no ponto, "
    "e respira na vírgula. Legal, né?"
)

# (rótulo, noise_scale, noise_w_scale, length_scale) — None = default treinado da voz.
VARIANTES = [
    ("default_da_voz", None, None, None),
    ("vivo_leve", 0.75, 1.0, None),      # o default novo do .env
    ("vivo_forte", 0.85, 1.1, None),
    ("calmo_lento", 0.70, 0.9, 1.08),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--texto", default=TEXTO_PADRAO)
    args = ap.parse_args()

    from piper.config import SynthesisConfig
    from piper.voice import PiperVoice

    voice = PiperVoice.load(settings.caminho_voz_piper)
    out_dir = Path(__file__).resolve().parent / "amostras_tts_out"
    out_dir.mkdir(exist_ok=True)

    for rotulo, noise, noise_w, length in VARIANTES:
        cfg = None
        if any(v is not None for v in (noise, noise_w, length)):
            cfg = SynthesisConfig(noise_scale=noise, noise_w_scale=noise_w, length_scale=length)
        destino = out_dir / f"{rotulo}.wav"
        with wave.open(str(destino), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(voice.config.sample_rate)
            chunks = voice.synthesize(args.texto, syn_config=cfg) if cfg else voice.synthesize(args.texto)
            for chunk in chunks:
                w.writeframes(chunk.audio_int16_bytes)
        print(f"[ok] {destino.name}  (noise={noise} noise_w={noise_w} length={length})")

    print(f"\nOuça os WAVs em {out_dir} e ponha a vencedora no .env (MENTE_TTS_*).")


if __name__ == "__main__":
    main()
