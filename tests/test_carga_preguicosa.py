"""CARGA PREGUIÇOSA DO TTS (2026-07-29).

O XTTS custa ~17 s de boot e ~1,4 GB de VRAM na 3080. Desde o portão de fala
(`state.turno_falado`), uma sessão só de TEXTO nunca o usa — então o `main.py`
deixou de carregá-lo no startup. Quem o acorda é o microfone abrindo.

O risco que estes testes guardam: duas inicializações concorrentes no mesmo
contexto CUDA são a classe de bug que já envenenou a GPU deste projeto (o
device-side assert de 2026-07-23). Antes o load era único e no boot; agora o
gatilho é externo, então concorrência deixou de ser hipótese.
"""
import threading
import time

import numpy as np
import pytest

from mente_digital.agent import Agent
from mente_digital.audio import TtsService
from mente_digital.config import settings
from mente_digital.state import AppContext, turno_falado
from mente_digital.tts_xtts import XttsService

from conftest import FakeLlama, make_send


class _TtsFrio:
    """Sobe só quando mandam — como o XTTS real depois desta mudança."""

    def __init__(self) -> None:
        self.ready = False
        self.chamadas = []
        self.carregou = 0

    def ensure_loaded(self) -> bool:
        self.carregou += 1
        self.ready = True
        return True

    async def synth_base64(self, texto):
        self.chamadas.append(texto)
        return None

    def cancel(self) -> None:
        pass


# --- o ensure_loaded do XTTS -------------------------------------------------
def _fases(monkeypatch, svc, cpu_ok=True, lento=False):
    """Substitui as DUAS fases reais e devolve o diário de chamadas.

    Remendar `load()` não bastaria: desde a divisão em fases, `ensure_loaded` passa por
    `_construir_cpu`/`_ativar` — e um teste que remendasse só o `load` carregaria o
    XTTS DE VERDADE (a suíte foi de 18s para 53s antes de eu perceber).
    """
    diario = []

    def _cpu():
        diario.append("cpu")
        if lento:
            time.sleep(0.05)             # janela larga para o outro thread entrar
        if cpu_ok:
            svc._modelo_pronto = object()

    def _gpu():
        diario.append("gpu")
        svc._model = object()

    monkeypatch.setattr(svc, "_construir_cpu", _cpu)
    monkeypatch.setattr(svc, "_ativar", _gpu)
    return diario


def test_carrega_uma_vez_so(monkeypatch):
    svc = XttsService()
    diario = _fases(monkeypatch, svc)

    assert svc.ensure_loaded() is True
    assert svc.ensure_loaded() is True

    assert diario == ["cpu", "gpu"]


def test_preparar_ram_NAO_ativa_o_device(monkeypatch):
    """O ponto da fase 1: 19,3s dos 20,3s do load são CPU e não tocam a VRAM."""
    svc = XttsService()
    diario = _fases(monkeypatch, svc)

    assert svc.preparar_ram() is True

    assert diario == ["cpu"]
    assert svc.ready is False            # nada na GPU ainda


def test_ativar_depois_do_boot_nao_refaz_a_fase_cara(monkeypatch):
    """É o ganho todo: o microfone paga só o ~1s do device."""
    svc = XttsService()
    diario = _fases(monkeypatch, svc)
    svc.preparar_ram()                   # como no boot, em segundo plano
    diario.clear()

    assert svc.ensure_loaded() is True

    assert diario == ["gpu"]


def test_unload_devolve_o_modelo_a_RAM_em_vez_de_destruir(monkeypatch):
    """A Fase 3 do OCR libera a GPU; antes disso o `unload` jogava fora também a
    cópia em RAM, e a voz seguinte pagava os 20s de novo."""
    svc = XttsService()
    diario = _fases(monkeypatch, svc)
    svc.ensure_loaded()
    diario.clear()

    svc.unload()
    assert svc.ready is False
    assert svc._modelo_pronto is not None    # continua montado, na RAM

    assert svc.ensure_loaded() is True
    assert diario == ["gpu"]                 # reativou sem reconstruir


def test_gatilhos_SIMULTANEOS_nao_inicializam_duas_vezes(monkeypatch):
    """O gatilho virou externo (microfone), então dois quadros de áudio ou duas
    sessões podem pedir o load ao mesmo tempo. Duas inicializações XTTS no mesmo
    contexto CUDA corrompem a GPU do processo inteiro."""
    svc = XttsService()
    diario = _fases(monkeypatch, svc, lento=True)

    threads = [threading.Thread(target=svc.ensure_loaded) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert diario == ["cpu", "gpu"]
    assert svc.ready is True


def test_falha_no_load_nao_vira_tempestade_de_retry(monkeypatch):
    """Sem a marca de falha, um modelo quebrado faria CADA frase tentar carregar
    de novo — no hot path da resposta."""
    svc = XttsService()
    diario = _fases(monkeypatch, svc, cpu_ok=False)

    assert svc.ensure_loaded() is False
    assert svc.ensure_loaded() is False
    assert svc.ensure_loaded() is False

    assert diario == ["cpu"]             # tentou UMA vez, e nunca chegou ao device


def test_piper_pronto_nao_recarrega(monkeypatch):
    """O Piper é CPU e segue carregando no boot; o `ensure_loaded` dele existe só
    para unificar o contrato (mesmo motivo do `cancel()` no-op)."""
    svc = TtsService()
    svc._voice = object()
    monkeypatch.setattr(svc, "load", lambda: pytest.fail("não devia recarregar"))

    assert svc.ensure_loaded() is True


# --- quem espera o load ------------------------------------------------------
@pytest.mark.asyncio
async def test_falar_espera_o_tts_subir_antes_de_sintetizar():
    """Sem esta espera a 1ª resposta falada sairia MUDA: `synth_base64` num serviço
    não-pronto devolve vazio em silêncio."""
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama([])
    ctx.tts = _TtsFrio()
    agent = Agent(ctx)
    send, _ = make_send()

    token = turno_falado.set(True)
    try:
        await agent._falar(send, ["Uma frase."])
    finally:
        turno_falado.reset(token)

    assert ctx.tts.carregou == 1
    assert ctx.tts.chamadas == ["Uma frase."]


@pytest.mark.asyncio
async def test_turno_mudo_nem_carrega_o_modelo():
    """O ponto da mudança: sessão só de texto não paga os 17s nem a VRAM."""
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama([])
    ctx.tts = _TtsFrio()
    agent = Agent(ctx)
    send, _ = make_send()

    token = turno_falado.set(False)
    try:
        await agent._falar(send, ["Uma frase."])
    finally:
        turno_falado.reset(token)

    assert ctx.tts.carregou == 0
    assert ctx.tts.ready is False


# --- o gatilho: o microfone ---------------------------------------------------
class _WsFake:
    async def send_json(self, data):
        return None


class _LlamaPronto:
    """O religar do LLM mora na MESMA transição silêncio→fala. Já pronto aqui, para
    o contador de tarefas medir só o aquecimento do TTS."""

    ready = True


def _frame_alto() -> bytes:
    return (np.ones(1600, dtype=np.int16) * 8000).tobytes()


def _sessao(monkeypatch):
    from mente_digital.ws import LiveSession

    monkeypatch.setattr(settings, "mestre_wake", False)
    ctx = AppContext(settings=settings)
    ctx.llama = _LlamaPronto()
    ctx.tts = _TtsFrio()
    s = LiveSession(ctx, _WsFake())
    monkeypatch.setattr(ctx, "track_task",
                        lambda coro: (getattr(coro, "close", lambda: None)(), None)[1])
    return s


def test_inicio_de_fala_aquece_o_tts_uma_unica_vez(monkeypatch):
    """O instante mais cedo em que se sabe que haverá resposta FALADA. Daqui até a
    1ª síntese ainda vêm o resto da fala, o endpoint do VAD, o STT e o prefill."""
    s = _sessao(monkeypatch)
    pedidos = []
    monkeypatch.setattr(s.ctx, "track_task",
                        lambda coro: (pedidos.append(1), coro.close())[1])

    for _ in range(5):
        s._on_audio(_frame_alto())

    assert len(pedidos) == 1        # e não um por quadro de áudio


def test_tts_ja_pronto_nao_aquece(monkeypatch):
    s = _sessao(monkeypatch)
    s.ctx.tts.ready = True
    pedidos = []
    monkeypatch.setattr(s.ctx, "track_task",
                        lambda coro: (pedidos.append(1), coro.close())[1])

    s._on_audio(_frame_alto())

    assert pedidos == []
