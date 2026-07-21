"""
Afirmação vs pergunta/pedido (conserto do caso "Falcão", teste real 2026-07-21):
frase declarativa sem âncora local vira REGISTRO — não escala pra web (a busca
achava um homônimo e o fato alheio contaminava a RAM e virava átomo permanente).
"""
from otimizador import e_declarativa


def test_afirmacao_e_declarativa():
    for t in [
        "O codinome do meu drone é Falcão.",
        "Meu projeto usa uma RTX 3080",
        "A reunião foi remarcada para sexta",
    ]:
        assert e_declarativa(t), t


def test_pergunta_nao_e_declarativa():
    for t in [
        "Qual o codinome do meu drone?",
        "o que é o framework Astro",
        "como funciona o TensorRT",
        "você sabe onde deixei a chave",
        "quanto o tensorrt acelera uma rede yolo",
    ]:
        assert not e_declarativa(t), t


def test_pedido_nao_e_declarativa():
    for t in [
        "salva uma nota: testar o drone amanhã",
        "pesquisa na web sobre o Python 3.13",
        "me explica fotossíntese",
        "anota que preciso revisar o pipeline",
        "me lembra de beber água daqui a 2 minutos",
    ]:
        assert not e_declarativa(t), t
