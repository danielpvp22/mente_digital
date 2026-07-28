"""Detecção de átomo no idioma errado — e os falso-positivos que ela custou.

O detector é assimétrico de propósito: reescrever um átomo que já está certo é
pior do que deixar um errado. Cada teste aqui trava um caso que ERROU numa
medição real antes de o conserto existir.
"""
from mente_digital import idioma


# --- o falso positivo que inflou a 1ª medição de 477 para 185 -----------------
def test_titulo_de_sintese_com_o_NOME_DO_LIVRO_nao_e_ingles():
    """O título das notas-síntese é um TEMPLATE português que embute o nome da
    obra em inglês. Sem descontar a proveniência, 463 notas boas eram acusadas."""
    t = ("Síntese — The Cannabis Encyclopedia (Jorge Cervantes) — edição web: "
         "Water – Chapter 20")
    assert not idioma.em_ingles(t)


def test_titulo_portugues_citando_obra_em_ingles_nao_e_ingles():
    assert not idioma.em_ingles("O livro Marijuana Horticulture trata da poda apical")


def test_titulo_portugues_com_termo_tecnico_ingles_nao_e_ingles():
    assert not idioma.em_ingles("Uso de hoop-house para proteger a cultura")
    assert not idioma.em_ingles("Variação do pH das águas municipais ao longo do ano")


# --- os que DEVEM ser pegos (casos reais do lote) ------------------------------
def test_titulo_em_ingles_e_detectado():
    assert idioma.em_ingles("Cannabinoid receptors are proteins embedded in cell membranes")
    assert idioma.em_ingles("Remove Shaded Lower Growth for Disease Prevention")
    assert idioma.em_ingles("Trellising for Tall Medical Cannabis Plants")


def test_titulo_curto_demais_nao_e_julgado():
    """Duas palavras não têm palavra funcional que decida — e reescrever por
    chute estraga o que funciona."""
    assert not idioma.em_ingles("Poda apical")
    assert not idioma.em_ingles("Root Pruning")


def test_sigla_entre_parenteses_nao_conta_como_ingles():
    assert not idioma.em_ingles("Receptores canabinoides no cérebro (CB1 receptors)")


def test_limpar_proveniencia_tira_obra_capitulo_e_parenteses():
    limpo = idioma.limpar_proveniencia(
        "Síntese — The Cannabis Encyclopedia (Jorge Cervantes): Soil – Chapter 18")
    assert "Cannabis Encyclopedia" not in limpo and "Chapter" not in limpo
    assert "Soil" in limpo


# --- corpo: régua mais frouxa, e só consultada depois do título ---------------
def test_corpo_em_ingles_predomina():
    assert idioma.corpo_em_ingles(
        "The plant absorbs water through the roots and moves it to the leaves.")


def test_corpo_portugues_com_termos_ingleses_nao_conta():
    assert not idioma.corpo_em_ingles(
        "A planta absorve água pelas raízes e a leva até as folhas, "
        "processo descrito no livro como water uptake.")


def test_contagem_devolve_as_tres_medidas():
    ingles, port, n = idioma.contar_funcionais("the plant and a folha")
    assert ingles == 2 and port == 1 and n == 5
