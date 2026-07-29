"""
Governador de verbosidade (#7): pergunta factual curta -> resposta de 1 frase (teto de
tokens menor); pedido de explicação -> resposta normal. Classificador puro.
"""
from mente_digital.config import settings
from mente_digital.verbosidade import classificar


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


# -- explique-como-para-criança (#45) -----------------------------------------
def test_crianca_detecta_variacoes():
    for p in [
        "me explica o que é RAG como para uma criança",
        "o que é blockchain, pra criança de 5 anos",
        "explica inflação como se eu fosse leigo",
        "o que é um transformer, em linguagem simples",
        "me explica ELI5 o que é uma GPU",
    ]:
        assert classificar(p).nome == "crianca", p


def test_crianca_leva_instrucao_de_simplicidade():
    n = classificar("explica computação quântica para uma criança")
    assert n.nome == "crianca"
    assert "criança de 5 anos" in n.instrucao
    assert n.max_tokens == settings.max_tokens_resposta   # não corta: analogia precisa de espaço


def test_crianca_vence_curto_e_detalhado():
    # Curtíssima que seria "curto", mas o pedido de simplicidade vence (não 1 frase).
    assert classificar("o que é RAG, bem simples").nome == "crianca"
    # "explica" (detalhado) + "para criança" -> criança ganha.
    assert classificar("explica RAG para uma criança").nome == "crianca"


def test_pergunta_normal_nao_e_crianca():
    # Não confundir uma pergunta comum com o pedido de ELI5.
    assert classificar("qual a capital da França?").nome != "crianca"
    assert classificar("me explica como funciona o RAG").nome == "detalhado"


# -- consertos do corte no teto (teste real 2026-07-21) -------------------------
def test_recuperacao_de_notas_nao_e_curto():
    for p in ["O que eu anotei sobre pneus balão?", "o que eu tenho sobre drones"]:
        assert classificar(p).nome == "detalhado", p


def test_definicional_nao_e_curto():
    for p in ["O que é o framework Astro?", "o que são embeddings"]:
        assert classificar(p).nome == "detalhado", p


def test_quanto_de_efeito_nao_e_curto():
    assert classificar("Quanto o TensorRT acelera uma rede YOLO?").nome == "detalhado"


def test_quanto_seco_continua_curto():
    for p in ["quanto é 3 vezes 7", "quanto custa o kg do filé", "quanto é 15% de 240"]:
        assert classificar(p).nome == "curto", p


def test_factuais_seguem_curto():
    # Regressão do ganho do #7: o conserto não pode matar a resposta de 1 frase.
    for p in ["que horas são?", "qual a capital da França?", "Céu Azul fica em que estado?"]:
        assert classificar(p).nome == "curto", p


# --- teste real de 2026-07-29: curtas em PALAVRAS, largas em RESPOSTA --------
def test_procedimento_com_como_nao_e_curto():
    """As quatro saíram CORTADAS no meio da frase no teste guiado. "Como" pede
    passo a passo, e passo a passo não cabe em uma frase."""
    for p in ["Como faço um teste de pH do solo?",
              "Como faço o transplante de uma muda?",
              "Como identificar ácaro da teia nas folhas?",
              "Como prevenir e tratar oídio?"]:
        assert classificar(p).nome == "detalhado", p


def test_como_de_fato_unico_continua_curto():
    """Nem todo "como" é procedimento — não devolver o ganho de latência à toa."""
    for p in ["como está o tempo hoje?", "como se chama o vizinho?", "como vai você"]:
        assert classificar(p).nome == "curto", p


def test_quantas_no_feminino_nao_e_curto():
    """A flexão feminina escapava por `startswith("quanto")`: "quanto tempo leva
    a secagem?" ganhava espaço e "QUANTAS semanas até a colheita?" saía truncada."""
    assert classificar("Quantas semanas de floração até a colheita?").nome == "detalhado"
    assert classificar("Quantos dias até a germinação?").nome == "detalhado"


def test_quantas_seco_continua_curto():
    """O conserto do feminino não pode furar a lista de exceções do fato único."""
    for p in ["quantas são 3 mais 4", "quantos são dois mais dois"]:
        assert classificar(p).nome == "curto", p


def test_comparativa_nao_e_curto():
    """"Qual o melhor X" pede COMPARAÇÃO. No teste real o corte foi tão grande que
    o pipeline julgou o banco insuficiente e escalou para a web — que respondeu pior."""
    for p in ["Qual o melhor meio para enraizar estacas?",
              "qual a melhor lâmpada para floração"]:
        assert classificar(p).nome == "detalhado", p
