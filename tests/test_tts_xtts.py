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
from mente_digital.tts_xtts import XttsService, _to_int16, dividir_para_xtts


class _FakeXttsModel:
    """Modelo XTTS falso: inference_stream faz yield de dois chunks numpy float32."""

    def inference_stream(self, texto, lang, gpt, spk, **kw):
        yield np.array([0.0, 0.5, -0.5], dtype="float32")
        yield np.array([0.25, -0.25], dtype="float32")


class _FakeXttsModelContador:
    """Registra cada texto recebido; faz yield de 1 amostra por chamada (p/ contar frames)."""

    def __init__(self) -> None:
        self.textos: list[str] = []

    def inference_stream(self, texto, lang, gpt, spk, **kw):
        self.textos.append(texto)
        yield np.array([0.1], dtype="float32")


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


# -- split de segurança contra o estouro do GPT-2 interno (device-side assert) -
def test_dividir_texto_curto_fica_inteiro():
    assert dividir_para_xtts("Oi, tudo bem?", 200) == ["Oi, tudo bem?"]


def test_dividir_vazio_e_sem_corte():
    assert dividir_para_xtts("   ", 200) == []
    inteiro = "a " * 300                                   # 600 chars
    assert dividir_para_xtts(inteiro, 0) == [inteiro.strip()]  # limite<=0 = sem corte


def test_dividir_corta_em_fronteira_de_sentenca_respeitando_limite():
    texto = "Primeira frase aqui. Segunda frase aqui. Terceira frase aqui aqui."
    partes = dividir_para_xtts(texto, 25)
    assert all(len(p) <= 25 for p in partes)
    assert "".join(partes.copy())                          # não perde conteúdo
    # cada pedaço é uma sentença (ou junção) — nenhuma foi partida no meio de palavra
    assert partes[0] == "Primeira frase aqui."


def test_dividir_sentenca_longa_quebra_por_palavra():
    # Uma única sentença SEM pontuação, acima do limite -> quebra por palavra.
    texto = "palavra " * 20                                 # 160 chars, sem fim de sentença
    partes = dividir_para_xtts(texto.strip(), 30)
    assert len(partes) > 1
    assert all(len(p) <= 30 for p in partes)


def test_dividir_palavra_gigante_e_cortada_no_limite():
    partes = dividir_para_xtts("x" * 250, 100)
    assert all(len(p) <= 100 for p in partes)
    assert "".join(partes) == "x" * 250                     # nada some


async def test_synth_frase_longa_faz_varias_chamadas_e_concatena(monkeypatch):
    """Frase acima do teto vira N chamadas ao inference_stream, num único WAV — sem
    isto o GPT-2 interno estouraria as posições e dispararia o device-side assert."""
    monkeypatch.setattr(settings, "tts_xtts_max_chars_frase", 20)
    fake = _FakeXttsModelContador()
    x = XttsService()
    x._model = fake
    x._sample_rate = 24000
    b64 = await x.synth_base64("Uma frase. Outra frase. Mais uma frase aqui.")
    assert b64
    assert len(fake.textos) >= 3                            # fatiou em >= 3 pedaços
    assert all(len(t) <= 20 for t in fake.textos)
    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
        assert w.getnframes() == len(fake.textos)           # 1 amostra por chamada, concatenadas


# -- barge-in: síntese XTTS cancelável (thread checa o Event) -------------------
class _FakeXttsModelMuitosChunks:
    """Faz yield de `total` chunks. Se `service` for dado, aciona service.cancel() logo
    ANTES de emitir o chunk de índice `corta_apos` — simula o barge-in chegando NO MEIO
    da síntese (a thread do _run ainda gerando chunks daquela frase)."""

    def __init__(self, service=None, total=6, corta_apos=2):
        self._service = service
        self._total = total
        self._corta_apos = corta_apos

    def inference_stream(self, texto, lang, gpt, spk, **kw):
        for i in range(self._total):
            if self._service is not None and i == self._corta_apos:
                self._service.cancel()               # barge-in chega agora
            yield np.array([0.1], dtype="float32")


def _nframes_wav(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnframes()


def _nframes_b64(b64: str) -> int:
    return _nframes_wav(base64.b64decode(b64))


async def test_cancel_no_meio_para_cedo_e_gera_wav_valido():
    """Barge-in durante a síntese: o _run vê o Event no laço interno e para — o WAV sai
    válido com MENOS frames que o total (o áudio não gerado não continua tocando)."""
    x = XttsService()
    x._sample_rate = 24000
    # corta_apos=2: chunks 0 e 1 entram; ao chegar no 2, o Event já está setado -> break.
    x._model = _FakeXttsModelMuitosChunks(service=x, total=6, corta_apos=2)
    b64 = await x.synth_base64("olá mundo")
    assert b64                                        # WAV parcial ainda é válido/base64
    n = _nframes_b64(b64)
    assert 0 < n < 6                                  # parou cedo: nem todos os chunks entraram
    assert n == 2


def test_cancel_no_topo_do_laco_gera_wav_vazio_valido():
    """Cancel setado ANTES de sintetizar: o laço externo do _run quebra no topo e o WAV
    sai vazio, porém válido (cabeçalho fechado pelo `with wave.open`, sem exceção).
    Chama _run direto para pular o clear() do synth_base64 e exercitar o guard do laço."""
    x = XttsService()
    x._sample_rate = 24000
    x._model = _FakeXttsModelMuitosChunks(total=6)   # sem auto-cancel
    x._cancelar.set()
    data = x._run("olá")                              # nada escrito, mas WAV válido
    assert _nframes_wav(data) == 0


async def test_novo_synth_limpa_cancel_anterior():
    """clear() no início do synth_base64: um cancel de um turno NÃO gruda no próximo —
    após cancelar, uma nova síntese (sem barge-in) sintetiza TUDO normalmente."""
    x = XttsService()
    x._sample_rate = 24000
    x._model = _FakeXttsModel()                       # 3 + 2 = 5 frames
    x.cancel()                                        # cauda de cancel do "turno anterior"
    b64 = await x.synth_base64("olá mundo")           # synth_base64 dá clear() no início
    assert b64
    assert _nframes_b64(b64) == 5                     # não ficou grudado cancelado


def test_tts_piper_cancel_e_noop():
    # Contrato unificado: o barge-in chama tts.cancel() sem checar o tipo do engine.
    t = TtsService()
    assert t.cancel() is None                         # Piper: no-op, não levanta


# -- default seguro no código (ignora o .env local) ----------------------------
def test_engine_default_e_piper():
    assert Settings(_env_file=None).tts_engine == "piper"
