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


def test_ttft_e_do_primeiro_token_mas_conta_todos():
    # O TTFT é o 1º token; os demais NÃO mexem no ttft, mas SÃO cronometrados (F4: cada
    # token marca o último instante e soma n_tokens, para derivar o tok/s do decode).
    tracker = LatencyTracker(clock=_clock_de([0.0, 1.0, 2.0]))
    tracker.note({"tipo": "token"})   # t=1.0 -> ttft
    tracker.note({"tipo": "token"})   # t=2.0 -> último token
    assert tracker.ttft == 1.0
    assert tracker.n_tokens == 2


def test_decode_tok_s():
    # 3 tokens: 1º em t=1, último em t=3 -> janela de decode = 2s; (3-1)/2 = 1.0 tok/s.
    tracker = LatencyTracker(clock=_clock_de([0.0, 1.0, 2.0, 3.0]))
    for _ in range(3):
        tracker.note({"tipo": "token"})
    assert tracker.decode_tok_s() == 1.0


def test_decode_tok_s_none_com_menos_de_dois_tokens():
    # Sem janela de decode medível (< 2 tokens) -> None, não divide por zero.
    tracker = LatencyTracker(clock=_clock_de([0.0, 1.0]))
    tracker.note({"tipo": "token"})
    assert tracker.decode_tok_s() is None


def test_tipos_desconhecidos_nao_marcam():
    tracker = LatencyTracker(clock=_clock_de([0.0]))
    tracker.note({"tipo": "status", "texto": "carregando"})
    assert tracker.ttft is None
    assert tracker.ttfa is None


def test_ms_converte_segundos_e_trata_none():
    assert LatencyTracker._ms(0.25) == 250
    assert LatencyTracker._ms(1.2345) == 1234
    assert LatencyTracker._ms(None) is None
