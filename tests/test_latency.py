"""
LatencyTracker — instrumentação de TTFT (1º token) e TTFA (1º áudio).
Clock injetável => teste determinístico, sem depender do relógio real.
"""
from agent import LatencyTracker


def _clock_de(valores):
    it = iter(valores)
    return lambda: next(it)


def test_marca_primeiro_token_e_primeiro_audio():
    # t0=100.0; token em 100.2; audio em 100.5; total lido em 100.9
    tracker = LatencyTracker(clock=_clock_de([100.0, 100.2, 100.5, 100.9]))
    tracker.note({"tipo": "token", "texto": "a"})
    tracker.note({"tipo": "audio", "base64": "..."})

    assert round(tracker.ttft, 3) == 0.2
    assert round(tracker.ttfa, 3) == 0.5
    assert round(tracker.total(), 3) == 0.9


def test_so_marca_o_primeiro_de_cada_tipo():
    # o 2º token não deve mexer no ttft (nem consumir o clock)
    tracker = LatencyTracker(clock=_clock_de([0.0, 1.0]))
    tracker.note({"tipo": "token"})
    tracker.note({"tipo": "token"})
    assert tracker.ttft == 1.0


def test_tipos_desconhecidos_nao_marcam():
    tracker = LatencyTracker(clock=_clock_de([0.0]))
    tracker.note({"tipo": "status", "texto": "carregando"})
    assert tracker.ttft is None
    assert tracker.ttfa is None


def test_ms_converte_segundos_e_trata_none():
    assert LatencyTracker._ms(0.25) == 250
    assert LatencyTracker._ms(1.2345) == 1234
    assert LatencyTracker._ms(None) is None
