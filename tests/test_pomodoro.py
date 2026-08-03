"""
Pomodoro (#19, Onda 3): ciclo foco/pausa anunciado por voz. Um agendamento tipo 'pomodoro'
alterna as fases no SchedulerService (payload {fase}); "mestre, inicia/para o pomodoro"
liga/desliga. Sem GPU/rede: db temporário + fakes.
"""
import asyncio
import time
from datetime import datetime, timedelta

from mente_digital import mestre
from mente_digital import telemetry
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.scheduler import SchedulerService
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


def test_pomodoro_detectores():
    assert mestre.comando_pomodoro_iniciar("inicia um pomodoro")
    assert mestre.comando_pomodoro_iniciar("modo foco")
    assert mestre.comando_pomodoro_parar("para o pomodoro")
    assert not mestre.comando_pomodoro_iniciar("o que é RAG")


# --- criar/parar via Agent --------------------------------------------------
class _FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "pom.db"))
    telemetry.db.init()
    ctx = AppContext(settings=settings)
    ctx.llama, ctx.tts, ctx.vectorstore = FakeLlama(["ok."]), FakeTts(), _FakeVS()
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    return Agent(ctx), SessionMemory(settings)


async def test_inicia_e_para_pomodoro(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    await agent.pipeline_resposta("mestre, inicia um pomodoro", send, mem)
    assert len(telemetry.db.listar_agendamentos(("pomodoro",))) == 1
    await agent.pipeline_resposta("mestre, para o pomodoro", send, mem)
    assert telemetry.db.listar_agendamentos(("pomodoro",)) == []


# --- transição de fase no scheduler -----------------------------------------
class FakeSession:
    def __init__(self):
        self.recebidos = []

    async def safe_send(self, data):
        self.recebidos.append(data)
        return True


class FakeCtx:
    def __init__(self, sessoes):
        self.sessoes = set(sessoes)
        self.tts = FakeTts()
        self.web = None
        self.llama = None
        self.interactive_idle = asyncio.Event()
        self.interactive_idle.set()
        # Modo economia (2026-08-02): o `tick` consulta o watcher a cada passada.
        # Recém-usado e acordado = o estado normal durante um pomodoro.
        self.descansando = False
        self.idle_em_andamento = False
        self.ultima_interacao = time.monotonic()

    def segundos_sem_uso(self, agora=None) -> float:
        return max(0.0, (agora if agora is not None else time.monotonic()) - self.ultima_interacao)


async def test_pomodoro_alterna_foco_e_pausa(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "sched.db"))
    telemetry.db.init()
    sess = FakeSession()
    sched = SchedulerService(FakeCtx([sess]))
    agora = datetime(2026, 7, 19, 10, 0, 0)

    # pomodoro vencido, na fase foco
    telemetry.db.criar_agendamento(
        "pomodoro", "Pomodoro", agora.isoformat(), None, '{"fase": "foco"}', None
    )
    await sched.tick(agora)                                     # fim do foco -> anuncia pausa
    # avança até o fim da pausa e dispara de novo -> anuncia volta ao foco
    depois = agora + timedelta(minutes=settings.pomodoro_pausa_min)
    await sched.tick(depois)

    textos = " ".join(d.get("texto", "").lower() for d in sess.recebidos)
    assert "pausa de" in textos                                # 1ª transição (foco->pausa)
    assert "de volta ao foco" in textos                        # 2ª transição (pausa->foco)
    assert len(telemetry.db.listar_agendamentos(("pomodoro",))) == 1   # segue ciclando
