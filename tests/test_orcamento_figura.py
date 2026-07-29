"""Teto de figura no orçamento de contexto (rag.selecionar_por_orcamento).

Medido em 2026-07-29 sobre 14 perguntas reais: nota de figura ocupava 40% do
orçamento — 17 delas na pergunta sobre oídio, 15 na de poda apical. Nesta
última o texto que respondia não coube no que sobrou, e o modelo devolveu
"Não tenho informações suficientes".
"""
from mente_digital import rag


class _Doc:
    def __init__(self, texto: str, tipo: str = "") -> None:
        self.page_content = texto
        self.metadata = {"tipo": tipo} if tipo else {}


def _fig(n: int) -> _Doc:
    return _Doc("F" * n, tipo="figura")


def _txt(n: int) -> _Doc:
    return _Doc("T" * n)


def _itens(docs):
    return [(1.0, d) for d in docs]


def test_figura_reconhecida_pelo_metadado_e_pelo_embed():
    assert rag.e_nota_de_figura(_Doc("qualquer", tipo="figura"))
    assert rag.e_nota_de_figura(_Doc("- ![[Figuras/x/y.webp]] — legenda"))
    assert not rag.e_nota_de_figura(_Doc("A cobertura orgânica retém umidade."))


def test_figura_nao_passa_do_seu_teto():
    """4 figuras de 100 disputam um teto de 150 em 1000: só 1 entra."""
    escolhidos = rag.selecionar_por_orcamento(
        _itens([_fig(100), _fig(100), _fig(100), _fig(100), _txt(200)]), 1000, 0.15)
    figs = [d for _s, d in escolhidos if rag.e_nota_de_figura(d)]
    assert len(figs) == 1
    assert len(escolhidos) == 2          # a figura barrada não interrompe o resto


def test_texto_ocupa_o_espaco_que_a_figura_deixou():
    """O ponto do teto: o átomo que responde entra no lugar da 2ª figura."""
    escolhidos = rag.selecionar_por_orcamento(
        _itens([_fig(400), _fig(400), _txt(400), _txt(400)]), 1000, 0.15)
    textos = [d for _s, d in escolhidos if not rag.e_nota_de_figura(d)]
    assert len(textos) == 2


def test_teto_zero_desliga_e_volta_ao_comportamento_anterior():
    escolhidos = rag.selecionar_por_orcamento(
        _itens([_fig(300), _fig(300), _fig(300)]), 1000, 0.0)
    assert len(escolhidos) == 3


def test_orcamento_total_continua_valendo():
    escolhidos = rag.selecionar_por_orcamento(_itens([_txt(600), _txt(600), _txt(600)]), 1000, 0.15)
    assert len(escolhidos) == 1


def test_primeiro_item_entra_mesmo_estourando():
    """Contexto vazio é pior que contexto grande: o 1º sempre entra (regra antiga)."""
    assert len(rag.selecionar_por_orcamento(_itens([_txt(5000)]), 1000, 0.15)) == 1
