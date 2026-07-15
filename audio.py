"""
Áudio — tudo na CPU, sempre atrás de asyncio.to_thread.

- SttService  : Whisper (small) -> texto.
- TtsService  : Piper (voz Cadu) -> WAV base64, com dicionário fonético.
- SentenceChunker : decide quando uma frase está "fechada" para sintetizar.

SentenceChunker é a peça nova de latência: no monólito, o TTS quebrava em QUALQUER
'.', então "Dr.", "3.5" ou "etc." picotavam o áudio. Aqui a quebra só acontece em
fim-de-frase real (ignora abreviações e decimais) e há flush por tamanho para
frases longas não segurarem o áudio.
"""
from __future__ import annotations

import asyncio
import base64
import io
import re
import wave
from typing import List, Optional

import numpy as np

from config import DICIONARIO_FONETICO, settings
from telemetry import telemetry


# ==========================================================================
# SentenceChunker
# ==========================================================================
class SentenceChunker:
    _ABBREV = {
        "sr", "sra", "srs", "dr", "dra", "prof", "profa", "etc", "ex", "p.ex",
        "i.e", "e.g", "vs", "núm", "no", "nº", "art", "fig", "sec", "min", "seg",
        "obs", "ref", "pg", "pág", "cap",
    }
    _BOUNDARY = re.compile(r"[.!?…]+(\s|$)")

    def __init__(self, min_len: Optional[int] = None, max_len: Optional[int] = None) -> None:
        self.buffer = ""
        self.min_len = min_len if min_len is not None else settings.tts_chunk_min_chars
        self.max_len = max_len if max_len is not None else settings.tts_chunk_max_chars

    def push(self, token: str) -> List[str]:
        """Adiciona um token e devolve as frases que ficaram prontas (0..n)."""
        self.buffer += token
        prontas: List[str] = []
        while True:
            frase = self._extract()
            if frase is None:
                break
            prontas.append(frase)
        return prontas

    def _extract(self) -> Optional[str]:
        for m in self._BOUNDARY.finditer(self.buffer):
            punct_start = m.start()
            end = m.start() + len(m.group().rstrip())  # posição após a pontuação
            head = self.buffer[:punct_start]
            # abreviação? (palavra antes do ponto)
            ultima = re.split(r"\s", head)[-1].lower().strip("([{\"'") if head else ""
            if self.buffer[punct_start] == "." and ultima in self._ABBREV:
                continue
            candidato = self.buffer[:end].strip()
            if len(candidato) >= self.min_len:
                self.buffer = self.buffer[end:].lstrip()
                return candidato
            # muito curta -> continua acumulando
        # flush por tamanho: frase longa sem pontuação não pode travar o áudio.
        # Corta no último espaço DENTRO da janela de max_len (não no fim do buffer),
        # para emitir pedaços ~max_len e manter o áudio fluindo.
        if len(self.buffer) >= self.max_len:
            janela = self.buffer[: self.max_len]
            corte = max(janela.rfind(", "), janela.rfind(" "), janela.rfind("\n"))
            if corte <= 0:
                corte = self.max_len
            candidato = self.buffer[:corte].strip()
            self.buffer = self.buffer[corte:].lstrip()
            if candidato:
                return candidato
        return None

    def flush(self) -> str:
        """Devolve o que sobrou (fim da resposta)."""
        resto = self.buffer.strip()
        self.buffer = ""
        return resto


# ==========================================================================
# STT — Whisper
# ==========================================================================
class SttService:
    """STT via faster-whisper (CTranslate2): mesmos pesos do Whisper, mais rápido.

    A troca não muda a qualidade por modelo — habilita subir para `large-v3` no
    mesmo hardware. A entrada é o mesmo `np.ndarray` float32 mono 16kHz de antes.
    """

    def __init__(self) -> None:
        self._model = None

    def load(self) -> None:
        """Síncrono — chamar via asyncio.to_thread no startup."""
        try:
            from faster_whisper import WhisperModel

            from rag import resolve_device  # reusa a resolução auto/cuda/cpu

            try:
                import torch

                cuda_ok = torch.cuda.is_available()
            except Exception:
                cuda_ok = False
            device = resolve_device(settings.whisper_device, cuda_ok)
            compute = settings.whisper_compute_type
            if compute == "auto":
                compute = "float16" if device == "cuda" else "int8"

            # download_root: baixa/lê os pesos do Whisper na pasta do projeto
            # (./modelos/whisper), em vez do cache global do HuggingFace.
            self._model = WhisperModel(
                settings.whisper_model,
                device=device,
                compute_type=compute,
                download_root=settings.caminho_cache_whisper,
            )
            telemetry.track(
                "WHISPER",
                f"faster-whisper '{settings.whisper_model}' carregado ({device}/{compute}).",
            )
        except Exception as exc:
            telemetry.error("WHISPER", "Falha ao carregar faster-whisper", exc)

    @property
    def ready(self) -> bool:
        return self._model is not None

    async def transcribe(self, audio_numpy: "np.ndarray") -> str:
        if self._model is None:
            telemetry.warn("WHISPER", "Transcrição solicitada sem modelo.")
            return ""
        try:
            def _run() -> str:
                # transcribe() é lazy: a geração só roda ao iterar os segmentos.
                segmentos, _info = self._model.transcribe(audio_numpy, language="pt")
                return "".join(seg.text for seg in segmentos).strip()

            return await asyncio.to_thread(_run)
        except Exception as exc:
            telemetry.error("WHISPER", "Erro na transcrição", exc)
            return ""


# ==========================================================================
# TTS — Piper
# ==========================================================================
class TtsService:
    _STRIP_MD = re.compile(r"[\[\]*#_`]")
    _ALLOWED = re.compile(r"[^\w\s.,!?çÇáéíóúÁÉÍÓÚâêîôûÂÊÎÔÛãõÃÕàÀ-]")

    def __init__(self) -> None:
        self._voice = None

    def load(self) -> None:
        """Síncrono — chamar via asyncio.to_thread no startup."""
        try:
            from piper.voice import PiperVoice

            self._voice = PiperVoice.load(settings.caminho_voz_piper)
            telemetry.track("PIPER", "Voz Cadu carregada (CPU).")
        except Exception as exc:
            telemetry.error("PIPER", "Falha ao carregar Piper", exc)

    @property
    def ready(self) -> bool:
        return self._voice is not None

    def _normalizar(self, texto: str) -> str:
        texto = self._STRIP_MD.sub("", texto)
        for ing, pt in DICIONARIO_FONETICO.items():
            texto = re.sub(rf"\b{re.escape(ing)}\b", pt, texto, flags=re.IGNORECASE)
        return self._ALLOWED.sub("", texto).strip()

    async def synth_base64(self, texto: str) -> Optional[str]:
        """Sintetiza uma frase em WAV base64. None se vazio ou indisponível."""
        if self._voice is None or not texto.strip():
            return None
        texto_voz = self._normalizar(texto)
        if not texto_voz:
            return None
        try:
            def _run() -> bytes:
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(self._voice.config.sample_rate)
                    for chunk in self._voice.synthesize(texto_voz):
                        w.writeframes(chunk.audio_int16_bytes)
                return buf.getvalue()

            data = await asyncio.to_thread(_run)
            return base64.b64encode(data).decode("utf-8")
        except Exception as exc:
            telemetry.error("PIPER", "Erro ao sintetizar áudio", exc)
            return None
