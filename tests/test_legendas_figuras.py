"""As DUAS cópias da legenda de uma figura, e o conserto que copia uma na outra.

O defeito medido em 2026-07-29 na Cannabis Encyclopedia: a linha de bloco que a
síntese do capítulo carrega está em PORTUGUÊS e a nota da mesma figura continua no
INGLÊS do site — 1.452 das 1.818 notas. A tradução já está no vault; o conserto é
copiá-la de um lado para o outro, sem LLM e sem texto novo.
"""
from mente_digital import figuras

NOTA = """---
origem: Livro 'The Cannabis Encyclopedia (Jorge Cervantes) — edição web' — Soil – Chapter 18 (p. 1-1)
colhido_em: 2026-07-27
---
## Rocky, clayey soil is packed with nutrients.
Rocky, clayey soil is packed with nutrients.
![[Figuras/enciclopedia/enciclopedia_p0001_f1.webp]]
Figura da página 1, do capítulo 'Soil – Chapter 18', em The Cannabis Encyclopedia.
**Malha Neural:** [[Soil]] [[Cannabis]]
#zettelkasten_atomico #conhecimento_novo
"""

PT = "Solo rochoso e argiloso é rico em nutrientes."


# --- ler o bloco -------------------------------------------------------------
def test_le_a_legenda_que_o_bloco_markdown_escreveu():
    """Round-trip de propósito: quem lê e quem escreve o formato ficam amarrados,
    então mudar `bloco_markdown` sem mudar o leitor reprova aqui."""
    bloco = figuras.bloco_markdown(
        [{"arquivo": "enciclopedia_p0001_f1.webp", "legenda": PT}], "Figuras")

    assert figuras.legendas_do_bloco(bloco) == {"enciclopedia_p0001_f1": PT}


def test_legenda_ausente_vira_rotulo_de_pagina_e_ainda_e_lida():
    bloco = figuras.bloco_markdown(
        [{"arquivo": "enciclopedia_p0009_f2.webp", "pagina": 9}], "Figuras")

    assert figuras.legendas_do_bloco(bloco) == {"enciclopedia_p0009_f2": "página 9"}


def test_texto_sem_bloco_devolve_vazio():
    assert figuras.legendas_do_bloco("## Só um átomo\nCorpo qualquer.") == {}
    assert figuras.legendas_do_bloco("") == {}


# --- escrever na nota --------------------------------------------------------
def test_troca_o_titulo_E_o_corpo_e_guarda_o_original():
    novo = figuras.trocar_legenda(NOTA, PT)

    assert novo is not None
    assert novo.count(PT) == 2                      # título e corpo
    assert "Rocky, clayey soil" not in novo.split("---")[2]   # sumiu do conteúdo
    assert "legenda_original: Rocky, clayey soil is packed with nutrients." in novo


def test_preserva_embed_proveniencia_malha_e_tags():
    novo = figuras.trocar_legenda(NOTA, PT)

    assert "![[Figuras/enciclopedia/enciclopedia_p0001_f1.webp]]" in novo
    assert "Figura da página 1, do capítulo 'Soil – Chapter 18'" in novo
    assert "**Malha Neural:** [[Soil]] [[Cannabis]]" in novo
    assert "#zettelkasten_atomico #conhecimento_novo" in novo
    assert "origem: Livro 'The Cannabis Encyclopedia" in novo


def test_e_idempotente_nota_ja_tocada_nao_muda():
    """Segunda passada não pode reescrever: o `legenda_original` só guarda UMA
    original, e sobrescrevê-lo perderia o inglês do site para sempre."""
    novo = figuras.trocar_legenda(NOTA, PT)

    assert figuras.trocar_legenda(novo, "Outra tradução qualquer.") is None


def test_legenda_igual_nao_reescreve():
    assert figuras.trocar_legenda(NOTA, "Rocky, clayey soil is packed with nutrients.") is None


def test_fail_soft_sem_titulo_ou_sem_frontmatter():
    assert figuras.trocar_legenda("sem título nenhum\ncorpo", PT) is None
    assert figuras.trocar_legenda("## Título\nCorpo sem frontmatter.", PT) is None
    assert figuras.trocar_legenda("", PT) is None


def test_nova_legenda_vazia_nao_apaga_a_atual():
    assert figuras.trocar_legenda(NOTA, "   ") is None


def test_corpo_repetido_em_outra_linha_tambem_troca_so_o_que_e_a_legenda():
    """A linha de proveniência CITA a página e o capítulo, mas não é a legenda —
    trocá-la apagaria a única pista de onde a figura veio."""
    nota = NOTA.replace(
        "Figura da página 1", "Rocky, clayey soil is packed with nutrients (p. 1)")
    novo = figuras.trocar_legenda(nota, PT)

    assert "Rocky, clayey soil is packed with nutrients (p. 1)" in novo


# --- título cortado em 120: 606 das 1.452 notas afetadas ---------------------
LONGA_EN = ("This cutaway drawing shows how roots penetrate the soil. Note: There must be "
            "enough air trapped in the soil to allow biological activity and absorption.")
LONGA_PT = ("Este desenho em corte mostra como as raízes penetram o solo. Nota: é preciso "
            "haver ar suficiente preso no solo para permitir atividade biológica e absorção.")
NOTA_CORTADA = NOTA.replace(
    "## Rocky, clayey soil is packed with nutrients.\n"
    "Rocky, clayey soil is packed with nutrients.",
    f"## {LONGA_EN[:figuras.TETO_TITULO]}\n{LONGA_EN}")


def test_corpo_inteiro_troca_mesmo_com_o_titulo_cortado():
    """O título nasce cortado em 120 chars e o corpo guarda a legenda INTEIRA.
    Trocar só o título deixaria a frase em inglês na linha de baixo — que é
    justamente a que entra no embedding."""
    novo = figuras.trocar_legenda(NOTA_CORTADA, LONGA_PT)

    assert LONGA_EN not in novo.split("---\n")[2]     # sumiu do conteúdo
    assert f"## {LONGA_PT[:figuras.TETO_TITULO]}" in novo
    assert f"\n{LONGA_PT}\n" in novo                  # corpo com a legenda inteira


def test_titulo_novo_respeita_o_teto_de_120():
    novo = figuras.trocar_legenda(NOTA_CORTADA, LONGA_PT)
    titulo = next(ln for ln in novo.split("\n") if ln.startswith("## "))

    assert len(titulo[3:]) <= figuras.TETO_TITULO


def test_original_guardada_e_a_INTEIRA_nao_a_cortada():
    """`legenda_original` é a prova que `idioma.aplicar_correcoes` usa depois;
    guardar a versão cortada perderia justamente o fim da frase."""
    novo = figuras.trocar_legenda(NOTA_CORTADA, LONGA_PT)

    assert f"legenda_original: {LONGA_EN}" in novo


def test_legenda_da_nota_devolve_a_INTEIRA_nao_a_do_titulo():
    """Regressão paga em passada real: julgar a nota pelo título (cortado em 120)
    deixou 14 legendas inteiramente em inglês escaparem da régua de idioma — o
    pedaço cortado é que carregava as palavras que denunciam."""
    assert figuras.legenda_da_nota(NOTA_CORTADA) == LONGA_EN
    assert figuras.legenda_da_nota(NOTA) == "Rocky, clayey soil is packed with nutrients."


def test_legenda_da_nota_cai_para_o_titulo_sem_corpo():
    sem_corpo = NOTA.replace("Rocky, clayey soil is packed with nutrients.\n![[", "![[")

    assert figuras.legenda_da_nota(sem_corpo) == "Rocky, clayey soil is packed with nutrients."
    assert figuras.legenda_da_nota("sem título") == ""


def test_linha_de_conteudo_alheio_nao_e_confundida_com_o_corpo():
    nota = NOTA.replace("Rocky, clayey soil is packed with nutrients.\n![[",
                        "Uma frase totalmente diferente do título.\n![[")
    novo = figuras.trocar_legenda(nota, PT)

    assert "Uma frase totalmente diferente do título." in novo   # intocada
    assert f"## {PT}" in novo                                     # título trocou
