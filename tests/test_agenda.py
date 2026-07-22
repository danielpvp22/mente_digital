"""
Testes do parser de tempo PT-BR (agenda.py) — puro, com `agora` injetado, sem relógio real.
"""
from datetime import datetime

import agenda

# Sexta-feira, 17/07/2026, 14:30. weekday()==4 (0=segunda ... 4=sexta).
AGORA = datetime(2026, 7, 17, 14, 30, 0)
assert AGORA.weekday() == 4


def test_relativo_minutos():
    dt, rec = agenda.parse_quando("me lembra daqui a 10 minutos", AGORA)
    assert rec is None
    assert dt == datetime(2026, 7, 17, 14, 40, 0)


def test_relativo_horas_e_minutos():
    dt, rec = agenda.parse_quando("em 1 hora e 30 minutos", AGORA)
    assert rec is None
    assert dt == datetime(2026, 7, 17, 16, 0, 0)


def test_relativo_h_colado():
    dt, rec = agenda.parse_quando("daqui 1h30", AGORA)
    assert rec is None
    assert dt == datetime(2026, 7, 17, 16, 0, 0)


def test_relativo_segundos():
    dt, rec = agenda.parse_quando("em 30 segundos", AGORA)
    assert dt == datetime(2026, 7, 17, 14, 30, 30)


def test_absoluto_amanha_com_hora():
    dt, rec = agenda.parse_quando("me lembra amanhã às 8h", AGORA)
    assert rec is None
    assert dt == datetime(2026, 7, 18, 8, 0, 0)


def test_absoluto_amanha_com_minutos():
    dt, rec = agenda.parse_quando("amanhã 18h30", AGORA)
    assert dt == datetime(2026, 7, 18, 18, 30, 0)


def test_absoluto_hoje_futuro():
    dt, rec = agenda.parse_quando("hoje às 18h", AGORA)
    assert dt == datetime(2026, 7, 17, 18, 0, 0)


def test_absoluto_horario_ja_passou_vai_amanha():
    # 09:00 já passou (agora 14:30) -> agenda para amanhã.
    dt, rec = agenda.parse_quando("às 9h", AGORA)
    assert dt == datetime(2026, 7, 18, 9, 0, 0)


def test_meio_dia_e_meia_noite():
    dt, _ = agenda.parse_quando("me acorda ao meio-dia", AGORA)
    assert dt.hour == 12 and dt.minute == 0
    dt2, _ = agenda.parse_quando("à meia-noite", AGORA)
    assert dt2.hour == 0 and dt2.day == 18  # já passou hoje -> amanhã


def test_recorrente_diario():
    dt, rec = agenda.parse_quando("todo dia às 7h", AGORA)
    assert rec == "diario"
    assert dt == datetime(2026, 7, 18, 7, 0, 0)  # 07:00 já passou hoje


def test_recorrente_dia_semana():
    # "toda segunda" a partir de uma quarta -> próxima segunda (dia 20).
    dt, rec = agenda.parse_quando("toda segunda às 9h", AGORA)
    assert rec == "semanal:0"
    assert dt == datetime(2026, 7, 20, 9, 0, 0)


def test_recorrente_intervalo():
    dt, rec = agenda.parse_quando("a cada 30 minutos", AGORA)
    assert rec == "intervalo:1800"
    assert dt == datetime(2026, 7, 17, 15, 0, 0)


def test_nao_reconhecido_devolve_none():
    dt, rec = agenda.parse_quando("qualquer coisa sem horário", AGORA)
    assert dt is None and rec is None
    assert agenda.parse_quando("", AGORA) == (None, None)


def test_proximo_disparo_diario():
    prox = agenda.proximo_disparo(datetime(2026, 7, 17, 7, 0), "diario")
    assert prox == datetime(2026, 7, 18, 7, 0)


def test_proximo_disparo_semanal():
    prox = agenda.proximo_disparo(datetime(2026, 7, 20, 9, 0), "semanal:0")
    assert prox == datetime(2026, 7, 27, 9, 0)


def test_proximo_disparo_intervalo():
    prox = agenda.proximo_disparo(datetime(2026, 7, 17, 14, 30), "intervalo:1800")
    assert prox == datetime(2026, 7, 17, 15, 0)


def test_proximo_dia_semana_hoje_ainda_nao_passou():
    # Hoje é sexta (wd 4). "toda sexta às 20h" e agora são 14:30 -> hoje mesmo.
    dt = agenda.proximo_dia_semana(AGORA, 4, 20, 0)
    assert dt == datetime(2026, 7, 17, 20, 0)


# -- Número por extenso (voz transcreve "dois", não "2") -----------------------
def test_relativo_por_extenso():
    dt, rec = agenda.parse_quando("me lembra daqui a dois minutos", AGORA)
    assert rec is None
    assert dt == datetime(2026, 7, 17, 14, 32, 0)


def test_relativo_uma_hora_por_extenso():
    dt, _ = agenda.parse_quando("em uma hora", AGORA)
    assert dt == datetime(2026, 7, 17, 15, 30, 0)


def test_meia_hora():
    dt, _ = agenda.parse_quando("me acorda daqui a meia hora", AGORA)
    assert dt == datetime(2026, 7, 17, 15, 0, 0)


def test_intervalo_por_extenso():
    _dt, rec = agenda.parse_quando("a cada dez minutos", AGORA)
    assert rec == "intervalo:600"


def test_composto_vinte_e_cinco():
    dt, _ = agenda.parse_quando("daqui a vinte e cinco minutos", AGORA)
    assert dt == datetime(2026, 7, 17, 14, 55, 0)


def test_extenso_preserva_meia_noite():
    # "meia hora" (30min) NÃO pode quebrar "meia-noite" (00:00).
    dt, _ = agenda.parse_quando("me acorda à meia-noite", AGORA)
    assert dt.hour == 0 and dt.minute == 0
