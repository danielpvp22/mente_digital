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


# --- glossário do domínio: jargão fica em inglês (ordem do dono) -------------
def test_jargao_do_cultivo_atravessa_a_traducao():
    assert idioma.GLOSSARIO["bud"] == "bud"
    assert idioma.GLOSSARIO["buds"] == "buds"
    assert "bud = bud" in idioma.linha_glossario(["bud"])


def test_linha_do_glossario_leva_so_os_termos_pedidos():
    linha = idioma.linha_glossario(["gnats"])
    assert "gnats = fungus gnats" in linha and "bud" not in linha


def test_termos_do_glossario_casa_palavra_inteira():
    achados = idioma.termos_do_glossario("Fungus gnat adults stick to resinous buds")
    assert "buds" in achados and "fungus gnat" in achados
    assert idioma.termos_do_glossario("O budismo não é jargão de cultivo") == []


# --- as correções EXIGEM prova no original (defeito pego antes de gravar) -----
def test_correcao_so_vale_com_o_termo_no_ORIGINAL():
    """'bordas resinosas' vem de 'resinous buds' — mas só nessa nota."""
    texto, n = idioma.aplicar_correcoes(
        "Adere a bordas resinosas como papel de fly",
        "Fungus gnat adults stick to resinous buds like flypaper")
    assert n == 2
    assert "buds resinosos" in texto and "papel pega-moscas" in texto


def test_BORBOLETA_DE_VERDADE_nao_e_trocada():
    """O ensaio a seco pegou isto antes de gravar: a troca cega de 'borboleta'
    corrompia notas do Raven em que a palavra está CERTA."""
    texto, n = idioma.aplicar_correcoes(
        "Coevolução borboleta-monarca e Asclepias",
        "Monarch butterfly coevolution with Asclepias")
    assert n == 0 and "borboleta-monarca" in texto


def test_o_PLURAL_tambem_e_corrigido():
    """A regressão que escapou da 1ª pós-checagem: `\\bborboleta\\b` não casa
    'borboletas', e o título tinha ficado no plural."""
    texto, n = idioma.aplicar_correcoes("Aplicação de vinagre em borboletas de flor",
                                        "Vinegar Application for Cut Flower Buds")
    assert n and "buds de flor" in texto and "borboleta" not in texto


def test_palavra_inventada_pelo_modelo_e_corrigida():
    texto, _n = idioma.aplicar_correcoes("Podre excessivo causa estresse",
                                         "Excessive Pruning Causes Stress")
    assert texto.startswith("Poda excessiva")


def test_palavra_inventada_sem_prova_no_original_fica():
    texto, n = idioma.aplicar_correcoes("Podre excessivo causa estresse",
                                        "Rotten Fruit Causes Stress")
    assert n == 0 and texto.startswith("Podre")


def test_sem_original_preservado_nada_e_trocado():
    texto, n = idioma.aplicar_correcoes("Refletor borboleta e deflector", "")
    assert n == 0 and texto == "Refletor borboleta e deflector"


def test_correcao_preserva_a_inicial_maiuscula():
    texto, _n = idioma.aplicar_correcoes("Formiga de fungo adere ao meio",
                                         "Fungus gnat sticks to the medium")
    assert texto.startswith("Fungus gnat")


def test_contagem_devolve_as_tres_medidas():
    ingles, port, n = idioma.contar_funcionais("the plant and a folha")
    assert ingles == 2 and port == 1 and n == 5
