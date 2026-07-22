"""
Extração da frase citada para a busca web (agent.frase_citada).

Bug medido em produção: "da onde saiu a expressão pega um prato faz a linha dá um tiro
na farinha?" virou a query 'saiu expressão pega prato' — o extrator de 5 palavras jogou
fora a EXPRESSÃO, que é o alvo. Para a web, a frase citada é o de maior sinal.
"""
from mente_digital.agent import frase_citada


def test_extrai_a_expressao_apos_o_gatilho():
    q = "da onde saiu a expressão pega um prato faz a linha da um tiro na farinha?"
    assert frase_citada(q) == "pega um prato faz a linha da um tiro na farinha"


def test_variacoes_de_gatilho():
    assert frase_citada("o que significa o ditado antes tarde do que nunca") == \
        "antes tarde do que nunca"
    assert frase_citada("de onde vem a frase o barato sai caro") == "o barato sai caro"


def test_pula_conector_apos_o_gatilho():
    # 'o ditado QUE DIZ x' -> a frase é x, sem o 'que diz'
    assert frase_citada("qual o ditado que diz agua mole em pedra dura") == \
        "agua mole em pedra dura"


def test_nao_dispara_sem_citacao_real():
    # moldura sem frase citada (resto < 3 palavras) -> vazio, cai no termos de sempre
    assert frase_citada("qual sua expressão favorita?") == ""
    assert frase_citada("o que é uma gíria?") == ""


def test_pergunta_normal_nao_e_citacao():
    assert frase_citada("como funciona o tensor RT?") == ""
    assert frase_citada("quais os itens da lista de compra do esp32?") == ""


def test_preserva_acento_e_caixa_da_frase():
    # a frase vai pro DDG como o usuário disse (não normalizada)
    assert frase_citada("de onde vem a expressão A União Faz a Força") == \
        "A União Faz a Força"
