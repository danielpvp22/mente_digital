"""
Leitor de agenda .ics 100% local (#40, Onda 3): parser puro de VEVENT (SUMMARY+DTSTART,
não-recorrente), leitura de pasta, e o comando "mestre, o que tenho hoje".
"""
from datetime import date, datetime

import calendario
import mestre
import telemetry
from agent import Agent
from config import settings
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send

_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Reunião com a equipe
DTSTART:20260719T140000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Almoço com cliente
DTSTART;TZID=America/Sao_Paulo:20260719T120000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Dentista
DTSTART:20260720T090000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Standup recorrente
DTSTART:20260719T100000
RRULE:FREQ=WEEKLY
END:VEVENT
END:VCALENDAR
"""


# --- parser puro ------------------------------------------------------------
def test_parse_eventos_ignora_recorrente_e_ordena():
    evs = calendario.parse_eventos(_ICS)
    # 3 eventos (o RRULE fica de fora), ordenados por início
    assert [t for _, t in evs] == ["Almoço com cliente", "Reunião com a equipe", "Dentista"]
    assert evs[0][0] == datetime(2026, 7, 19, 12, 0, 0)


def test_eventos_do_dia_filtra_pela_data():
    do_dia = calendario.eventos_do_dia(_ICS, date(2026, 7, 19))
    assert [t for _, t in do_dia] == ["Almoço com cliente", "Reunião com a equipe"]
    assert calendario.eventos_do_dia(_ICS, date(2026, 7, 20)) == [
        (datetime(2026, 7, 20, 9, 0, 0), "Dentista")
    ]


def test_dtstart_dia_inteiro():
    ics = "BEGIN:VEVENT\nSUMMARY:Feriado\nDTSTART;VALUE=DATE:20260719\nEND:VEVENT\n"
    evs = calendario.parse_eventos(ics)
    assert evs == [(datetime(2026, 7, 19, 0, 0, 0), "Feriado")]


def test_linha_desdobrada():
    # SUMMARY quebrado em duas linhas (continuação começa por espaço) é remontado
    ics = "BEGIN:VEVENT\nSUMMARY:Reunião muito\n  importante\nDTSTART:20260719T090000\nEND:VEVENT\n"
    assert calendario.parse_eventos(ics)[0][1] == "Reunião muito importante"


def test_ler_pasta(tmp_path):
    (tmp_path / "cal.ics").write_text(_ICS, encoding="utf-8")
    (tmp_path / "vazio.txt").write_text("não é ics", encoding="utf-8")   # ignorado
    evs = calendario.ler_pasta(str(tmp_path), date(2026, 7, 19))
    assert [t for _, t in evs] == ["Almoço com cliente", "Reunião com a equipe"]


# --- detector ---------------------------------------------------------------
def test_comando_agenda_detecta():
    for c in ("o que tenho hoje", "minha agenda", "meus compromissos", "reuniões de hoje"):
        assert mestre.comando_agenda(c), c
    for c in ("adiciona pão na lista", "o que é RAG"):
        assert not mestre.comando_agenda(c), c


# --- comando via Agent ------------------------------------------------------
class _FakeVS:
    async def sync(self):
        pass


async def test_comando_fala_a_agenda_de_hoje(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.db.listar_atalhos", lambda *a, **k: {})
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_agenda.mkdir(parents=True, exist_ok=True)
    # evento para HOJE (o handler usa date.today())
    hoje = datetime.now().strftime("%Y%m%d")
    (settings.dir_agenda / "hoje.ics").write_text(
        f"BEGIN:VEVENT\nSUMMARY:Consulta médica\nDTSTART:{hoje}T153000\nEND:VEVENT\n",
        encoding="utf-8",
    )
    ctx = AppContext(settings=settings)
    ctx.llama, ctx.tts, ctx.vectorstore = FakeLlama(["ok."]), FakeTts(), _FakeVS()
    agent, mem = Agent(ctx), SessionMemory(settings)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, o que tenho hoje", send, mem)
    falado = " ".join(agent.ctx.tts.chamadas)
    assert "Consulta médica" in falado and "15:30" in falado
