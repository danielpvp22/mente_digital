"""
Governador de verbosidade (#7): pergunta factual curta -> resposta de 1 frase (teto de
tokens menor); pedido de explicação -> resposta normal. Classificador puro.
"""
from config import settings
from verbosidade import classificar


def test_pergunta_curta_e_curta():
    n = classificar("que horas são?")
    assert n.nome == "curto"
    assert n.max_tokens == settings.max_tokens_resposta_curto
    assert "UMA frase" in n.instrucao


def test_factual_curta():
    for p in ["qual a capital da França?", "quanto é 3 vezes 7", "quem descobriu o Brasil"]:
        assert classificar(p).nome == "curto"


def test_pedido_de_explicacao_e_detalhado():
    for p in [
        "me explica como funciona o RAG",
        "por que o céu é azul?",
        "detalhe o processo de fotossíntese",
    ]:
        n = classificar(p)
        assert n.nome == "detalhado"
        assert n.max_tokens == settings.max_tokens_resposta
        assert n.instrucao == ""


def test_pergunta_longa_sem_pista_e_normal():
    p = "considerando o cenário atual da inteligência artificial no brasil quais empresas investem"
    n = classificar(p)
    assert n.nome == "normal"
    assert n.max_tokens == settings.max_tokens_resposta


def test_explica_vence_o_tamanho():
    # Curta MAS pede explicação -> não corta a 1 frase.
    assert classificar("explica RAG").nome == "detalhado"
