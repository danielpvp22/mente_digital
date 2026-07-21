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
from collections import OrderedDict
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
    # Fronteira EXTRA do 1º chunk (consultoria TTFT #8): vírgula/;/: seguidas de espaço.
    # O espaço é OBRIGATÓRIO (sem o `$` que o _BOUNDARY aceita): protege o decimal
    # PT-BR ("3,5") e, no streaming, segura a vírgula no fim do buffer até o PRÓXIMO
    # token confirmar a fronteira — senão "custa 3," cortaria antes de o "5" chegar,
    # picotando o número (e a decisão dependeria de onde o stream foi fatiado).
    _VIRGULA = re.compile(r"[,;:]\s")

    def __init__(
        self, min_len: Optional[int] = None, max_len: Optional[int] = None,
        primeiro_max: Optional[int] = None,
    ) -> None:
        self.buffer = ""
        self.min_len = min_len if min_len is not None else settings.tts_chunk_min_chars
        self.max_len = max_len if max_len is not None else settings.tts_chunk_max_chars
        # 1º CHUNK AGRESSIVO (#8): o TTFA espera a primeira frase fechar — janela menor
        # e vírgula-como-fronteira SÓ até a primeira emissão. 0 desliga.
        self.primeiro_max = (
            primeiro_max if primeiro_max is not None else settings.tts_chunk_primeiro_max_chars
        )
        self._emitiu = False

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
        # 1º CHUNK AGRESSIVO (consultoria TTFT #8): antes da 1ª emissão, vírgula também
        # fecha frase (o Piper pausa em vírgula — soa respiração, não corte) e o flush
        # forçado usa a janela menor. As decisões dependem só do CONTEÚDO do buffer,
        # nunca de onde o stream foi cortado — é o que preserva as propriedades de
        # invariância/não-perda do test_propriedades.
        primeiro = not self._emitiu and (self.primeiro_max or 0) > 0

        fim: Optional[int] = None
        for m in self._BOUNDARY.finditer(self.buffer):
            punct_start = m.start()
            end = m.start() + len(m.group().rstrip())  # posição após a pontuação
            head = self.buffer[:punct_start]
            # abreviação? (palavra antes do ponto)
            ultima = re.split(r"\s", head)[-1].lower().strip("([{\"'") if head else ""
            if self.buffer[punct_start] == "." and ultima in self._ABBREV:
                continue
            if len(self.buffer[:end].strip()) >= self.min_len:
                fim = end
                break
            # muito curta -> tenta a próxima fronteira

        if primeiro:
            # A vírgula só VENCE se vier ANTES do fim de frase (fronteira mais cedo =
            # áudio mais cedo); curta demais (< min_len) pula para a próxima vírgula.
            for mv in self._VIRGULA.finditer(self.buffer):
                end_v = mv.start() + 1              # inclui a pontuação no chunk
                if len(self.buffer[:end_v].strip()) < self.min_len:
                    continue
                if fim is None or end_v < fim:
                    fim = end_v
                break

        if fim is not None:
            candidato = self.buffer[:fim].strip()
            self.buffer = self.buffer[fim:].lstrip()
            self._emitiu = True
            return candidato

        # flush por tamanho: frase longa sem pontuação não pode travar o áudio.
        # Corta no último espaço DENTRO da janela (não no fim do buffer), para emitir
        # pedaços ~janela e manter o áudio fluindo. No 1º chunk a janela é a menor —
        # min() com max_len para NUNCA alargar (testes usam max_len pequeno de propósito).
        limite = min(self.primeiro_max, self.max_len) if primeiro else self.max_len
        if len(self.buffer) >= limite:
            janela = self.buffer[:limite]
            corte = max(janela.rfind(", "), janela.rfind(" "), janela.rfind("\n"))
            if corte <= 0:
                corte = limite
            candidato = self.buffer[:corte].strip()
            self.buffer = self.buffer[corte:].lstrip()
            if candidato:
                self._emitiu = True
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

            def _carregar(dev: str, comp: str):
                # download_root: baixa/lê os pesos do Whisper na pasta do projeto
                # (./modelos/whisper), em vez do cache global do HuggingFace.
                return WhisperModel(
                    settings.whisper_model,
                    device=dev,
                    compute_type=comp,
                    download_root=settings.caminho_cache_whisper,
                    # 16 núcleos no host; o default do CT2 (~4) deixava GEMM int8 na mesa.
                    cpu_threads=settings.whisper_cpu_threads,
                )

            try:
                self._model = _carregar(device, compute)
            except Exception as exc:
                # Spike Whisper->GPU (consultoria TTFT #4) com critério de aborto
                # AUTOMÁTICO: a VRAM opera com ~1,3GB livres, então o load em cuda pode
                # falhar (OOM/fragmentação — a 3080 também segura o desktop). Cair para
                # CPU/int8 mantém o STT VIVO no comportamento estável de produção; o
                # pior caso do experimento é voltar ao status quo, logado.
                if device != "cuda" or not settings.whisper_gpu_fallback_cpu:
                    raise
                telemetry.warn(
                    "WHISPER",
                    f"Falha ao carregar na GPU ({exc}) — caindo para CPU/int8 (spike #4 abortado).",
                )
                device, compute = "cpu", "int8"
                self._model = _carregar(device, compute)
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
                # beam configurável (greedy por default — ver config); condition_on_
                # previous_text=False porque cada fala aqui é um enunciado isolado —
                # e é o condicionamento que causa os loops de alucinação do Whisper
                # em fala curta/ruidosa.
                segmentos, _info = self._model.transcribe(
                    audio_numpy,
                    language="pt",
                    beam_size=settings.whisper_beam_size,
                    condition_on_previous_text=False,
                )
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
    # Pontuação que o Piper converte em PAUSA/entonação mas que o _ALLOWED descartaria:
    # troca por equivalente permitido em vez de sumir (reticências = pausa longa; ponto-
    # e-vírgula, dois-pontos e travessão = respiro de vírgula). Descartar deixava a
    # fala monótona e emendada — a pontuação É o canal de prosódia do Piper.
    _PAUSAS = (("…", "..."), ("—", ","), ("–", ","), (";", ","), (":", ","))
    # Dígitos exigem leitura própria ANTES da troca genérica acima: "14:30" com vírgula
    # viraria decimal falado ("quatorze vírgula trinta") e, com o strip antigo, virava
    # milhar ("1430"). "14 e 30" é a leitura natural de horário em PT-BR — e atinge as
    # falas mais frequentes do assistente (hora_atual, confirmações de agenda). Mesmo
    # racional para intervalo com traço: "10–15" lê "10 a 15".
    _HORA = re.compile(r"(\d):(\d)")
    _INTERVALO = re.compile(r"(\d)\s*[–—]\s*(\d)")

    def __init__(self) -> None:
        self._voice = None
        # Prosódia (naturalidade): montado UMA vez no load() a partir dos knobs
        # MENTE_TTS_*; None = síntese com os defaults treinados da voz (histórico).
        self._syn_config = None
        # CACHE DE VOZ (#1): a síntese Piper é determinística para o mesmo texto, então
        # frases RECORRENTES (fillers, confirmações, status, "Pronto.") não precisam ser
        # re-sintetizadas — TTFA≈0 na segunda vez em diante. LRU limitado por
        # `tts_cache_size` (base64 é leve). Vive no event loop (leitura/escrita fora do
        # to_thread), então não precisa de lock.
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        self._cache_hits = 0

    def load(self) -> None:
        """Síncrono — chamar via asyncio.to_thread no startup."""
        try:
            from piper.voice import PiperVoice

            self._voice = PiperVoice.load(settings.caminho_voz_piper)
            # Só monta o SynthesisConfig se algum knob foi setado: com todos None, a
            # síntese segue idêntica ao histórico (e os fakes dos testes, que não
            # conhecem o kwarg syn_config, continuam válidos).
            if any(v is not None for v in (
                settings.tts_noise_scale, settings.tts_noise_w_scale, settings.tts_length_scale,
            )):
                from piper.config import SynthesisConfig

                self._syn_config = SynthesisConfig(
                    noise_scale=settings.tts_noise_scale,
                    noise_w_scale=settings.tts_noise_w_scale,
                    length_scale=settings.tts_length_scale,
                )
                telemetry.track(
                    "PIPER",
                    f"Prosódia: noise={settings.tts_noise_scale} "
                    f"noise_w={settings.tts_noise_w_scale} length={settings.tts_length_scale}",
                )
            telemetry.track("PIPER", "Voz Cadu carregada (CPU).")
        except Exception as exc:
            telemetry.error("PIPER", "Falha ao carregar Piper", exc)

    @property
    def ready(self) -> bool:
        return self._voice is not None

    def _normalizar(self, texto: str) -> str:
        texto = self._STRIP_MD.sub("", texto)
        texto = self._HORA.sub(r"\1 e \2", texto)
        texto = self._INTERVALO.sub(r"\1 a \2", texto)
        for de, para in self._PAUSAS:
            texto = texto.replace(de, para)
        for ing, pt in DICIONARIO_FONETICO.items():
            texto = re.sub(rf"\b{re.escape(ing)}\b", pt, texto, flags=re.IGNORECASE)
        texto = self._ALLOWED.sub("", texto)
        return re.sub(r"\s{2,}", " ", texto).strip()

    async def synth_base64(self, texto: str) -> Optional[str]:
        """Sintetiza uma frase em WAV base64. None se vazio ou indisponível.

        Cache de voz (#1): frase recorrente já sintetizada volta na hora, sem tocar a
        CPU do Piper (TTFA≈0). A chave é o texto JÁ normalizado — 'Pronto!' e 'pronto'
        batem no mesmo áudio."""
        if self._voice is None or not texto.strip():
            return None
        texto_voz = self._normalizar(texto)
        if not texto_voz:
            return None
        cached = self._cache.get(texto_voz)
        if cached is not None:
            self._cache.move_to_end(texto_voz)   # LRU: marca como recém-usado
            self._cache_hits += 1
            return cached
        try:
            def _run() -> bytes:
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(self._voice.config.sample_rate)
                    # syn_config só quando configurado — o caminho None preserva a
                    # assinatura antiga (fakes de teste sem o kwarg).
                    chunks = (
                        self._voice.synthesize(texto_voz, syn_config=self._syn_config)
                        if self._syn_config is not None
                        else self._voice.synthesize(texto_voz)
                    )
                    for chunk in chunks:
                        w.writeframes(chunk.audio_int16_bytes)
                return buf.getvalue()

            data = await asyncio.to_thread(_run)
            b64 = base64.b64encode(data).decode("utf-8")
            self._cache[texto_voz] = b64
            while len(self._cache) > settings.tts_cache_size:
                self._cache.popitem(last=False)   # descarta o menos recente
            return b64
        except Exception as exc:
            telemetry.error("PIPER", "Erro ao sintetizar áudio", exc)
            return None
