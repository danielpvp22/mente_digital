"""
A conta da TOMADA — o que o modelo promete e onde ele se recusa a chutar.

Estes testes não conferem watts contra um wattímetro (não há um); conferem a
ESTRUTURA da estimativa, que é onde os erros deste tipo de conta se escondem:
monitor entrando na carga da fonte, medição ausente virando zero, eficiência
constante, e entrada absurda saindo como número plausível.

Puro do começo ao fim: nenhum sensor, nenhum arquivo, nenhuma GPU.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from mente_digital import tomada


# --------------------------------------------------------------------------- #
# Erro nº 1: o monitor não passa pela fonte do PC                              #
# --------------------------------------------------------------------------- #
def test_monitor_entra_no_total_pelo_valor_CHEIO_sem_a_perda_da_fonte():
    """Monitor tem cabo próprio na tomada. Acrescentar dois monitores tem de somar
    exatamente o watt deles — se somasse `watts / eficiência`, estaríamos cobrando
    a perda de uma fonte pela qual eles não passam."""
    sem = replace(tomada.CENARIO_PADRAO, monitores=0)
    com = replace(tomada.CENARIO_PADRAO, monitores=2, monitores_ligados=True)

    t_sem, t_com = tomada.estimar(cenario=sem), tomada.estimar(cenario=com)

    por_monitor = tomada.CENARIO_PADRAO.monitor_ligado_w
    assert t_com.total.minimo - t_sem.total.minimo == pytest.approx(2 * por_monitor.minimo)
    assert t_com.total.maximo - t_sem.total.maximo == pytest.approx(2 * por_monitor.maximo)


def test_monitor_nao_mexe_na_fracao_de_carga_da_fonte():
    """A outra metade do mesmo erro, e a mais sorrateira: jogar o monitor na carga
    DC deslocaria o ponto lido na curva de eficiência, mudando o rendimento de
    TODO o resto da máquina. A fração de carga tem de ignorar o monitor."""
    sem = replace(tomada.CENARIO_PADRAO, monitores=0)
    com = replace(tomada.CENARIO_PADRAO, monitores=2, monitores_ligados=True)

    t_sem, t_com = tomada.estimar(cenario=sem), tomada.estimar(cenario=com)

    assert t_com.fracao_de_carga == t_sem.fracao_de_carga
    assert t_com.pc_dc == t_sem.pc_dc
    assert t_com.eficiencia == t_sem.eficiencia


def test_monitor_de_estado_desconhecido_devolve_faixa_e_nao_um_chute():
    """Não sabemos se estão acesos. A faixa vai do standby ao brilho alto em vez de
    arbitrar um "valor típico" que estaria errado em metade das horas do dia."""
    incerto = replace(tomada.CENARIO_PADRAO, monitores=2, monitores_ligados=None)
    acesos = replace(tomada.CENARIO_PADRAO, monitores=2, monitores_ligados=True)

    t_incerto, t_acesos = tomada.estimar(cenario=incerto), tomada.estimar(cenario=acesos)

    assert t_incerto.fora_da_fonte.minimo < t_acesos.fora_da_fonte.minimo
    assert t_incerto.fora_da_fonte.maximo == pytest.approx(t_acesos.fora_da_fonte.maximo)


# --------------------------------------------------------------------------- #
# Erro nº 2: o periférico USB PASSA pela fonte                                 #
# --------------------------------------------------------------------------- #
def test_periferico_usb_paga_a_perda_da_fonte_ao_contrario_do_monitor():
    """O espelho do teste acima. Teclado e mouse comem do rail de 5 V: 10 W a mais
    de USB custam MAIS de 10 W na parede. Se custassem exatamente 10, alguém teria
    mandado o periférico para o lado errado do cabo."""
    base = tomada.estimar()
    mais_usb = tomada.estimar(cenario=replace(
        tomada.CENARIO_PADRAO,
        perifericos_usb_w=tomada.Faixa(
            tomada.CENARIO_PADRAO.perifericos_usb_w.minimo + 10.0,
            tomada.CENARIO_PADRAO.perifericos_usb_w.maximo + 10.0),
    ))

    acrescimo = mais_usb.total.minimo - base.total.minimo
    assert acrescimo > 10.0          # pagou a perda da fonte
    assert acrescimo < 14.0          # mas uma perda de fonte, não um fator inventado


# --------------------------------------------------------------------------- #
# A curva da fonte                                                             #
# --------------------------------------------------------------------------- #
def test_eficiencia_e_pior_em_carga_baixa_que_em_carga_media():
    """O ponto todo de modelar por curva em vez de por um número fixo: uma fonte de
    850 W entregando 85 W rende bem menos do que entregando 425 W."""
    baixa = tomada.eficiencia_fonte(85.0, 850.0)
    media = tomada.eficiencia_fonte(425.0, 850.0)

    assert baixa < media
    assert media == pytest.approx(0.90)          # o ponto de 50% da norma Gold


def test_eficiencia_cai_de_novo_na_carga_maxima():
    """A curva do 80 PLUS tem JOROBA: 87% a 20%, 90% a 50% e 87% de volta a 100%.
    Um modelo monotônico crescente erraria a ponta de cima da máquina em carga."""
    media = tomada.eficiencia_fonte(425.0, 850.0)
    cheia = tomada.eficiencia_fonte(850.0, 850.0)

    assert cheia < media
    assert cheia == pytest.approx(0.87)


def test_abaixo_da_curva_a_eficiencia_CLAMPA_em_vez_de_extrapolar():
    """Prolongar a reta dos dois pontos mais baixos até 0% de carga devolveria um
    rendimento otimista e inventado justamente onde a fonte é pior. Clampar admite
    que ali não há dado."""
    menor_ponto = min(x for x, _ in tomada.CURVA_80PLUS_GOLD)
    menor_valor = dict(tomada.CURVA_80PLUS_GOLD)[menor_ponto]

    assert tomada.eficiencia_fonte(1.0, 850.0) == pytest.approx(menor_valor)
    assert tomada.eficiencia_fonte(menor_ponto * 850.0, 850.0) == pytest.approx(menor_valor)


def test_interpolacao_no_meio_de_dois_pontos_fica_no_meio():
    """35% de carga cai entre os pontos de 20% (87%) e 50% (90%) da norma."""
    eff = tomada.eficiencia_fonte(0.35 * 850.0, 850.0)

    assert eff == pytest.approx(0.885)


# --------------------------------------------------------------------------- #
# Medição ausente: a regra "None é None, nunca zero"                           #
# --------------------------------------------------------------------------- #
def test_cpu_ausente_nao_zera_a_parcela_e_nao_quebra_a_conta():
    """O estado NORMAL desta máquina: ler o watt de pacote no Windows exige driver
    de kernel, então o ajudante elevado quase nunca está de pé. A conta tem de sair
    assim mesmo — com a CPU modelada, jamais com a CPU valendo zero."""
    t = tomada.estimar(gpu_watts=96.0, cpu_watts=None)

    parcela = next(p for p in t.parcelas if p.nome == "CPU")
    assert parcela.medido is False
    assert parcela.faixa.minimo > 0.0                      # ⚠ não virou zero
    assert parcela.faixa.maximo == tomada.RYZEN_7950X3D.ppt_w
    assert "CPU" in t.estimados
    assert t.total.minimo > 0.0


def test_cpu_ausente_alarga_a_faixa_pelo_TAMANHO_da_ignorancia():
    """A consequência honesta de não ter o sensor: a resposta fica mais larga — e
    larga NA MEDIDA do que se deixou de saber, que é o vão entre repouso e PPT.

    ⚠ A primeira versão deste teste só exigia `largura(sem) > largura(com)`, e uma
    checagem de mutação mostrou que ele PASSAVA mesmo com a CPU ausente virando
    zero: a carga DC menor cai num ponto pior da curva e alarga o total sozinha,
    por 0,2 W. Comparar contra o vão da CPU é o que torna a asserção real."""
    com_sensor = tomada.estimar(gpu_watts=96.0, cpu_watts=45.0)
    sem_sensor = tomada.estimar(gpu_watts=96.0, cpu_watts=None)

    largura = lambda t: t.total.maximo - t.total.minimo     # noqa: E731
    vao_da_cpu = tomada.RYZEN_7950X3D.ppt_w - tomada.RYZEN_7950X3D.repouso_w
    assert largura(sem_sensor) - largura(com_sensor) > vao_da_cpu
    assert "CPU" in sem_sensor.estimados
    assert "CPU" in com_sensor.medidos


def test_medicao_presente_entra_como_PONTO_e_marcada_como_medida():
    """Sensor não tem faixa. E a procedência viaja junto, senão medido e modelado
    ficam indistinguíveis no resultado."""
    t = tomada.estimar(gpu_watts=318.0, cpu_watts=140.0)

    gpu = next(p for p in t.parcelas if p.nome == "GPU")
    assert gpu.medido is True
    assert gpu.faixa.e_ponto
    assert gpu.faixa.minimo == 318.0
    assert "NVML" in gpu.fonte


def test_o_total_NUNCA_e_cem_por_cento_medido():
    """Mesmo com as duas medições em mãos, placa-mãe, refrigeração, periféricos e
    monitores continuam sendo modelo. A estrutura diz isso em vez de deixar quem lê
    supor que o número saiu de um wattímetro."""
    t = tomada.estimar(gpu_watts=96.0, cpu_watts=45.0)

    assert t.estimados                                      # nunca vazio
    assert set(t.medidos) == {"GPU", "CPU"}


# --------------------------------------------------------------------------- #
# A troca 3080 → 5080                                                          #
# --------------------------------------------------------------------------- #
def test_trocar_a_3080_pela_5080_sobe_o_teto_na_ordem_de_grandeza_do_TGP():
    """40 W a mais de TGP (320 → 360) viram um pouco MAIS de 40 W na parede, porque
    passam pela fonte. Muito mais que isso seria fator inventado; menos, seria a
    perda da fonte tendo sumido."""
    t3080 = tomada.estimar(cenario=tomada.CENARIO_PADRAO)
    t5080 = tomada.estimar(cenario=tomada.CENARIO_5080)

    delta = t5080.total.maximo - t3080.total.maximo
    diferenca_de_tgp = tomada.RTX_5080.tgp_w - tomada.RTX_3080.tgp_w
    assert delta > diferenca_de_tgp
    assert delta < diferenca_de_tgp * 1.5


def test_a_troca_de_placa_mexe_so_no_teto_porque_so_o_teto_e_conhecido():
    """O repouso da 5080 é HERDADO da 3080 de propósito (o repouso alto aqui é dos
    dois monitores segurando os clocks de VRAM, não da placa). Enquanto for
    suposição, o piso não pode se mexer — mexer fingiria uma medição que não existe."""
    t3080 = tomada.estimar(cenario=tomada.CENARIO_PADRAO)
    t5080 = tomada.estimar(cenario=tomada.CENARIO_5080)

    assert t5080.total.minimo == pytest.approx(t3080.total.minimo)
    assert t5080.fora_da_fonte == t3080.fora_da_fonte       # monitor não muda com a GPU


def test_a_placa_medida_ignora_o_perfil_e_a_troca_nao_muda_nada():
    """Com a NVML respondendo, o TGP de catálogo não entra na conta — o sensor
    manda. Trocar o perfil de placa não pode mexer num número medido."""
    medido_3080 = tomada.estimar(gpu_watts=250.0, cenario=tomada.CENARIO_PADRAO)
    medido_5080 = tomada.estimar(gpu_watts=250.0, cenario=tomada.CENARIO_5080)

    assert medido_5080.total == medido_3080.total


# --------------------------------------------------------------------------- #
# Entrada absurda: levanta em vez de virar número plausível                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("valor", [-1.0, float("nan"), float("inf"), 9_999.0, True, "96", []])
def test_medicao_absurda_LEVANTA_em_vez_de_virar_um_total_plausivel(valor):
    """Um sensor devolvendo -5 W, NaN ou 9.999 W é defeito. Engolir e cair na
    parcela modelada entregaria um total crível que esconderia o defeito para
    sempre; `True` está na lista porque `bool` é subclasse de `int` e viraria 1,0 W."""
    with pytest.raises(ValueError):
        tomada.estimar(gpu_watts=valor)
    with pytest.raises(ValueError):
        tomada.estimar(cpu_watts=valor)


@pytest.mark.parametrize("minimo,maximo", [(10.0, 5.0), (-1.0, 5.0), (float("nan"), 5.0)])
def test_faixa_invalida_e_recusada_na_construcao(minimo, maximo):
    """A faixa é o tijolo de tudo aqui. Uma faixa invertida ou com NaN se propagaria
    calada por todas as somas e só apareceria como um total esquisito no fim."""
    with pytest.raises(ValueError):
        tomada.Faixa(minimo, maximo)


@pytest.mark.parametrize("carga,capacidade", [(100.0, 0.0), (100.0, -850.0), (-1.0, 850.0)])
def test_carga_ou_capacidade_impossivel_levanta(carga, capacidade):
    """Capacidade zero daria divisão por zero; capacidade negativa daria uma fração
    de carga negativa que o clamp da curva aceitaria calado."""
    with pytest.raises(ValueError):
        tomada.eficiencia_fonte(carga, capacidade)


def test_curva_com_eficiencia_acima_de_cem_por_cento_e_recusada():
    """Uma fonte que devolve mais do que recebe é erro de digitação, não uma fonte."""
    with pytest.raises(ValueError):
        tomada.eficiencia_fonte(425.0, 850.0, curva=((0.5, 1.4),))


def test_vrm_fora_de_zero_a_um_levanta():
    with pytest.raises(ValueError):
        tomada.estimar(cenario=replace(tomada.CENARIO_PADRAO, vrm_eficiencia=0.0))


# --------------------------------------------------------------------------- #
# Invariantes da composição                                                    #
# --------------------------------------------------------------------------- #
def test_o_total_e_exatamente_o_pc_pela_fonte_mais_o_que_tem_tomada_propria():
    t = tomada.estimar(gpu_watts=96.0, cpu_watts=45.0)

    assert t.total.minimo == pytest.approx(t.pc_parede.minimo + t.fora_da_fonte.minimo)
    assert t.total.maximo == pytest.approx(t.pc_parede.maximo + t.fora_da_fonte.maximo)


def test_a_carga_dc_e_a_soma_exata_das_parcelas_que_passam_pela_fonte():
    """A partição por `Parcela.na_fonte` tem de fechar dos dois lados — é o que
    torna o erro nº 1 impossível por construção e não por disciplina."""
    t = tomada.estimar(gpu_watts=96.0, cpu_watts=45.0)

    dentro = [p for p in t.parcelas if p.na_fonte]
    fora = [p for p in t.parcelas if not p.na_fonte]
    assert t.pc_dc.minimo == pytest.approx(sum(p.faixa.minimo for p in dentro))
    assert t.fora_da_fonte.maximo == pytest.approx(sum(p.faixa.maximo for p in fora))
    assert fora, "o monitor tem de estar do lado de fora da fonte"


def test_o_pc_puxa_da_parede_mais_do_que_entrega_em_corrente_continua():
    """Não existe fonte com 100% de rendimento. Se `pc_parede <= pc_dc`, a perda
    sumiu em algum lugar da conta."""
    t = tomada.estimar(gpu_watts=96.0, cpu_watts=45.0)

    assert t.pc_parede.minimo > t.pc_dc.minimo
    assert t.pc_parede.maximo > t.pc_dc.maximo
    assert 0.5 < t.eficiencia.minimo <= 1.0


def test_perda_do_vrm_e_parcela_visivel_e_some_com_eficiencia_um():
    """A perda do VRM da placa-mãe (~10% sobre o watt de pacote) aparece com nome
    próprio, para o dono ver de onde saiu — e `vrm_eficiencia=1.0` a zera para quem
    preferir só o número cru do MSR."""
    com_perda = tomada.estimar(cpu_watts=100.0)
    sem_perda = tomada.estimar(cpu_watts=100.0,
                               cenario=replace(tomada.CENARIO_PADRAO, vrm_eficiencia=1.0))

    vrm = next(p for p in com_perda.parcelas if "VRM" in p.nome)
    assert vrm.medido is False
    assert vrm.faixa.minimo == pytest.approx(100.0 * (1 / 0.9 - 1))
    assert next(p for p in sem_perda.parcelas if "VRM" in p.nome).faixa == tomada.Faixa(0.0, 0.0)


def test_sobrecarga_da_fonte_AVISA_em_vez_de_devolver_um_numero_calado():
    """Uma placa de 575 W numa fonte de 450 W não fecha. A conta ainda sai (para o
    dono ver o quanto não fecha), mas com o aviso junto."""
    monstro = replace(
        tomada.CENARIO_PADRAO,
        placa=tomada.Placa("placa fictícia", 575.0, 30.0, "teste"),
        fonte_capacidade_w=450.0,
    )

    t = tomada.estimar(cenario=monstro)

    assert any("acima da capacidade da fonte" in a for a in t.avisos)
    assert t.total.maximo > 0.0


def test_cenario_normal_nao_dispara_aviso_nenhum():
    assert tomada.estimar(gpu_watts=96.0, cpu_watts=45.0).avisos == ()


# --------------------------------------------------------------------------- #
# Âncora de realidade e ponte com energia.medir()                              #
# --------------------------------------------------------------------------- #
def test_a_maquina_em_repouso_cobre_os_240_W_que_o_projeto_ja_registrou():
    """A única âncora empírica disponível: o docstring de `energia.somar_watts`
    registra que esta máquina gasta ~240 W enquanto a GPU sozinha marca ~96 W. O
    modelo tem de conter esse número sem os monitores — se não contiver, alguma
    parcela está grosseiramente fora, e é melhor descobrir aqui."""
    so_o_pc = replace(tomada.CENARIO_PADRAO, monitores=0)

    t = tomada.estimar(gpu_watts=96.0, cpu_watts=45.0, cenario=so_o_pc)

    assert t.pc_parede.minimo <= 240.0 <= t.pc_parede.maximo


def test_a_ponte_le_os_campos_certos_do_dicionario_do_energia():
    """`energia.medir()` devolve `gpu_watts`, `cpu_watts` E `watts_total`. Passar o
    total já somado no lugar da GPU daria uma conta errada e crível — a ponte existe
    para os nomes ficarem num lugar só."""
    medida = {"gpu_watts": 96.0, "cpu_watts": 45.0, "watts_total": 141.0, "ram_mb": 7000}

    t = tomada.a_partir_de_energia(medida)

    assert t == tomada.estimar(gpu_watts=96.0, cpu_watts=45.0)


def test_a_ponte_trata_campo_ausente_como_ausente_e_nao_como_zero():
    """O dicionário real vem quase sempre com `cpu_watts: None` (ajudante fora do
    ar) e, numa máquina sem NVIDIA, com `gpu_watts: None` também."""
    t = tomada.a_partir_de_energia({"gpu_watts": None, "cpu_watts": None})

    assert set(t.medidos) == set()
    assert t.pc_dc.minimo > 0.0
    assert "GPU" in t.estimados and "CPU" in t.estimados


def test_o_resumo_diz_o_que_foi_medido_e_o_que_foi_chutado():
    """A frase que vai para log/tela não pode passar por leitura de wattímetro."""
    linha = tomada.estimar(gpu_watts=96.0, cpu_watts=45.0).resumo()

    assert "na tomada" in linha
    assert "medido: GPU, CPU" in linha
    assert "estimado:" in linha
    assert "." not in linha              # PT-BR: decimal com vírgula, como energia.resumo


def test_o_resumo_sobrevive_a_um_console_cp1252():
    """Esta frase é candidata a log, e o console do Windows nesta env é cp1252 —
    um "≈" derruba com UnicodeEncodeError quem só queria registrar uma linha.
    Aconteceu na 1ª execução do módulo, então virou teste."""
    linha = tomada.estimar(gpu_watts=96.0).resumo()

    linha.encode("cp1252")               # levanta se algum símbolo escapar do charset
