"""
O watcher de economia DENTRO do scheduler — a parte que age, não a que decide.

Separado de `test_standby.py` (que cobre a decisão pura) porque o que se prova
aqui é outra coisa: que o tick chama, que os vetos vindos do mundo real (sessão
respondendo, ETL rodando) chegam à decisão, e — o mais importante — que dormir
AVISA quem está conectado. Um standby silencioso deixa a página acreditando que o
servidor está ligado, e a pergunta seguinte sai com o RAG cego.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from mente_digital import energia, telemetry
from mente_digital.scheduler import SchedulerService
from conftest import FakeTts


class FakeSessao:
    def __init__(self) -> None:
        self.recebidos: list[dict] = []

    async def safe_send(self, data: dict) -> bool:
        self.recebidos.append(data)
        return True


class CtxEconomia:
    """O mínimo que o watcher toca. Duck-typed, como todo fake deste projeto."""

    def __init__(self, sessoes=()) -> None:
        self.sessoes = set(sessoes)
        self.tts = FakeTts()
        self.web = None
        self.llama = None
        self.interactive_idle = asyncio.Event()
        self.interactive_idle.set()
        self.descansando = False
        self.idle_em_andamento = False
        self.ultima_interacao = time.monotonic()
        self.liberou = 0
        self.tarefas: list = []

    def segundos_sem_uso(self, agora=None) -> float:
        return max(0.0, (agora if agora is not None else time.monotonic()) - self.ultima_interacao)

    async def aguardar_ocio(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.interactive_idle.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def liberar_vram(self):
        self.liberou += 1
        return {"llama", "stt", "tts", "embeddings"}

    def track_task(self, coro):
        tarefa = asyncio.ensure_future(coro)
        self.tarefas.append(tarefa)
        return tarefa


@pytest.fixture(autouse=True)
def _sem_medicao_real(monkeypatch):
    """Medir VRAM/RAM de verdade num teste dependeria da máquina. O que importa
    aqui é o fluxo; os números têm testes próprios em test_standby.py."""
    monkeypatch.setattr(energia, "medir", lambda: {"vram_mb": 5200, "ram_commit_mb": 7000})
    monkeypatch.setattr(energia, "enxugar", lambda: None)


@pytest.fixture(autouse=True)
def _db_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "economia.db"))
    telemetry.db.init()


def _com_minutos(monkeypatch, minutos: int) -> None:
    from mente_digital import scheduler as mod

    monkeypatch.setattr(mod.settings, "idle_standby_minutos", minutos)


async def _esperar_tarefas(ctx) -> None:
    if ctx.tarefas:
        await asyncio.gather(*ctx.tarefas)


async def test_tick_dorme_e_avisa_as_sessoes(monkeypatch):
    """O caminho feliz inteiro: 20 min parado -> solta a VRAM -> a página recebe
    o novo estado. O aviso é a metade que não pode faltar."""
    _com_minutos(monkeypatch, 20)
    sessao = FakeSessao()
    ctx = CtxEconomia([sessao])
    ctx.ultima_interacao -= 21 * 60
    sched = SchedulerService(ctx)

    await sched.tick()
    await _esperar_tarefas(ctx)

    assert ctx.liberou == 1
    assert ctx.descansando is True
    energias = [m for m in sessao.recebidos if m.get("tipo") == "energia"]
    assert energias and energias[0]["estado"] == "descansando"
    assert energias[0]["medida"]["vram_mb"] == 5200


async def test_nao_dorme_com_pergunta_em_voo(monkeypatch):
    _com_minutos(monkeypatch, 20)
    ctx = CtxEconomia()
    ctx.ultima_interacao -= 60 * 60
    ctx.interactive_idle.clear()             # decode em curso
    sched = SchedulerService(ctx)

    await sched.tick()
    await _esperar_tarefas(ctx)

    assert ctx.liberou == 0
    assert ctx.descansando is False


async def test_nao_dorme_com_etl_rodando(monkeypatch):
    """O ETL do idle não limpa `interactive_idle` (ele só ESPERA por ele), então
    sem a flag própria o watcher soltaria o modelo debaixo de uma atomização."""
    _com_minutos(monkeypatch, 20)
    ctx = CtxEconomia()
    ctx.ultima_interacao -= 60 * 60
    ctx.idle_em_andamento = True
    sched = SchedulerService(ctx)

    await sched.tick()
    await _esperar_tarefas(ctx)

    assert ctx.liberou == 0


async def test_sessao_conectada_nao_impede(monkeypatch):
    """A régua deste watcher é ATIVIDADE, não presença de sessão — e é o que o
    separa de todo o resto do trabalho de fundo. Com a régua da sessão ele nunca
    dispararia, porque a janela do dono fica aberta o dia inteiro."""
    _com_minutos(monkeypatch, 20)
    sessao = FakeSessao()
    ctx = CtxEconomia([sessao])
    ctx.ultima_interacao -= 21 * 60
    sched = SchedulerService(ctx)

    await sched.tick()
    await _esperar_tarefas(ctx)

    assert ctx.descansando is True


async def test_desligado_por_configuracao_nunca_dorme(monkeypatch):
    _com_minutos(monkeypatch, 0)
    ctx = CtxEconomia()
    ctx.ultima_interacao -= 48 * 3600
    sched = SchedulerService(ctx)

    await sched.tick()
    await _esperar_tarefas(ctx)

    assert ctx.liberou == 0


async def test_nao_dorme_duas_vezes_em_ticks_seguidos(monkeypatch):
    """Anti-sobreposição: `liberar_vram` leva segundos e o tick se repete."""
    _com_minutos(monkeypatch, 20)
    ctx = CtxEconomia()
    ctx.ultima_interacao -= 21 * 60
    sched = SchedulerService(ctx)

    await sched.tick()
    await _esperar_tarefas(ctx)
    await sched.tick()
    await _esperar_tarefas(ctx)

    assert ctx.liberou == 1


async def test_conversa_retomada_entre_o_tick_e_a_acao_aborta_o_standby(monkeypatch):
    """A corrida que sobra: o `tick` checou "ninguém está usando" e despachou a
    task; entre uma coisa e outra alguém mandou uma pergunta. Sem a segunda
    checagem, o standby soltaria os modelos por cima dela."""
    _com_minutos(monkeypatch, 20)
    ctx = CtxEconomia()
    ctx.ultima_interacao -= 21 * 60
    sched = SchedulerService(ctx)

    # O tick decide dormir com a GPU livre...
    assert sched._standby_devido() is True
    sched._standby_em_andamento = True
    # ...e a conversa recomeça antes de a task rodar.
    ctx.interactive_idle.clear()
    await sched._executar_standby()

    assert ctx.liberou == 0
    assert ctx.descansando is False
    assert sched._standby_em_andamento is False     # a flag não vaza


async def test_avisar_energia_sobrevive_a_sessao_morrendo(monkeypatch):
    """Uma sessão que explode ao receber não pode impedir as outras de saber."""
    _com_minutos(monkeypatch, 20)

    class SessaoQuebrada:
        async def safe_send(self, data):
            raise RuntimeError("socket fechado")

    boa = FakeSessao()
    ctx = CtxEconomia([SessaoQuebrada(), boa])
    sched = SchedulerService(ctx)

    await sched.avisar_energia("descansando", {"vram_mb": 400})

    assert [m["tipo"] for m in boa.recebidos] == ["energia"]
