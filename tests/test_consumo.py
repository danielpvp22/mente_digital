"""
O WATTÍMETRO — acumular ENERGIA, e recusar o que não é energia medida.

Watt é taxa e taxa não se soma; o que se soma é watt × tempo. O risco deste
módulo não é errar a conta (é uma subtração), é **somar o que não devia**: o
contador que zerou no reboot, as sete horas em que o app estava fechado, o pico de
ruído. Cada um desses vira, no gráfico do mês, uma barra que ninguém consegue
explicar seis semanas depois.

Relógio INJETADO em tudo, como em `agenda.py` e `standby.py`. SQLite de verdade em
`tmp_path`, como em `test_registro_aparelhos`: o que se quer provar é que o número
SOBREVIVE, e um fake de banco provaria só que o fake funciona.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from mente_digital import consumo


class RelogioFalso:
    """Parede e monotônico separados de propósito — o módulo usa um para decidir o
    DIA e outro para medir a JANELA, e confundi-los é o defeito que este fake
    conseguiria esconder."""

    def __init__(self, parede: datetime, mono: float = 1000.0) -> None:
        self.parede = parede
        self.mono = mono

    def agora(self) -> datetime:
        return self.parede

    def monotonico(self) -> float:
        return self.mono

    def avancar(self, segundos: float) -> None:
        from datetime import timedelta

        self.mono += segundos
        self.parede += timedelta(seconds=segundos)


# --------------------------------------------------------------------------- #
# O núcleo puro: o contador da GPU                                             #
# --------------------------------------------------------------------------- #
def test_a_primeira_amostra_nao_soma_nada():
    """Não existe janela ainda — só ancora."""
    a = consumo.acumular_contador(None, milijoules=1_000_000, agora=10.0)
    assert a.joules is None and a.motivo == "primeira_amostra"
    assert a.baseline == (1_000_000, 10.0)


def test_subtracao_exata_entre_duas_leituras():
    """1.800.000 mJ em 20 s = 1800 J = 90 W médios. A graça do contador é que ele
    já integrou por dentro do driver: nenhum pico entre as amostras se perde."""
    a = consumo.acumular_contador((1_000_000.0, 10.0), 2_800_000.0, agora=30.0)
    assert a.joules == pytest.approx(1800.0)
    assert a.segundos == pytest.approx(20.0)


def test_contador_que_zerou_NAO_vira_energia_negativa():
    """Driver recarregado / reboot. A energia do meio existiu, mas é impossível
    saber quanta — e inventar seria pior que perder o pedaço."""
    a = consumo.acumular_contador((5_000_000.0, 10.0), 12_000.0, agora=30.0)
    assert a.joules is None and a.motivo == "contador_reiniciou"
    assert a.baseline == (12_000.0, 30.0)      # rebaseou para a próxima ser limpa


def test_buraco_NAO_vira_energia_do_intervalo():
    """O CASO QUE MOTIVA O MÓDULO: o app fica fechado das 2h às 9h. Na volta, a
    subtração do contador traz SETE HORAS de energia do dispositivo. Ela existiu,
    mas não foi medida por nós — creditá-la a um intervalo de amostragem faria
    aquele dia ganhar uma barra gigante que nenhuma explicação alcança."""
    sete_horas = 7 * 3600.0
    a = consumo.acumular_contador((1_000_000.0, 10.0), 900_000_000.0,
                                  agora=10.0 + sete_horas)
    assert a.joules is None and a.motivo == "buraco"


def test_janela_curta_SOMA_o_pouco_que_houve():
    """Diferente do `potencia.media_integral`, que descarta janela curta: lá o
    resultado é uma TAXA e uma janela curta dá taxa instável; aqui é ENERGIA, e
    pouca energia é a resposta certa. Descartar perderia o pedaço."""
    a = consumo.acumular_contador((1_000_000.0, 10.0), 1_050_000.0, agora=10.5)
    assert a.joules == pytest.approx(50.0)


def test_relogio_que_anda_para_tras_nao_vira_energia():
    a = consumo.acumular_contador((1_000_000.0, 30.0), 2_000_000.0, agora=10.0)
    assert a.joules is None and a.motivo == "relogio_andou_para_tras"


# --------------------------------------------------------------------------- #
# O núcleo puro: os watts da CPU                                               #
# --------------------------------------------------------------------------- #
def test_cpu_integra_por_trapezio():
    """40 W subindo para 60 W em 10 s = média 50 W = 500 J. Retângulo daria 400 ou
    600 conforme a ponta escolhida, e o viés não se cancela em 4.300 amostras."""
    a = consumo.acumular_watts((40.0, 100.0), 60.0, agora=110.0)
    assert a.joules == pytest.approx(500.0)


def test_ajudante_que_caiu_PERDE_o_baseline():
    """Ausência não é zero. E o baseline tem de morrer junto: senão a amostra que
    vier depois do buraco fecharia um trapézio POR CIMA dele, creditando à CPU uma
    energia que ninguém mediu."""
    a = consumo.acumular_watts((40.0, 100.0), None, agora=110.0)
    assert a.joules is None and a.motivo == "sem_leitura"
    assert a.baseline is None


def test_cpu_em_zero_watt_ainda_e_medida():
    """0 W é uma leitura, não uma ausência — a mesma regra do `somar_watts`."""
    a = consumo.acumular_watts((0.0, 100.0), 0.0, agora=110.0)
    assert a.joules == pytest.approx(0.0) and a.motivo is None


# --------------------------------------------------------------------------- #
# Cobertura — o número que impede a leitura errada do gráfico                  #
# --------------------------------------------------------------------------- #
def test_cobertura_separa_dia_economico_de_dia_MAL_MEDIDO():
    assert consumo.cobertura(43200.0, 86400.0) == pytest.approx(0.5)
    assert consumo.cobertura(0.0, 86400.0) == 0.0


def test_hoje_nao_tem_24h_ainda():
    """Usar 86400 para o dia corrente faria a cobertura de hoje parecer sempre
    ruim, e o dono concluiria que o medidor está furado quando ele só está no meio
    da tarde."""
    agora = datetime(2026, 8, 3, 12, 0, 0)
    assert consumo.segundos_no_dia(date(2026, 8, 3), agora) == pytest.approx(43200.0)
    assert consumo.segundos_no_dia(date(2026, 8, 2), agora) == pytest.approx(86400.0)
    assert consumo.segundos_no_dia(date(2026, 8, 4), agora) == 0.0


def test_wh_converte_e_propaga_ausencia():
    assert consumo.wh(3600.0) == pytest.approx(1.0)
    assert consumo.wh(None) is None


# --------------------------------------------------------------------------- #
# Persistência                                                                 #
# --------------------------------------------------------------------------- #
@pytest.fixture
def registro(tmp_path):
    relogio = RelogioFalso(datetime(2026, 8, 3, 12, 0, 0))
    r = consumo.RegistroConsumo(str(tmp_path / "consumo.db"),
                                relogio=relogio.agora, monotonico=relogio.monotonico)
    r.init()
    r.relogio = relogio
    return r


def test_acumula_ao_longo_do_dia(registro):
    """Uma hora a 100 W na GPU (contador) + 50 W na CPU = 100 Wh + 50 Wh."""
    mj = 0.0
    registro.registrar(mj, 50.0)                      # ancora as duas parcelas
    for _ in range(180):                              # 180 × 20 s = 1 h
        registro.relogio.avancar(20.0)
        mj += 100.0 * 20.0 * 1000.0                   # 100 W durante 20 s, em mJ
        registro.registrar(mj, 50.0)

    dias = registro.diario(dias=1)
    assert len(dias) == 1
    assert dias[0]["gpu_wh"] == pytest.approx(100.0, rel=0.01)
    assert dias[0]["cpu_wh"] == pytest.approx(50.0, rel=0.01)
    assert dias[0]["medido_wh"] == pytest.approx(150.0, rel=0.01)


def test_dia_sem_medicao_entra_com_ZERO_e_cobertura_zero(registro):
    """Sem isto o gráfico encosta duas barras distantes e inventa uma continuidade
    que não houve."""
    registro.registrar(0.0, 40.0)
    registro.relogio.avancar(20.0)
    registro.registrar(2_000_000.0, 40.0)

    dias = registro.diario(dias=5)
    assert len(dias) == 5
    assert [d["dia"] for d in dias] == [
        "2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02", "2026-08-03"]
    assert all(d["medido_wh"] == 0.0 and d["cobertura"] == 0.0 for d in dias[:4])
    assert dias[-1]["medido_wh"] > 0


def test_a_cobertura_de_hoje_reflete_o_que_foi_medido(registro):
    """20 s medidos às 12h de um dia que já teve 43.200 s: cobertura ~0,05%."""
    registro.registrar(0.0, 40.0)
    registro.relogio.avancar(20.0)
    registro.registrar(2_000_000.0, 40.0)

    hoje = registro.diario(dias=1)[0]
    assert hoje["segundos"] == 20
    assert hoje["cobertura"] == pytest.approx(20 / 43220.0, abs=1e-4)


def test_o_mensal_agrega_e_conta_os_dias_medidos(registro):
    registro.registrar(0.0, 40.0)
    registro.relogio.avancar(20.0)
    registro.registrar(3_600_000.0, 40.0)             # 3600 J = 1 Wh na GPU

    meses = registro.mensal(meses=6)
    assert meses[-1]["mes"] == "2026-08"
    assert meses[-1]["gpu_wh"] == pytest.approx(1.0)
    assert meses[-1]["dias_com_medicao"] == 1


def test_o_descarte_fica_registrado_com_o_motivo(registro):
    """"Por que a cobertura de ontem foi 40%?" tem de ser respondível sem reencenar
    a noite."""
    registro.registrar(5_000_000.0, 40.0)
    registro.relogio.avancar(20.0)
    registro.registrar(12_000.0, 40.0)                # contador zerou

    assert registro.descartes()["contador_reiniciou"] == 1
    assert registro.descartes()["primeira_amostra"] == 2   # uma por parcela


def test_a_gpu_continua_contando_quando_o_ajudante_da_cpu_cai(registro):
    """As duas parcelas são independentes de propósito: a da GPU é exata e não
    pode ser contaminada pela que depende de um processo elevado externo."""
    mj = 0.0
    registro.registrar(mj, 40.0)
    for i in range(3):
        registro.relogio.avancar(20.0)
        mj += 100.0 * 20.0 * 1000.0
        registro.registrar(mj, None)                  # ajudante fora do ar

    dia = registro.diario(dias=1)[0]
    assert dia["gpu_wh"] > 0
    assert dia["cpu_wh"] == 0.0


def test_devido_estrangula_a_gravacao(registro):
    """O scheduler tica bem mais rápido que o intervalo de amostragem; gravar a
    cada tique seria uma escrita por segundo num banco que também serve o chat."""
    registro.registrar(0.0, 40.0)
    assert registro.devido(intervalo=20.0) is False
    registro.relogio.avancar(21.0)
    assert registro.devido(intervalo=20.0) is True
