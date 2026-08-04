"""A tela apagada entra na conta da parede.

O que estes testes protegem é uma DECISÃO de modelagem, não uma função: a parcela
dos monitores responde sozinha por 89 dos 164 W de largura da faixa (medido em
2026-08-04), e ela é a única que nenhum sensor dentro do PC enxerga. Saber que a
tela apagou é o maior ganho de precisão disponível de graça — e errar esse sinal
para o lado errado produziria um número menor e plausível, que é a pior forma de
errar uma conta de luz.
"""
from __future__ import annotations

import os
from dataclasses import replace

import pytest

from mente_digital import tela, tomada


# --------------------------------------------------------------------------- #
# A regra pura                                                                 #
# --------------------------------------------------------------------------- #
def test_ociosidade_alem_do_timeout_significa_apagada():
    assert tela.apagada(ocioso_s=310.0, timeout_s=300.0) is True
    assert tela.apagada(ocioso_s=300.0, timeout_s=300.0) is True   # no limite, apagou


def test_ociosidade_aquem_do_timeout_significa_acesa():
    assert tela.apagada(ocioso_s=12.0, timeout_s=300.0) is False


def test_sem_informacao_devolve_None_e_nao_um_palpite():
    """`None` é o valor que mantém a faixa larga standby..aceso. Devolver False
    ("acesos") seria seguro para a conta mas mentiria sobre o que se sabe; devolver
    True seria pior ainda. O contrato do projeto é não arbitrar."""
    assert tela.apagada(None, 300.0) is None
    assert tela.apagada(310.0, None) is None


def test_timeout_zero_e_nunca_apagar_e_nao_apagar_sempre():
    """0 no Windows significa NUNCA apagar. Lido como número, `ocioso >= 0` seria
    sempre verdadeiro e o modelo afirmaria monitores em standby 24 h por dia — um
    erro que corta ~60 W da conta e não levanta suspeita nenhuma."""
    assert tela.apagada(ocioso_s=99999.0, timeout_s=0.0) is False
    assert tela.apagada(ocioso_s=99999.0, timeout_s=-1.0) is False


@pytest.fixture
def sem_leitura(monkeypatch):
    """Desliga a LEITURA para exercitar a inferência sozinha.

    ⚠ Não é decoração: sem isto estes testes passariam no CI (Linux, onde `acesa()`
    é None de qualquer jeito) e falhariam na máquina do dono, onde a leitura existe
    e responde ANTES da inferência. Um teste que só é verde em metade das
    plataformas não protege nada."""
    monkeypatch.setattr(tela, "acesa", lambda: None)


def test_monitores_ligados_inverte_num_lugar_so(monkeypatch, sem_leitura):
    monkeypatch.setattr(tela, "segundos_ocioso", lambda: 400.0)
    assert tela.monitores_ligados(300.0) is False       # ocioso > timeout -> apagada
    monkeypatch.setattr(tela, "segundos_ocioso", lambda: 10.0)
    assert tela.monitores_ligados(300.0) is True
    monkeypatch.setattr(tela, "segundos_ocioso", lambda: None)
    assert tela.monitores_ligados(300.0) is None


# --------------------------------------------------------------------------- #
# A LEITURA — e a regressão que a trouxe                                       #
# --------------------------------------------------------------------------- #
def test_power_request_segurando_a_tela_NAO_vira_economia_falsa(monkeypatch):
    """A regressão de 2026-08-04, reduzida ao osso.

    O dono saiu de casa; ninguém tocou em teclado ou mouse por horas, então a
    inferência afirma APAGADA. Mas o app do Claude Desktop segurava uma power
    request de DISPLAY ("Capturing"), que SUSPENDE o contador de ocioso do Windows
    — e os dois monitores ficaram acesos o tempo todo. Medido: 35,4 a 88,0 W
    subcontados, na direção que finge economia.

    A leitura tem de VENCER a inferência. Se algum dia alguém inverter a ordem das
    duas fontes em `monitores_ligados`, é este teste que acusa."""
    monkeypatch.setattr(tela, "segundos_ocioso", lambda: 7200.0)   # 2 h sem tocar
    assert tela.apagada(7200.0, 300.0) is True                     # inferência: apagada
    monkeypatch.setattr(tela, "acesa", lambda: True)               # Windows: acesa
    assert tela.monitores_ligados(300.0) is True                   # vale a LEITURA


def test_a_leitura_tambem_manda_quando_diz_apagada(monkeypatch):
    """O outro sentido: dono apagou a tela no botão do monitor e mexeu no mouse
    agora. A inferência diria ACESA (ocioso pequeno) e superestimaria; a leitura
    sabe que o console está apagado. Sem este caso, o teste acima seria satisfeito
    por um código que simplesmente devolve True sempre."""
    monkeypatch.setattr(tela, "segundos_ocioso", lambda: 3.0)
    assert tela.apagada(3.0, 300.0) is False                       # inferência: acesa
    monkeypatch.setattr(tela, "acesa", lambda: False)
    assert tela.monitores_ligados(300.0) is False


def test_esmaecida_conta_como_acesa(monkeypatch):
    """`GUID_CONSOLE_DISPLAY_STATE` tem TRÊS valores, não dois. O painel esmaecido
    (2) continua energizado, e tratá-lo como apagado cortaria ~60 W da conta em
    todo intervalo de dimming. Só o 0 é apagada — e na dúvida, superestima."""
    monkeypatch.setattr(tela, "_tentado", True)
    monkeypatch.setattr(tela, "_vivos", [object()])                # registro "vivo"
    for valor, esperado in ((0, False), (1, True), (2, True)):
        monkeypatch.setattr(tela, "_estado", valor)
        assert tela.acesa() is esperado, f"estado {valor}"


def test_sem_registro_a_leitura_diz_nao_sei_e_a_inferencia_assume(monkeypatch):
    """Fora do Windows não há powrprof. A leitura tem de devolver None (não False,
    que afirmaria tela apagada e cortaria a conta), e o módulo cai na inferência —
    que é o comportamento antigo, defeito e tudo, mas melhor que inventar."""
    monkeypatch.setattr(tela, "_tentado", False)
    monkeypatch.setattr(tela, "_vivos", [])
    monkeypatch.setattr(tela, "_estado", None)
    monkeypatch.setattr(tela, "_windows", lambda: False)
    assert tela.acesa() is None

    monkeypatch.setattr(tela, "segundos_ocioso", lambda: 400.0)
    assert tela.monitores_ligados(300.0) is False       # caiu na inferência


@pytest.mark.skipif(os.name != "nt", reason="powrprof só existe no Windows")
def test_no_windows_a_PRIMEIRA_chamada_ja_traz_valor():
    """A entrega do valor inicial é assíncrona (~11,5 ms). Sem a espera dentro do
    registro, a primeira amostra de todo processo leria None e cairia na
    inferência — de novo a fonte com o defeito, justo na largada do plantão.

    ⚠ Este teste é PULADO no CI (Linux). Ele vale na máquina do dono, que é onde o
    plantão roda; quem mexer no `_ESPERA_INICIAL_S` tem de rodar a suíte aqui, não
    só olhar o verde do GitHub."""
    import importlib

    from mente_digital import tela as recarregado
    recarregado = importlib.reload(recarregado)      # processo virgem, sem registro
    assert recarregado.acesa() is not None, "a 1ª chamada não esperou o valor chegar"


def test_registro_falho_nao_e_retentado_a_cada_amostra(monkeypatch):
    """Este processo atravessa o dia amostrando a cada 20 s. Se cada amostra
    retentasse um registro que já falhou, seriam milhares de chamadas inúteis ao
    Win32 por dia — o mesmo motivo pelo qual `segundos_ocioso` não loga o erro."""
    monkeypatch.setattr(tela, "_tentado", False)
    monkeypatch.setattr(tela, "_vivos", [])
    tentativas = []

    def _falso_windows():
        tentativas.append(1)
        return False

    monkeypatch.setattr(tela, "_windows", _falso_windows)
    for _ in range(5):
        tela.acesa()
    assert len(tentativas) == 1, f"registrou {len(tentativas)}x — deveria ser 1"


def test_fora_do_windows_nao_finge_saber(monkeypatch):
    """O CI roda em Linux. `GetLastInputInfo` não existe lá, e a resposta certa é
    'não sei' — não 0.0, que seria indistinguível de 'o dono acabou de mexer no
    mouse' e faria a conta afirmar tela acesa o tempo todo."""
    monkeypatch.setattr(tela, "_windows", lambda: False)
    assert tela.segundos_ocioso() is None


# --------------------------------------------------------------------------- #
# O efeito na conta — que é o motivo de tudo isto existir                      #
# --------------------------------------------------------------------------- #
def _monitores(t: tomada.Tomada) -> tomada.Parcela:
    return next(p for p in t.parcelas if p.nome.startswith("monitores"))


def test_tela_apagada_derruba_a_parcela_dos_monitores():
    largo = tomada.estimar(gpu_watts=50.0, cpu_watts=45.0)
    apagada = tomada.estimar(
        gpu_watts=50.0, cpu_watts=45.0,
        cenario=replace(tomada.CENARIO_PADRAO, monitores_ligados=False),
    )

    m_largo, m_apagada = _monitores(largo), _monitores(apagada)
    # O ganho é a largura: de dezenas de watts de incerteza para ~1 W.
    largura_antes = m_largo.faixa.maximo - m_largo.faixa.minimo
    largura_depois = m_apagada.faixa.maximo - m_apagada.faixa.minimo
    assert largura_antes > 80.0
    assert largura_depois < 2.0
    assert m_apagada.faixa.maximo < m_largo.faixa.maximo
    # E o total da parede encolhe junto — é o número que o dono lê.
    assert apagada.total.maximo < largo.total.maximo


def test_a_ponte_le_o_estado_do_dict_e_ausencia_nao_muda_nada():
    """`a_partir_de_energia` é a costura entre o sensor (`tela.py`) e o modelo. Um
    chamador que não conheça o campo tem de produzir EXATAMENTE o resultado de
    antes — senão este acréscimo mudaria silenciosamente contas já gravadas."""
    base = {"gpu_watts": 50.0, "cpu_watts": 45.0}

    sem_campo = tomada.a_partir_de_energia(base)
    com_none = tomada.a_partir_de_energia({**base, "monitores_ligados": None})
    apagada = tomada.a_partir_de_energia({**base, "monitores_ligados": False})
    acesa = tomada.a_partir_de_energia({**base, "monitores_ligados": True})

    assert sem_campo.total == com_none.total          # ausente == None == status quo
    assert apagada.total.maximo < sem_campo.total.maximo
    assert acesa.total.minimo > apagada.total.minimo


def test_o_erro_possivel_da_inferencia_e_para_o_lado_SEGURO():
    """A inferência erra para o lado seguro no caso do BOTÃO — e só nele.

    Mexer o mouse acende a tela na hora, então logo após o dono apagá-la pelo botão
    do monitor o ocioso ainda é pequeno e a gente diz ACESA — superestimando.

    ⚠ CORRIGIDO EM 2026-08-04. Este docstring afirmava a seguir que "o contrário
    (dizer APAGADA com a tela acesa) não acontece, porque o Windows só apaga depois
    do mesmo timeout que a regra compara". É FALSO: uma power request de DISPLAY
    suspende o contador de ocioso, e o erro proibido aconteceu em produção por
    horas. Quem cobre esse caso é a LEITURA — ver
    `test_power_request_segurando_a_tela_NAO_vira_economia_falsa`. O que segue
    valendo aqui é só o caso do botão.

    Superestimar consumo é o erro tolerável; fingir economia que não houve é o que
    corrompe a conta do mês."""
    logo_apos_apagar_no_botao = tela.apagada(ocioso_s=3.0, timeout_s=300.0)
    assert logo_apos_apagar_no_botao is False          # diz ACESA -> superestima

    acesa = tomada.estimar(gpu_watts=50.0, cpu_watts=45.0,
                           cenario=replace(tomada.CENARIO_PADRAO, monitores_ligados=True))
    apagada = tomada.estimar(gpu_watts=50.0, cpu_watts=45.0,
                             cenario=replace(tomada.CENARIO_PADRAO, monitores_ligados=False))
    # ⚠ A faixa inteira SOBE — não é que "acesa" fique acima de "apagada" sem se
    # tocarem. A primeira versão deste teste afirmava
    # `acesa.minimo > apagada.maximo` e falhou: medido, dá 198,2 contra 238,7, ou
    # seja elas se SOBREPÕEM. E o motivo é honesto — a incerteza das outras parcelas
    # modeladas (placa-mãe 30..70, refrigeração 8..25, USB 2..12) é larga o bastante
    # para cobrir a diferença dos monitores. Afirmar separação total seria vender
    # mais precisão do que este modelo tem, no teste de um módulo cujo cabeçalho
    # inteiro é sobre não fazer isso.
    assert acesa.total.minimo > apagada.total.minimo
    assert acesa.total.maximo > apagada.total.maximo
