"""
Verbalização de números PT-BR para fala (TTS).

O Piper foneiza via espeak-ng, que ADIVINHA como ler números — e em PT-BR erra o
decimal (lê a vírgula como pausa), as horas com "h" e os símbolos ($ % ° º) que o
filtro _ALLOWED depois descarta. `verbalizar` soletra tudo em palavras ANTES do Piper,
tirando a adivinhação. Módulo PURO/testável (como agenda.py) — nenhuma dependência de
GPU/rede; só num2words para o cardinal PT-BR.
"""
from __future__ import annotations


from mente_digital.verbalizar import verbalizar


# -- decimais (o sintoma reportado: "se confunde com número com vírgula") ------
def test_decimal_simples():
    assert verbalizar("3,5") == "três vírgula cinco"


def test_decimal_preserva_zero_a_direita():
    # 5,50 NÃO pode virar "cinco vírgula cinco" (perderia o zero) — dígito a dígito.
    assert verbalizar("custa 5,50 no total") == "custa cinco vírgula cinco zero no total"


def test_decimal_multiplos_digitos():
    assert verbalizar("pi é 3,14") == "pi é três vírgula um quatro"


# -- horas (o sintoma reportado: "lê de forma estranha horas") -----------------
def test_hora_dois_pontos():
    assert verbalizar("são 14:30") == "são catorze e trinta"


def test_hora_com_h():
    assert verbalizar("às 22h42") == "às vinte e dois e quarenta e dois"


def test_hora_so_hora_com_h():
    assert verbalizar("reunião às 8h") == "reunião às oito horas"


def test_hora_zero_minutos():
    assert verbalizar("começa 9h00") == "começa nove horas"


def test_hora_uma_feminino():
    assert verbalizar("1h") == "uma hora"


def test_hora_duas_feminino():
    assert verbalizar("2:00") == "duas horas"


# -- moeda ---------------------------------------------------------------------
def test_moeda_reais_e_centavos():
    assert verbalizar("R$ 5,50") == "cinco reais e cinquenta centavos"


def test_moeda_singular_sem_centavos():
    assert verbalizar("R$ 1,00") == "um real"


def test_moeda_so_centavos():
    assert verbalizar("R$ 0,99") == "noventa e nove centavos"


def test_moeda_sem_centavos_inteiro():
    assert verbalizar("R$ 10") == "dez reais"


def test_moeda_milhar():
    assert verbalizar("R$ 1.234,56") == (
        "mil duzentos e trinta e quatro reais e cinquenta e seis centavos"
    )


# -- unidades / ordinais -------------------------------------------------------
def test_percentual():
    assert verbalizar("subiu 50%") == "subiu cinquenta por cento"


def test_graus_celsius():
    assert verbalizar("faz 80°C") == "faz oitenta graus célsius"


def test_graus_sem_unidade():
    assert verbalizar("gira 30°") == "gira trinta graus"


def test_ordinal_masculino():
    assert verbalizar("1º lugar") == "primeiro lugar"


def test_ordinal_feminino():
    assert verbalizar("2ª vez") == "segunda vez"


# -- milhar e intervalo --------------------------------------------------------
def test_milhar_agrupado():
    assert verbalizar("1.200 itens") == "mil e duzentos itens"


def test_intervalo_com_traco():
    assert verbalizar("leva 10–15 minutos") == "leva dez a quinze minutos"


def test_inteiro_simples():
    assert verbalizar("tenho 3 itens") == "tenho três itens"


# -- invariância: texto sem número não muda ------------------------------------
def test_sem_numero_inalterado():
    assert verbalizar("olá, tudo bem por aí?") == "olá, tudo bem por aí?"


def test_vazio():
    assert verbalizar("") == ""


# -- não picota: número colado a texto ainda vira só o número ------------------
def test_multiplos_numeros_na_frase():
    assert verbalizar("de 8h às 18h") == "de oito horas às dezoito horas"
