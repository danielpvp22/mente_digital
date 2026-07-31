"""
Gate de PÁGINA da co-locação: a página veio por mérito ou por acidente? (2026-07-31)

O CASO REAL: o dono perguntou *"poderia me mostrar uma tartaruga marinha?"* e recebeu a
tartaruga **e uma figura de fertilizante** do livro de cannabis.

A cadeia, medida no índice real:
  1. `_buscar_figuras` acertou sozinha — só a tartaruga, a 0,1382;
  2. a busca de TEXTO casou "tartaruga MARINHA" com "algas MARINHAS", que na Cannabis
     Encyclopedia é adubo (`Livro_algas_marinhas_fornece_elementos`, 0,1811);
  3. esses átomos são páginas de livro, então `_anexos_colocados` anexou as figuras
     DAQUELAS páginas — guano de aves marinhas, alga contra ácaros.

POR QUE O GATE É DE PÁGINA E NÃO DE FIGURA. A primeira tentativa cortou a figura
co-locada por distância (ancorando no melhor da busca). Foi MEDIDA e reprovada:
consertava a tartaruga e quebrava dois casos bons — removia os "tricomas nonglandulares
cystolíticos" e a "glândula resinosa a 370X" de *o que são tricomas?*, e as fotos de
condução de ramos de *como podar a planta*. A figura co-locada PODE estar longe da
pergunta; é exatamente o defeito que aquele caminho existe para contornar.

O que separa é a página, não a figura. Medido em 9 sondas, sem sobreposição:

    co-locação BOA   tricomas 0,764 · podar 0,914 · defic. N 0,921 · clones 1,012 · hidro 0,975
    co-locação RUIM  tartaruga 1,310 · Eiffel 1,158 · pinguim 1,264 · Coliseu 1,373

(razão = melhor texto ÷ melhor figura). Quando a figura vence o texto, a pergunta é
sobre uma imagem e as páginas de texto entraram por acidente léxico. n=9 é pequeno —
por isso `figuras_anexo_texto_ratio` é botão, e 0 desliga.
"""
import pytest

from mente_digital.config import settings


def _pula_colocacao(melhor_texto, melhor_figura, razao=None):
    """A condição exata do `search` (ver `rag.py`, bloco 'GATE DE PÁGINA')."""
    r = settings.figuras_anexo_texto_ratio if razao is None else razao
    return bool(r > 0 and melhor_figura is not None and melhor_texto is not None
                and melhor_texto > melhor_figura * r)


# --- as 9 sondas medidas, como regressão -------------------------------------
@pytest.mark.parametrize("nome, figura, texto", [
    ("tricomas", 0.1482, 0.1133),
    ("podar", 0.1383, 0.1264),
    ("deficiencia de N", 0.1201, 0.1107),
    ("clones", 0.1247, 0.1262),
    ("hidroponia", 0.1209, 0.1179),
])
def test_colocacao_boa_passa(nome, figura, texto):
    assert not _pula_colocacao(texto, figura)


@pytest.mark.parametrize("nome, figura, texto", [
    ("tartaruga marinha", 0.1382, 0.1811),
    ("torre eiffel", 0.1500, 0.1737),
    ("pinguim", 0.1447, 0.1829),
    ("coliseu de roma", 0.1286, 0.1765),
])
def test_colocacao_ruim_e_pulada(nome, figura, texto):
    assert _pula_colocacao(texto, figura)


@pytest.mark.parametrize("nome, figura, texto", [
    ("mofo branco", 0.1300, 0.1396),      # razão 1,074 — a borda superior do lado BOM
    ("camaleao", 0.1644, 0.1950),         # razão 1,186 — lado RUIM
])
def test_as_bordas_medidas_na_2a_rodada(nome, figura, texto):
    esperado = texto / figura > settings.figuras_anexo_texto_ratio
    assert _pula_colocacao(texto, figura) == esperado


def test_o_vao_entre_os_dois_grupos_existe():
    """O limiar tem de cair ENTRE o pior caso bom e o melhor caso ruim.

    ⚠ O vão ESTREITOU quando a amostra passou de 9 para 12 sondas: o lado bom, que
    parecia terminar em 1,012 (clones), vai até **1,074** ("o que é mofo branco"), e o
    lado ruim começa em 1,158 (torre Eiffel). O default 1,08 cabe, mas com folga de
    0,006 para baixo — qualquer mexida sem medir de novo corta caso bom.
    """
    assert 1.074 < settings.figuras_anexo_texto_ratio < 1.158


# --- guardas de borda --------------------------------------------------------
def test_sem_figura_na_busca_nao_pula():
    # A co-locação é a ÚNICA fonte de imagem aqui — pular deixaria a resposta sem nada.
    assert not _pula_colocacao(0.9, None)


def test_sem_texto_nao_pula():
    assert not _pula_colocacao(None, 0.1382)


def test_razao_zero_desliga_o_gate():
    # Botão de emergência: volta exatamente ao comportamento anterior ao conserto.
    assert not _pula_colocacao(0.1811, 0.1382, razao=0)


def test_empate_tecnico_nao_pula():
    # Igual ao teto não é "perder": só corta quem passa do teto.
    r = settings.figuras_anexo_texto_ratio
    assert not _pula_colocacao(0.1382 * r, 0.1382)
