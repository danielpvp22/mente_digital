"""A tela apagada entra na conta da parede.

O que estes testes protegem é uma DECISÃO de modelagem, não uma função: a parcela
dos monitores responde sozinha por 89 dos 164 W de largura da faixa (medido em
2026-08-04), e ela é a única que nenhum sensor dentro do PC enxerga. Saber que a
tela apagou é o maior ganho de precisão disponível de graça — e errar esse sinal
para o lado errado produziria um número menor e plausível, que é a pior forma de
errar uma conta de luz.
"""
from __future__ import annotations

from dataclasses import replace

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


def test_monitores_ligados_inverte_num_lugar_so(monkeypatch):
    monkeypatch.setattr(tela, "segundos_ocioso", lambda: 400.0)
    assert tela.monitores_ligados(300.0) is False       # ocioso > timeout -> apagada
    monkeypatch.setattr(tela, "segundos_ocioso", lambda: 10.0)
    assert tela.monitores_ligados(300.0) is True
    monkeypatch.setattr(tela, "segundos_ocioso", lambda: None)
    assert tela.monitores_ligados(300.0) is None


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


def test_o_erro_possivel_e_para_o_lado_SEGURO():
    """A inferência erra num sentido só, e isso é escolha.

    Mexer o mouse acende a tela na hora, então logo após o dono apagá-la pelo botão
    do monitor o ocioso ainda é pequeno e a gente diz ACESA — superestimando. O
    contrário (dizer APAGADA com a tela acesa) não acontece, porque o Windows só
    apaga depois do mesmo timeout que a regra compara.

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
