"""
XTTS-v2 (Coqui) — engine de TTS alternativo na GPU.

PORQUÊ: o Piper (VITS/CPU) é rápido, mas robótico. O XTTS-v2 soa muito mais humano e
clona voz, ao custo de VRAM + GPU. É opt-in por MENTE_TTS_ENGINE=xtts (default piper).

CONTRATO (duck-typed, o MESMO do TtsService — não há classe base): load() síncrono e à
prova de falha (NUNCA levanta; em erro deixa ready=False), ready -> bool, e
async synth_base64(texto) -> WAV base64 (ou None). O texto chega CRU — a modelagem
(verbalizar) é feita aqui, mas SEM o filtro _ALLOWED do Piper (o XTTS foneiza texto rico).

GPU: roda no PRÓPRIO asyncio.to_thread (como o Piper na CPU), NÃO no executor serializado
do LLM. Rotear pelo executor do LLM DEADLOCKARIA: o stream() do LLM ocupa o worker único
durante o turno inteiro, e a síntese por frase (chamada DURANTE o stream) ficaria presa
atrás dele. Consequência: na MESMA GPU do LLM há contenção real (VRAM/compute) — mitigada
por fp16, mas inerente; o cenário ideal é GPU separada (tts_xtts_device=cuda:1). O import
do coqui/torch é TARDIO (dentro de load()/_to_int16) para o módulo importar no CI sem eles.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import wave
from collections import OrderedDict
from typing import Optional

from mente_digital.config import settings
from mente_digital.telemetry import telemetry
from mente_digital.verbalizar import verbalizar

# Só tira a marcação Markdown (o XTTS lê pontuação/acentos nativamente — ao contrário do
# Piper, não precisa do _ALLOWED que apagava $ % ° etc.). Números viram palavra via verbalizar.
_STRIP_MD = re.compile(r"[\[\]*#`]")


def _to_int16(chunk) -> bytes:
    """Converte um chunk de áudio (torch.Tensor OU numpy float em [-1,1]) em PCM int16.

    Detecta torch por duck-typing (hasattr 'detach') para NÃO importar torch — assim os
    testes injetam um modelo falso que faz yield de numpy e isto roda sem GPU/torch."""
    import numpy as np

    if hasattr(chunk, "detach"):                     # torch.Tensor
        chunk = chunk.detach().to("cpu").float().numpy()
    a = np.asarray(chunk, dtype="float32").reshape(-1).clip(-1.0, 1.0)
    return (a * 32767.0).astype("<i2").tobytes()


class XttsService:
    """Backend XTTS-v2. Mesmos 3 membros públicos do TtsService (load/ready/synth_base64)."""

    def __init__(self) -> None:
        self._model = None
        self._device: Optional[str] = None
        self._gpt_cond_latent = None
        self._speaker_embedding = None
        self._sample_rate = 24000
        self._cache: "OrderedDict[str, str]" = OrderedDict()

    def load(self) -> None:
        """Síncrono — chamar via asyncio.to_thread no startup. NUNCA levanta (fail-soft)."""
        try:
            import torch
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts

            from mente_digital.rag import resolve_device

            device = resolve_device(settings.tts_xtts_device, torch.cuda.is_available())
            cfg_dir = settings.caminho_modelo_xtts
            config = XttsConfig()
            config.load_json(os.path.join(cfg_dir, "config.json"))
            model = Xtts.init_from_config(config)
            model.load_checkpoint(
                config, checkpoint_dir=cfg_dir, use_deepspeed=settings.tts_xtts_use_deepspeed,
            )
            model.to(device)
            usar_fp16 = settings.tts_xtts_fp16 and device.startswith("cuda")
            if usar_fp16:
                model.half()
            model.eval()

            gpt_cond, spk_emb = self._resolver_speaker(model)
            if usar_fp16:
                # Latentes precisam casar o dtype do modelo (senão inference_stream quebra).
                gpt_cond, spk_emb = gpt_cond.half(), spk_emb.half()

            self._model = model
            self._device = device
            self._gpt_cond_latent = gpt_cond
            self._speaker_embedding = spk_emb
            self._sample_rate = int(
                getattr(config.model_args, "output_sample_rate", 24000) or 24000
            )
            # Warm-up: a 1ª inferência aloca/compila kernels — absorve o pico fora do hot path.
            for _ in model.inference_stream(
                "Ok.", settings.tts_xtts_language, gpt_cond, spk_emb
            ):
                pass
            telemetry.track(
                "XTTS",
                f"XTTS-v2 carregado ({device}, fp16={usar_fp16}, sr={self._sample_rate}).",
            )
        except Exception as exc:
            telemetry.error("XTTS", "Falha ao carregar XTTS-v2", exc)  # ready fica False

    def _resolver_speaker(self, model):
        """(gpt_cond_latent, speaker_embedding): clone de .wav OU locutor embutido."""
        wav = settings.tts_xtts_speaker_wav
        if wav and os.path.exists(wav):                          # (b) clonagem zero-shot
            return model.get_conditioning_latents(audio_path=[wav])
        sm = getattr(model, "speaker_manager", None)             # (a) locutor embutido
        if sm is None or not getattr(sm, "speakers", None):
            raise RuntimeError(
                "speakers_xtts.pth ausente e nenhum tts_xtts_speaker_wav — sem voz para o XTTS."
            )
        entry = sm.speakers[settings.tts_xtts_speaker]
        return entry["gpt_cond_latent"], entry["speaker_embedding"]

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _preparar(self, texto: str) -> str:
        """Modelagem de texto: verbaliza números e tira Markdown. SEM o filtro _ALLOWED
        do Piper — o XTTS foneiza acentos/símbolos nativamente. Puro/testável sem modelo."""
        texto = _STRIP_MD.sub("", texto)
        texto = verbalizar(texto)
        return re.sub(r"\s{2,}", " ", texto).strip()

    async def synth_base64(self, texto: str) -> Optional[str]:
        """Sintetiza uma frase em WAV base64. None se vazio/indisponível/erro (o pipeline
        pula o áudio nesse caso). Síntese pesada roda em asyncio.to_thread (fora do loop)."""
        if self._model is None or not texto.strip():
            return None
        texto_voz = self._preparar(texto)
        if not texto_voz:
            return None
        if settings.tts_xtts_cache_enabled:
            cached = self._cache.get(texto_voz)
            if cached is not None:
                self._cache.move_to_end(texto_voz)
                return cached
        try:
            data = await asyncio.to_thread(self._run, texto_voz)
            b64 = base64.b64encode(data).decode("utf-8")
            if settings.tts_xtts_cache_enabled:
                self._cache[texto_voz] = b64
                while len(self._cache) > settings.tts_cache_size:
                    self._cache.popitem(last=False)
            return b64
        except Exception as exc:
            telemetry.error("XTTS", "Erro ao sintetizar (XTTS)", exc)
            return None

    def _run(self, texto_voz: str) -> bytes:
        """Streaming interno do XTTS -> um WAV (mono, 16-bit, sample_rate no cabeçalho)."""
        kw = {
            "stream_chunk_size": settings.tts_xtts_stream_chunk_size,
            "enable_text_splitting": settings.tts_xtts_enable_text_splitting,
        }
        # Knobs de amostragem só entram quando setados (None = default treinado do XTTS).
        for nome, valor in (
            ("temperature", settings.tts_xtts_temperature),
            ("repetition_penalty", settings.tts_xtts_repetition_penalty),
            ("top_k", settings.tts_xtts_top_k),
            ("top_p", settings.tts_xtts_top_p),
        ):
            if valor is not None:
                kw[nome] = valor
        chunks = self._model.inference_stream(
            texto_voz, settings.tts_xtts_language,
            self._gpt_cond_latent, self._speaker_embedding, **kw,
        )
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self._sample_rate)
            for ch in chunks:                            # torch float @ sample_rate em [-1,1]
                w.writeframes(_to_int16(ch))
        return buf.getvalue()
