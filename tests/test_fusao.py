"""Fusão enriquecedora: base nova é a espinha, a antiga entra dentro dela.

Os casos vêm do teste cego de 2026-07-28, em que a base nova perdeu 5 a 3 nas
respostas — sempre por faltar o DADO DURO que a antiga tinha ("5 a 14 dias" de
secagem, "±0,5 ponto" de ajuste de pH, "3 a 5 dias" para a folha reverdecer).
"""
from mente_digital import fusao


# --- tokens de dado ----------------------------------------------------------
def test_pega_numero_faixa_unidade_e_grau():
    achados = fusao.tokens_de_dado("Seque de 5 a 14 dias a 70°F com 45% de umidade.")
    assert {"5", "14", "70°f", "45%"} <= achados


def test_ignora_referencia_editorial():
    """'p. 12' e 'fig. 3' são endereço, não fato — disparariam fusão à toa."""
    assert fusao.tokens_de_dado("Ver fig. 3 na p. 12") == set()


def test_normaliza_para_nao_contar_o_mesmo_fato_duas_vezes():
    assert fusao.tokens_de_dado("70°F") == fusao.tokens_de_dado("70°f")


# --- gate de enriquecimento --------------------------------------------------
NOVO = "A cura remove clorofila e sais, e deixa as flores macias."
ANTIGO = "A secagem lenta, de 5 a 14 dias, preserva os cannabinoides."


def test_vale_quando_o_antigo_tem_numero_que_o_novo_perdeu():
    vale, motivo = fusao.vale_enriquecer(ANTIGO, [NOVO])
    assert vale and "dado duro" in motivo


def test_nao_vale_quando_o_novo_ja_cobre():
    vale, motivo = fusao.vale_enriquecer("A cura remove clorofila.", [NOVO])
    assert not vale and "já cobre" in motivo


def test_lacuna_da_pagina_atravessa_o_idioma():
    """O livro está em inglês e os átomos em português — mas NÚMERO não se
    traduz. É o que faz esta comparação funcionar onde a régua léxica falhou."""
    fonte = "Dry slowly for 5 to 14 days at 70°F. Cure for 2 to 3 weeks."
    atomos = ["A secagem lenta leva de 5 a 14 dias a 70°F."]
    assert fusao.lacunas_da_pagina(fonte, atomos) == {"2", "3"}


def test_pagina_coberta_nao_tem_lacuna():
    fonte = "Dry for 5 to 14 days."
    assert fusao.lacunas_da_pagina(fonte, ["Seque de 5 a 14 dias."]) == set()


def test_sem_par_na_base_nova_nao_funde():
    """Átomo antigo sem correspondente é assunto do dono — enfiá-lo num átomo
    qualquer seria pior que deixá-lo de fora."""
    vale, motivo = fusao.vale_enriquecer(ANTIGO, [])
    assert not vale and "sem par" in motivo


def test_vale_por_conteudo_novo_mesmo_sem_numero():
    antigo = ("Colha ao amanhecer, quando as glândulas de resina estiverem turvas "
              "e leitosas, antes que escureçam.")
    vale, motivo = fusao.vale_enriquecer(antigo, ["A colheita ocorre no pico dos canabinoides."])
    assert vale and "palavras de conteúdo" in motivo


# --- guardas -----------------------------------------------------------------
def test_fusao_nao_pode_subtrair_o_que_a_base_nova_dizia():
    novo = "Mantenha a zona radicular entre 21°C e 24°C."
    assert not fusao.preserva_o_novo(novo, "Mantenha a zona radicular amena.")
    assert fusao.preserva_o_novo(novo, "Mantenha a zona radicular entre 21°C e 24°C, por 14 dias.")


def test_contradicao_e_detectada_e_nao_vira_corpo():
    assert fusao.houve_contradicao("CONTRADICAO")
    assert fusao.limpar_fusao("CONTRADICAO") == ""


def test_par_que_so_compartilha_vocabulario_e_recusado_pelo_modelo():
    """O e5 pôs 'galões de solo por mês' a 0,133 de 'lixiviação com solução' —
    dentro da faixa medida de paráfrase. Nenhum limiar separa; o julgamento sim."""
    assert fusao.nao_e_a_mesma_ideia("OUTRAIDEIA")
    assert fusao.limpar_fusao("OUTRAIDEIA") == ""
    assert not fusao.nao_e_a_mesma_ideia("A cura remove clorofila.")


def test_limpar_fusao_tira_rotulo_que_o_modelo_inventa():
    assert fusao.limpar_fusao("Corpo: A cura remove clorofila.") == "A cura remove clorofila."
