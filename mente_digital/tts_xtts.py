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
import threading
import wave
from collections import OrderedDict
from typing import Optional

from mente_digital import vram
from mente_digital.config import settings
from mente_digital.telemetry import telemetry
from mente_digital.textutils import sem_embeds_de_imagem
from mente_digital.verbalizar import verbalizar

# Só tira a marcação Markdown (o XTTS lê pontuação/acentos nativamente — ao contrário do
# Piper, não precisa do _ALLOWED que apagava $ % ° etc.). Números viram palavra via verbalizar.
_STRIP_MD = re.compile(r"[\[\]*#`]")

# Fronteira de sentença: fim de frase (. ! ? …) OU ; — mantém o pontuador no pedaço.
_FIM_SENTENCA = re.compile(r"(?<=[.!?…;])\s+")

# ANTI-CRASH (device-side assert): emoji, pictogramas, dingbats e seletores de variação
# NÃO são fala e podem mapear para um id fora do vocab do tokenizer do XTTS -> índice fora
# do range no embedding do GPT-2 -> device-side assert que CORROMPE o contexto CUDA do
# processo (derruba o LLM/Whisper junto). O Piper filtra tudo pelo _ALLOWED; o XTTS lê
# texto rico (acento, pontuação, e $ % ° já viram palavra no verbalizar), mas estes blocos
# não têm leitura — some com eles. Aplicado DEPOIS do verbalizar, então aqui só resta fala.
_SANITIZAR = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji, pictogramas e símbolos suplementares
    "\U00002600-\U000027BF"   # dingbats & símbolos diversos
    "\U00002B00-\U00002BFF"   # setas e símbolos diversos
    "\U00002190-\U000021FF"   # setas
    "\U0000FE00-\U0000FE0F"   # seletores de variação (VS1–VS16)
    "\U0000200D"              # zero-width joiner (emoji ZWJ)
    "]"
)


def dividir_para_xtts(texto: str, limite: int) -> list[str]:
    """Fatia o texto em pedaços com <= `limite` chars ANTES de mandar ao GPT-2 do XTTS.

    PORQUÊ: o GPT-2 interno tem teto de posições (gpt_max_audio_tokens ~605/14s). Uma frase
    longa gera audio-tokens além do teto e o índice de posição estoura -> device-side assert
    que corrompe o contexto CUDA do processo (derruba o LLM junto). O corte é a rede de
    segurança na fronteira do XTTS, independente do que o pipeline entregou. Puro/testável.

    Corta primeiro em fronteira de SENTENÇA; sentença que ainda passe do limite é quebrada em
    fronteira de PALAVRA; palavra única gigante (URL etc.) é cortada no limite. limite<=0 = sem
    corte (devolve o texto inteiro num pedaço)."""
    texto = texto.strip()
    if not texto or limite <= 0 or len(texto) <= limite:
        return [texto] if texto else []
    pedacos: list[str] = []
    atual = ""
    for sentenca in _FIM_SENTENCA.split(texto):
        if not sentenca:
            continue
        # Cabe junto do que já acumulou? Junta (menos passes = melhor).
        candidato = f"{atual} {sentenca}".strip() if atual else sentenca
        if len(candidato) <= limite:
            atual = candidato
            continue
        if atual:
            pedacos.append(atual)
            atual = ""
        if len(sentenca) <= limite:
            atual = sentenca
            continue
        # Sentença sozinha estoura o limite -> quebra por palavra.
        for palavra in sentenca.split():
            if len(palavra) > limite:                # palavra única > limite (URL etc.)
                if atual:
                    pedacos.append(atual)
                    atual = ""
                while len(palavra) > limite:         # fatia até caber (não sobra resto grande)
                    pedacos.append(palavra[:limite])
                    palavra = palavra[limite:]
                atual = palavra
                continue
            cand = f"{atual} {palavra}".strip() if atual else palavra
            if len(cand) <= limite:
                atual = cand
            else:
                pedacos.append(atual)
                atual = palavra
    if atual:
        pedacos.append(atual)
    return pedacos


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
        self._autocast_fp16 = False
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        # BARGE-IN + ANTI-CRASH. Duas peças moram aqui:
        #
        # (1) CANCELAMENTO por TOKEN DE GERAÇÃO (`_gen`, não um Event compartilhado): a
        #     síntese pesada roda em asyncio.to_thread, então cancelar a CORROTINA que a
        #     espera NÃO para a thread — ela segue gerando chunks (5-6s/frase). `cancel()`
        #     INCREMENTA `_gen` do event loop; cada `_run` captura seu gen ao ser despachado
        #     e ABORTA assim que `_gen` mudar. O Event antigo tinha um clear() no início de
        #     synth_base64 que RESSUSCITAVA uma thread órfã (des-cancelava quem já devia
        #     parar) — a corrida que deixava duas sínteses vivas. O contador só AVANÇA, então
        #     a órfã morre e a nova nasce válida sem clear().
        #
        # (2) LOCK DE INFERÊNCIA (`_infer_lock`) serializando o modelo: duas inferências XTTS
        #     concorrentes no MESMO contexto CUDA corrompem o estado do GPT-2 -> device-side
        #     assert que ENVENENA a GPU do processo (derruba LLM + Whisper + app). Barge-in em
        #     rajada (turno cortando turno) deixava a thread órfã e a nova rodando JUNTAS; o
        #     lock garante ESTRUTURALMENTE uma de cada vez. O gen-token (1) faz a órfã largar
        #     o lock rápido (aborta no próximo chunk), então a espera da nova é curta.
        self._gen = 0
        self._gen_lock = threading.Lock()      # protege o incremento de _gen
        self._infer_lock = threading.Lock()    # 1 inferência XTTS por vez (anti-corrupção CUDA)

    def cancel(self) -> None:
        """Sinaliza à síntese em voo (thread do _run) para abortar. Chamável do event loop
        no barge-in — barato (só incrementa `_gen`); a thread vê a mudança e para nos laços
        do _run. Não afeta o PRÓXIMO turno: a nova síntese captura o gen JÁ incrementado, logo
        nasce válida — sem o clear() que ressuscitava a thread órfã do turno anterior."""
        with self._gen_lock:
            self._gen += 1

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
            model.eval()
            # fp16 = MIXED PRECISION via autocast, NÃO model.half(): o XTTS QUEBRA com
            # pesos half ("expected scalar type Float but found Half" no layer_norm do GPT).
            # O autocast casta só as ops seguras p/ fp16 e mantém layer_norm/softmax em fp32.
            # (Consequência: os PESOS ficam em fp32 — o ganho é de compute/ativação, não a
            # metade da VRAM dos pesos; o XTTS não suporta pesos half de forma estável.)
            self._autocast_fp16 = settings.tts_xtts_fp16 and device.startswith("cuda")

            gpt_cond, spk_emb = self._resolver_speaker(model)

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
                f"XTTS-v2 carregado ({device}, fp16={self._autocast_fp16}, "
                f"sr={self._sample_rate}).",
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

    def unload(self) -> None:
        """Solta o XTTS da VRAM (Fase 3: GPU limpa para o OCR em outro processo).
        Cancela a geração em voo antes — descarregar por baixo de uma síntese foi
        exatamente o que causou o device-side assert de 2026-07-23."""
        if self._model is None:
            return
        self.cancel()
        with self._infer_lock:
            self._model = None
            self._latentes = None
        vram.liberar_cache_gpu()

    def _preparar(self, texto: str) -> str:
        """Modelagem de texto: verbaliza números e tira Markdown. SEM o filtro _ALLOWED
        do Piper — o XTTS foneiza acentos/símbolos nativamente. Puro/testável sem modelo."""
        # ANTES do _STRIP_MD, pelo mesmo motivo do Piper: sem isto o embed de figura
        # vira caminho de arquivo e o modelo o LÊ. Ver sem_embeds_de_imagem.
        texto = sem_embeds_de_imagem(texto)
        texto = _STRIP_MD.sub("", texto)
        texto = verbalizar(texto)
        texto = _SANITIZAR.sub(" ", texto)   # tira emoji/símbolo sem leitura (anti-assert)
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
        # Captura a geração CORRENTE (já refletindo qualquer cancel anterior). Se um cancel
        # (barge-in) chegar DURANTE esta síntese, `_gen` muda e a thread do _run aborta; não
        # há clear() para ressuscitar a thread de um turno passado — o contador só avança.
        meu_gen = self._gen
        try:
            data = await asyncio.to_thread(self._run, texto_voz, meu_gen)
            b64 = base64.b64encode(data).decode("utf-8")
            # Não cacheia o WAV PARCIAL de uma síntese cancelada (barge-in): congelaria uma
            # "tomada" truncada para aquele texto. Só a síntese que terminou inteira entra.
            if settings.tts_xtts_cache_enabled and self._gen == meu_gen:
                self._cache[texto_voz] = b64
                while len(self._cache) > settings.tts_cache_size:
                    self._cache.popitem(last=False)
            return b64
        except Exception as exc:
            telemetry.error("XTTS", "Erro ao sintetizar (XTTS)", exc)
            return None

    def _run(self, texto_voz: str, meu_gen: int) -> bytes:
        """Streaming interno do XTTS -> um WAV (mono, 16-bit, sample_rate no cabeçalho)."""
        import contextlib

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
        # autocast fp16 (mixed precision) SÓ com modelo real na GPU. Nos testes (modelo
        # falso, sem torch) usa nullcontext — nada de importar torch no caminho mockado.
        if getattr(self, "_autocast_fp16", False):
            import torch

            ctx = torch.autocast("cuda", dtype=torch.float16)
        else:
            ctx = contextlib.nullcontext()
        # Corte de segurança: nenhum pedaço passa do teto do GPT-2 interno (ver dividir_para_xtts).
        partes = dividir_para_xtts(texto_voz, settings.tts_xtts_max_chars_frase)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self._sample_rate)
            # SERIALIZAÇÃO ANTI-CRASH: uma inferência XTTS por vez no modelo/contexto CUDA.
            # Sem isto, a thread órfã de um turno cortado e a nova rodavam JUNTAS -> estado
            # do GPT-2 corrompido -> device-side assert que envenena a GPU. A órfã (gen
            # mudou) aborta no laço abaixo e larga o lock rápido, então a nova espera pouco.
            with self._infer_lock:
                with ctx:
                    for parte in partes:                 # sub-frases já abaixo do limite
                        # Checado no TOPO: cancelado/substituído (gen mudou) sai sem iniciar
                        # nova frase (barge-in entre sub-frases).
                        if self._gen != meu_gen:
                            break
                        chunks = self._model.inference_stream(
                            parte, settings.tts_xtts_language,
                            self._gpt_cond_latent, self._speaker_embedding, **kw,
                        )
                        for ch in chunks:                # torch float @ sample_rate em [-1,1]
                            # Checar SÓ entre partes não basta: o inference_stream de UMA
                            # frase dura 5-6s. Checar por chunk aborta no meio — é o que faz
                            # a fala parar de fato no barge-in, não só após a frase corrente.
                            if self._gen != meu_gen:
                                break
                            w.writeframes(_to_int16(ch))
        # Cancelado ou não, o `with wave.open` fecha e escreve o cabeçalho correto para os
        # frames JÁ gravados (WAV parcial válido) — só demos break, nunca retornamos de
        # dentro do `with`. Nada escrito = WAV vazio válido. O chamador trata normalmente.
        return buf.getvalue()
