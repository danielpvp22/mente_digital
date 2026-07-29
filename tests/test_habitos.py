"""
Diário de hábitos (#37, Onda 3): "fiz X" / "marca que ..." registra o hábito do dia;
"meus hábitos" mostra as sequências. Fluxo independente. Streak puro em habitos.py.
"""
from datetime import date, timedelta

from mente_digital import habitos
from mente_digital import mestre
from mente_digital import telemetry
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


# --- streak puro ------------------------------------------------------------
def test_streak_conta_dias_consecutivos():
    hoje = date(2026, 7, 19)
    d = lambda n: hoje - timedelta(days=n)
    assert habitos.streak({d(0), d(1), d(2)}, hoje) == 3
    assert habitos.streak({d(1), d(2)}, hoje) == 2      # hoje não marcado, ontem sim -> conta
    assert habitos.streak({d(0), d(2)}, hoje) == 1      # buraco em d(1)
    assert habitos.streak(set(), hoje) == 0
    assert habitos.streak({d(3)}, hoje) == 0            # nem hoje nem ontem


# --- parser -----------------------------------------------------------------
def test_parse_habito_marcar():
    assert mestre.parse_habito_marcar("fiz treino") == "treino"
    assert mestre.parse_habito_marcar("marca que treinei") == "treinei"
    assert mestre.parse_habito_marcar("marca o habito de leitura") == "leitura"
    assert mestre.parse_habito_marcar("completei a corrida hoje") == "corrida"
    # não captura outros agentes
    assert mestre.parse_habito_marcar("marca pão na lista") is None
    assert mestre.parse_habito_marcar("cria um lembrete") is None


def test_comando_habitos_listar():
    assert mestre.comando_habitos_listar("meus hábitos")
    assert mestre.comando_habitos_listar("como vão meus hábitos")
    assert not mestre.comando_habitos_listar("meus compromissos")


# --- integração -------------------------------------------------------------
class _FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "h.db"))
    telemetry.db.init()
    ctx = AppContext(settings=settings)
    ctx.llama, ctx.tts, ctx.vectorstore = FakeLlama(["ok."]), FakeTts(), _FakeVS()
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.listar_atalhos", lambda *a, **k: {})
    return Agent(ctx), SessionMemory(settings)


async def test_marca_habito_e_lista(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    # turno de VOZ (stt_ms): este teste afere a FALA, e turno digitado não sintetiza.
    await agent.pipeline_resposta("mestre, fiz treino", send, mem, stt_ms=1)
    assert telemetry.db.habito_datas("treino") == [date.today().isoformat()]
    assert "treino" in " ".join(agent.ctx.tts.chamadas)

    await agent.pipeline_resposta("mestre, meus hábitos", send, mem, stt_ms=1)
    assert "treino" in " ".join(agent.ctx.tts.chamadas)


async def test_marcar_duas_vezes_no_mesmo_dia_nao_duplica(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    await agent.pipeline_resposta("mestre, fiz meditação", send, mem, stt_ms=1)
    await agent.pipeline_resposta("mestre, fiz meditação", send, mem, stt_ms=1)
    assert telemetry.db.habito_datas("meditacao") == [date.today().isoformat()]   # 1 só
