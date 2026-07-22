"""
Testes do SchedulerService: disparo de lembrete, entrega pendente (sem ouvinte),
reprogramação de recorrência e o ACK de aplicação (painel 2026-07): o push só é
CONCLUÍDO quando o cliente confirma que exibiu — send "ok" num TCP meio-aberto
mente, então o default é pessimista (pendente_entrega até o ack).
Usa um SQLite temporário e fakes (sem GPU/rede).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from mente_digital import telemetry
from conftest import FakeTts
from mente_digital.scheduler import SchedulerService


class FakeSession:
    def __init__(self) -> None:
        self.recebidos: list[dict] = []

    async def safe_send(self, data: dict) -> bool:
        self.recebidos.append(data)
        return True


class FakeCtx:
    def __init__(self, sessoes) -> None:
        self.sessoes = set(sessoes)
        self.tts = FakeTts()
        self.web = None
        self.llama = None
        self.interactive_idle = asyncio.Event()
        self.interactive_idle.set()


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    """Aponta o singleton `db` para um SQLite temporário e cria o schema."""
    caminho = str(tmp_path / "sched_test.db")
    monkeypatch.setattr(telemetry.db, "path", caminho)
    telemetry.db.init()
    return telemetry.db


def _proativos(sessao: FakeSession) -> list[dict]:
    return [m for m in sessao.recebidos if m.get("tipo") == "proativo"]


async def test_lembrete_dispara_e_conclui_apos_ack(db_tmp):
    sessao = FakeSession()
    ctx = FakeCtx([sessao])
    sched = SchedulerService(ctx)

    passado = (datetime.now() - timedelta(minutes=1)).isoformat()
    ag_id = db_tmp.criar_agendamento("lembrete", "tomar remédio", passado, None, None, None)

    await sched.tick()

    # A sessão recebeu a bolha proativa com a mensagem E o ack_id.
    proativos = _proativos(sessao)
    assert proativos and "tomar remédio" in proativos[0]["texto"]
    assert proativos[0].get("ack_id") == str(ag_id)
    # SEM ack ainda não conclui (pessimista: send não prova entrega).
    assert len(db_tmp.get_agendamentos_pendentes()) == 1

    # O cliente confirma que exibiu -> aí sim conclui.
    await sched.confirmar_entrega(str(ag_id))
    assert db_tmp.listar_agendamentos() == []
    assert db_tmp.get_agendamentos_pendentes() == []


async def test_sem_ouvinte_vira_pendente_e_entrega_depois(db_tmp):
    ctx = FakeCtx([])  # ninguém conectado
    sched = SchedulerService(ctx)
    passado = (datetime.now() - timedelta(minutes=1)).isoformat()
    ag_id = db_tmp.criar_agendamento("lembrete", "beber água", passado, None, None, None)

    await sched.tick()
    # Sem sessão: não pode ter concluído; ficou pendente de entrega.
    pendentes = db_tmp.get_agendamentos_pendentes()
    assert len(pendentes) == 1 and pendentes[0]["id"] == ag_id

    # Chega uma conexão, entrega os pendentes e o cliente dá o ack.
    sessao = FakeSession()
    ctx.sessoes.add(sessao)
    await sched.entregar_pendentes()
    assert any("beber água" in m.get("texto", "") for m in sessao.recebidos)

    await sched.confirmar_entrega(str(ag_id))
    assert db_tmp.get_agendamentos_pendentes() == []


async def test_reentrega_nao_duplica_enquanto_espera_ack(db_tmp):
    # Entre o envio e o ack há ticks: a espera segura a reentrega (sem fala dupla).
    sessao = FakeSession()
    ctx = FakeCtx([sessao])
    sched = SchedulerService(ctx)
    passado = (datetime.now() - timedelta(minutes=1)).isoformat()
    db_tmp.criar_agendamento("lembrete", "alongar as costas", passado, None, None, None)

    await sched.tick()          # dispara (1 proativo, aguardando ack)
    await sched.tick()          # reentrega de pendentes NÃO deve duplicar
    await sched.entregar_pendentes()

    assert len(_proativos(sessao)) == 1


async def test_sem_ack_apos_timeout_reentrega(db_tmp, monkeypatch):
    # Ack nunca chegou: expirada a espera, a reentrega normal volta a falar.
    from mente_digital.config import settings

    monkeypatch.setattr(settings, "proativo_ack_timeout_seconds", 0.0)
    sessao = FakeSession()
    ctx = FakeCtx([sessao])
    sched = SchedulerService(ctx)
    passado = (datetime.now() - timedelta(minutes=1)).isoformat()
    db_tmp.criar_agendamento("lembrete", "pagar o boleto", passado, None, None, None)

    await sched.tick()          # dispara
    await sched.tick()          # espera expirou (timeout 0) -> reenvia

    assert len(_proativos(sessao)) == 2


async def test_recorrente_reprograma_apos_ack(db_tmp):
    sessao = FakeSession()
    ctx = FakeCtx([sessao])
    sched = SchedulerService(ctx)

    disparo = (datetime.now() - timedelta(minutes=1)).isoformat()
    ag_id = db_tmp.criar_agendamento("lembrete", "alongar", disparo, "diario", None, None)

    await sched.tick()
    await sched.confirmar_entrega(str(ag_id))

    ativos = db_tmp.listar_agendamentos()
    assert len(ativos) == 1 and ativos[0]["id"] == ag_id
    # Reprogramado para o futuro (não dispara de novo no mesmo tick).
    prox = datetime.fromisoformat(ativos[0]["proximo_disparo"])
    assert prox > datetime.now()


async def test_ack_desconhecido_e_noop(db_tmp):
    sched = SchedulerService(FakeCtx([]))
    await sched.confirmar_entrega("9999")   # atrasado/duplicado: não explode


async def test_cancelar_remove_dos_vencidos(db_tmp):
    ctx = FakeCtx([FakeSession()])
    sched = SchedulerService(ctx)
    passado = (datetime.now() - timedelta(minutes=1)).isoformat()
    ag_id = db_tmp.criar_agendamento("lembrete", "não me lembre", passado, None, None, None)

    assert db_tmp.cancelar_agendamento(ag_id) is True
    await sched.tick()
    # Cancelado antes do tick: ninguém foi notificado.
    assert all(m.get("tipo") != "proativo" for s in ctx.sessoes for m in s.recebidos)


# --- Pesquisa proativa AGENDADA (#1): o "cresce a noite toda" -----------------
# Gatilho POR TEMPO (não por fim-de-sessão): o run_idle roda 1x quando o chat para;
# sem sessão a base não cresce. O scheduler dispara uma passada de pesquisa proativa
# + temas quentes, religando o modelo (o idle o descarrega) e devolvendo a GPU.

class _EtlGravador:
    def __init__(self) -> None:
        self.proativa = 0
        self.temas = 0

    async def pesquisa_proativa(self) -> None:
        self.proativa += 1

    async def pesquisa_temas_quentes(self) -> None:
        self.temas += 1


class _LlamaCarga:
    def __init__(self) -> None:
        self.loaded = 0
        self.unloaded = 0

    async def ensure_loaded(self) -> None:
        self.loaded += 1

    async def unload(self) -> None:
        self.unloaded += 1


class _CtxPesquisa:
    def __init__(self, sessoes=()) -> None:
        self.sessoes = set(sessoes)
        self.etl = _EtlGravador()
        self.llama = _LlamaCarga()
        self.interactive_idle = asyncio.Event()
        self.interactive_idle.set()


def test_pesquisa_idle_devida_gating(monkeypatch):
    from mente_digital.config import settings

    sched = SchedulerService(_CtxPesquisa())
    agora = datetime(2026, 7, 22, 3, 0, 0)

    # Desligado por default (intervalo 0).
    monkeypatch.setattr(settings, "pesquisa_agendada_intervalo_seconds", 0)
    assert sched._pesquisa_idle_devida(agora) is False

    # Ligado e nunca rodou -> devido.
    monkeypatch.setattr(settings, "pesquisa_agendada_intervalo_seconds", 7200)
    assert sched._pesquisa_idle_devida(agora) is True

    # Dentro do intervalo desde a última -> não devido.
    sched._ultima_pesquisa_idle = agora - timedelta(seconds=100)
    assert sched._pesquisa_idle_devida(agora) is False
    # Passou o intervalo -> devido de novo.
    sched._ultima_pesquisa_idle = agora - timedelta(seconds=7300)
    assert sched._pesquisa_idle_devida(agora) is True
    # Uma passada já em andamento -> não dispara outra (evita sobreposição).
    sched._pesquisa_em_andamento = True
    assert sched._pesquisa_idle_devida(agora) is False


def test_pesquisa_idle_nao_concorre_com_sessao(monkeypatch):
    from mente_digital.config import settings

    monkeypatch.setattr(settings, "pesquisa_agendada_intervalo_seconds", 7200)
    sched = SchedulerService(_CtxPesquisa(sessoes=[object()]))
    # Sessão viva: o run_idle dela já cobre; não competimos pela GPU serializada.
    assert sched._pesquisa_idle_devida(datetime(2026, 7, 22, 3, 0)) is False


async def test_executar_pesquisa_idle_religa_chama_etl_e_descarrega(monkeypatch):
    from mente_digital.config import settings

    monkeypatch.setattr(settings, "idle_descarregar_modelo", True)
    ctx = _CtxPesquisa()
    sched = SchedulerService(ctx)
    sched._pesquisa_em_andamento = True   # como o tick marcaria antes de disparar

    await sched._executar_pesquisa_idle()

    assert ctx.llama.loaded == 1          # religou (collect não auto-carrega)
    assert ctx.etl.proativa == 1 and ctx.etl.temas == 1
    assert ctx.llama.unloaded == 1        # devolveu a VRAM (idle, sem sessão)
    assert sched._pesquisa_em_andamento is False   # flag sempre reseta


async def test_executar_pesquisa_idle_erro_nao_propaga_e_reseta_flag():
    ctx = _CtxPesquisa()

    async def _explode() -> None:
        raise RuntimeError("web caiu")

    ctx.etl.pesquisa_proativa = _explode  # type: ignore[assignment]
    sched = SchedulerService(ctx)
    sched._pesquisa_em_andamento = True
    # É trabalho de FUNDO: uma falha não pode derrubar o scheduler nem travar a flag.
    await sched._executar_pesquisa_idle()
    assert sched._pesquisa_em_andamento is False
