"""
Revisão diária (#21, Onda 3): "mestre, resumo do dia" agrega o que você fez hoje
(auditoria), a inbox a processar e o que vem amanhã (agenda .ics + lembretes). Só
leitura. Gatilhos distintos do SRS — e checado ANTES dele (senão "revisão do dia" cairia
na revisão de cards).
"""
from datetime import date, timedelta
from pathlib import Path

from mente_digital import mestre
from mente_digital import telemetry
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


def test_comando_revisao_diaria_detecta():
    for c in ("resumo do dia", "fechamento do dia", "como foi meu dia", "revisão do dia"):
        assert mestre.comando_revisao_diaria(c), c
    assert not mestre.comando_revisao_diaria("revisão")          # bare -> é do SRS
    assert not mestre.comando_revisao_diaria("adiciona pão")


class _FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "r.db"))
    telemetry.db.init()
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_agenda.mkdir(parents=True, exist_ok=True)
    ctx = AppContext(settings=settings)
    ctx.llama, ctx.tts, ctx.vectorstore = FakeLlama(["ok."]), FakeTts(), _FakeVS()
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    return Agent(ctx), SessionMemory(settings)


async def test_revisao_diaria_agrega_fontes(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    # 1 ação de hoje na auditoria
    telemetry.db.registrar_auditoria("lembrete_disparado", "algo")
    # 2 itens na inbox
    Path(settings.arquivo_inbox).write_text("- comprar pilhas\n- ligar pro médico\n", encoding="utf-8")
    # 1 evento amanhã
    amanha = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
    (settings.dir_agenda / "a.ics").write_text(
        f"BEGIN:VEVENT\nSUMMARY:Reunião importante\nDTSTART:{amanha}T100000\nEND:VEVENT\n",
        encoding="utf-8",
    )

    # turno de VOZ (stt_ms): este teste afere a FALA, e turno digitado não sintetiza.
    await agent.pipeline_resposta("mestre, resumo do dia", send, mem, stt_ms=1)
    falado = " ".join(agent.ctx.tts.chamadas)
    assert "fez 1" in falado                    # 1 ação hoje
    assert "2 item" in falado                   # inbox
    assert "Reunião importante" in falado       # agenda de amanhã


async def test_revisao_do_dia_nao_cai_no_srs(monkeypatch, tmp_path):
    # "revisão do dia" contém "revisão" mas deve ir pra revisão diária, não pra o SRS.
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    await agent.pipeline_resposta("mestre, revisão do dia", send, mem, stt_ms=1)
    assert mem.revisao is None                  # NÃO abriu sessão de cards
    assert "Fechamento do dia" in " ".join(agent.ctx.tts.chamadas)
