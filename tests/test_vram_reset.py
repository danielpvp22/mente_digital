"""
Reset da janela do detector de vazamento (#28): o unload/religar do LLM muda o
patamar de VRAM por causa conhecida — sem reset, a janela compara o vale
pós-unload com o pico pós-reload e acusa vazamento falso (medido 2026-07-21:
aviso aos 7,33 GB logo após religar o modelo).
"""
from mente_digital.vram import MonitorVram


def test_reset_zera_a_janela():
    m = MonitorVram(3, 100)
    m.registrar(0)
    m.registrar(50)
    m.reset()
    # A janela recomeçou: precisa encher DE NOVO antes de poder disparar.
    assert m.registrar(1_000) is False
    assert m.registrar(1_100) is False
    assert m.registrar(1_200) is True   # cheia e crescendo -> dispara de novo
