"""Perguntas curtas em PALAVRAS e largas em RESPOSTA (teste real 2026-07-30).

Dois dos 11 turnos saíram cortados no meio da frase, e eram exatamente os dois
classificados `curto` (128 tokens): "e o fimming, qual a diferença?" parou em
"...mas seu" e "me fale sobre lâmpadas HPS" parou em "...potências de 1".
"""
from mente_digital import verbosidade


def test_comparativa_no_MEIO_da_pergunta_ganha_espaco():
    """O caso que nenhum prefixo alcança: o "qual a diferença" vem DEPOIS do assunto."""
    assert verbosidade.classificar("e o fimming, qual a diferença?").nome == "detalhado"


def test_diferenca_entre_x_e_y_ganha_espaco():
    assert verbosidade.classificar("diferença entre LED e HPS").nome == "detalhado"


def test_pedido_aberto_ganha_espaco():
    """"me fale sobre X" é pedido de panorama, não de fato único."""
    for p in ("me fale sobre lâmpadas HPS", "fale sobre nutrientes",
              "me conte sobre a floração"):
        assert verbosidade.classificar(p).nome == "detalhado", p


def test_fato_unico_CONTINUA_curto():
    """A alargada não pode comer o ganho de latência do #7: pergunta seca segue curta."""
    assert verbosidade.classificar("qual a variação de pH aceitável?").nome == "curto"
    assert verbosidade.classificar("quanto custa um ballast").nome == "curto"
