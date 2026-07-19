"""
Governador de VRAM (#28) + orçamento de tokens de fundo (#29): as partes puras
(detector de vazamento e cálculo do orçamento). A leitura torch.cuda não é testada
aqui — sem GPU ela devolve None e tudo degrada.
"""
import vram


# ----------------------- #28: detector de vazamento -----------------------
def test_uso_estavel_nao_alerta():
    m = vram.MonitorVram(amostras=4, slack_bytes=100)
    for _ in range(10):
        assert m.registrar(5000) is False   # platô -> sem vazamento


def test_crescimento_sustentado_alerta():
    m = vram.MonitorVram(amostras=4, slack_bytes=100)
    # sobe 200/amostra: janela cheia (4) com crescimento > slack e ainda no pico
    assert m.registrar(1000) is False
    assert m.registrar(1200) is False
    assert m.registrar(1400) is False
    assert m.registrar(1600) is True        # 1600-1000=600 > 100, no pico


def test_pico_e_volta_nao_alerta():
    m = vram.MonitorVram(amostras=4, slack_bytes=100)
    m.registrar(1000)
    m.registrar(2000)
    m.registrar(3000)
    # último NÃO é o pico (recuperou memória) -> não é vazamento
    assert m.registrar(1100) is False


def test_alerta_limpa_a_janela():
    m = vram.MonitorVram(amostras=3, slack_bytes=50)
    m.registrar(100); m.registrar(300)
    assert m.registrar(500) is True         # dispara
    # logo após, precisa encher a janela de novo antes de re-disparar
    assert m.registrar(600) is False
    assert m.registrar(700) is False


# ----------------------- #29: orçamento de tokens -----------------------
def test_vram_folgada_usa_a_base():
    assert vram.orcamento_tokens(0.5, base=512, minimo=128, frac_min=0.1, frac_ok=0.35) == 512


def test_vram_apertada_usa_o_piso():
    assert vram.orcamento_tokens(0.05, base=512, minimo=128, frac_min=0.1, frac_ok=0.35) == 128


def test_interpola_no_meio():
    # frac 0.225 = meio do caminho entre 0.1 e 0.35 -> meio entre 128 e 512 = 320
    v = vram.orcamento_tokens(0.225, base=512, minimo=128, frac_min=0.1, frac_ok=0.35)
    assert v == 320


def test_sem_leitura_nao_penaliza():
    assert vram.orcamento_tokens(None, base=512, minimo=128, frac_min=0.1, frac_ok=0.35) == 512
