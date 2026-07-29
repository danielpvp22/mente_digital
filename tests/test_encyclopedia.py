"""Extração do livro publicado na WEB (encyclopedia.py) + precedência de obras.

Tudo PURO: HTML como string, sem rede nem disco. O que se testa aqui é o que
custou medição de verdade na sessão de 2026-07-27 — a escada de legenda, o
attach-only, a página sintética (que é o que dá co-locação exata) e o filtro de
layout que deixou o banner do autor entrar como figura do livro.
"""
from mente_digital import encyclopedia, livro as livro_mod, obras

CORPO = '<div class="elementor-widget-theme-post-content">{}</div>'


def _pagina(interno: str, titulo: str = "Soil - Chapter 18") -> str:
    # 500+ chars de corpo: o seletor só é aceito com conteúdo real (guarda do
    # extrator contra pegar um card de "posts relacionados" de 28 chars).
    enchimento = "<p>" + ("Cannabis needs a living soil. " * 30) + "</p>"
    return f"<html><body><h1>{titulo}</h1>{CORPO.format(enchimento + interno)}</body></html>"


# --- escada de legenda -------------------------------------------------------
def test_paragrafo_todo_em_italico_vira_legenda_da_figura_acima():
    html = _pagina(
        '<figure class="wp-block-image"><img src="https://x/y/soil-ph.jpg"></figure>'
        "<p><em>The pH scale of popular growing mediums.</em></p>"
    )
    _titulo, blocos = encyclopedia.extrair_capitulo(html)
    fig = blocos[0].figuras[0]
    assert fig.legenda == "The pH scale of popular growing mediums."
    assert fig.legenda_origem == encyclopedia.DEGRAU_ITALICO
    assert not fig.attach_only
    # a legenda NÃO pode virar corpo do capítulo: ela já é da figura
    assert "pH scale of popular" not in blocos[0].texto


def test_enfase_solta_no_meio_do_paragrafo_nao_e_legenda():
    """'contém <em>' não serve: um parágrafo de corpo enfatiza uma palavra e
    viraria legenda de uma figura com a qual não tem relação."""
    html = _pagina(
        '<figure class="wp-block-image"><img src="https://x/y/pote.jpg"></figure>'
        "<p>Growers <em>always</em> check the runoff before feeding the plants "
        "again, because the medium changes faster than the schedule does.</p>"
    )
    _titulo, blocos = encyclopedia.extrair_capitulo(html)
    fig = blocos[0].figuras[0]
    assert fig.legenda_origem != encyclopedia.DEGRAU_ITALICO


def test_sem_legenda_cai_para_a_secao_e_vira_attach_only():
    html = _pagina(
        "<h2>Soil Amendments</h2>"
        '<figure class="wp-block-image"><img src="https://x/y/foto.jpg"></figure>'
        "<p>Amendments change the texture of the mix over time.</p>"
    )
    _titulo, blocos = encyclopedia.extrair_capitulo(html)
    fig = blocos[0].figuras[0]
    assert fig.legenda == "Soil Amendments"
    assert fig.legenda_origem == encyclopedia.DEGRAU_SECAO
    # o ponto do achado de 2026-07-27: rótulo herdado NÃO é texto buscável
    assert fig.attach_only


def test_figura_seguida_de_figura_fecha_a_primeira_sem_legenda():
    html = _pagina(
        '<figure class="wp-block-image"><img src="https://x/y/a.jpg"></figure>'
        '<figure class="wp-block-image"><img src="https://x/y/b.jpg"></figure>'
        "<p><em>Only the second one is captioned.</em></p>"
    )
    _titulo, blocos = encyclopedia.extrair_capitulo(html)
    a, b = blocos[0].figuras
    assert a.legenda_origem == encyclopedia.DEGRAU_AUSENTE and a.attach_only
    assert b.legenda == "Only the second one is captioned."


def test_figcaption_explicito_vence_a_escada():
    html = _pagina(
        '<figure class="wp-block-image"><img src="https://x/y/c.jpg">'
        "<figcaption>Root zone temperature.</figcaption></figure>"
    )
    _titulo, blocos = encyclopedia.extrair_capitulo(html)
    fig = blocos[0].figuras[0]
    assert fig.legenda_origem == encyclopedia.DEGRAU_FIGCAPTION
    assert not fig.attach_only


# --- layout x conteúdo (defeito medido: o banner do autor virou figura) ------
def test_banner_e_logo_nao_sao_figura_do_livro():
    assert not encyclopedia.e_conteudo("https://x/JorgeCervantes_Banner-1024x280.jpg")
    assert not encyclopedia.e_conteudo("https://x/site-logo.png")
    assert encyclopedia.e_conteudo("https://x/wp-content/2019/soil-ph-1024x768.jpg")


def test_tira_de_proporcao_extrema_e_layout():
    assert not encyclopedia.e_conteudo("https://x/faixa.jpg", 1024, 90)
    assert encyclopedia.e_conteudo("https://x/faixa.jpg", 1024, 768)
    # sem dimensões declaradas não se reprova: perder figura é o erro caro
    assert encyclopedia.e_conteudo("https://x/faixa.jpg")


def test_banner_nao_entra_na_extracao():
    html = _pagina(
        '<figure class="wp-block-image">'
        '<img src="https://x/JorgeCervantes_Banner-1024x280.jpg"></figure>'
        '<figure class="wp-block-image"><img src="https://x/soil.jpg"></figure>'
        "<p><em>A healthy soil mix.</em></p>"
    )
    _titulo, blocos = encyclopedia.extrair_capitulo(html)
    assert [f.url for f in blocos[0].figuras] == ["https://x/soil.jpg"]


# --- página sintética (a co-locação) ----------------------------------------
def test_fatiar_numera_paginas_e_coloca_a_figura_na_pagina_do_texto():
    figs = [encyclopedia.FiguraWeb(url=f"u{i}") for i in range(4)]
    bloco = encyclopedia.BlocoWeb(texto="\n".join(f"linha {i}" * 40 for i in range(8)),
                                  figuras=figs)
    saida = encyclopedia.fatiar([bloco], max_chars=400, pagina_inicial=7)
    assert [b.pagina for b in saida] == list(range(7, 7 + len(saida)))
    # toda figura recebeu a página do pedaço em que caiu (nada fica em 0)
    assert all(f.pagina == b.pagina for b in saida for f in b.figuras)
    assert sum(len(b.figuras) for b in saida) == 4


def test_fatiar_nao_corta_no_meio_do_paragrafo():
    bloco = encyclopedia.BlocoWeb(texto="a" * 100 + "\n" + "b" * 100)
    saida = encyclopedia.fatiar([bloco], max_chars=120)
    assert [b.texto for b in saida] == ["a" * 100, "b" * 100]


# --- malha determinística da figura -----------------------------------------
def test_conceitos_da_figura_sao_secao_capitulo_e_assunto():
    fig = encyclopedia.FiguraWeb(url="u", titulo_secao="Soil Amendments")
    assert encyclopedia.conceitos_da_figura(fig, "Soil - Chapter 18") == [
        "Soil Amendments", "Soil", encyclopedia.ASSUNTO_LIVRO]


def test_conceitos_nao_repetem_quando_secao_e_capitulo_coincidem():
    fig = encyclopedia.FiguraWeb(url="u", titulo_secao="Soil")
    assert encyclopedia.conceitos_da_figura(fig, "Soil - Chapter 18") == [
        "Soil", encyclopedia.ASSUNTO_LIVRO]


# --- corpo da nota (o que torna a figura achável E anexável) -----------------
def test_corpo_da_nota_tem_embed_proveniencia_e_malha():
    fig = encyclopedia.FiguraWeb(url="u", legenda="The pH of mediums.", pagina=12,
                                 titulo_secao="Soil", legenda_origem="italico")
    corpo = encyclopedia.corpo_nota_figura(fig, "slug/slug_p0012_f1.webp", "Figuras",
                                           "Cannabis Encyclopedia", "Soil - Chapter 18")
    assert corpo.startswith("## The pH of mediums.")
    assert "![[Figuras/slug/slug_p0012_f1.webp]]" in corpo
    assert "página 12" in corpo and "Cannabis Encyclopedia" in corpo
    assert "[[Cannabis]]" in corpo


def test_corpo_da_nota_marca_a_attach_only_com_a_tag():
    fig = encyclopedia.FiguraWeb(url="u", pagina=3, titulo_secao="Soil",
                                 legenda="Soil", legenda_origem="secao")
    corpo = encyclopedia.corpo_nota_figura(fig, "s/s_p0003_f1.webp", "Figuras",
                                           "Livro", "Cap", tag_anexo="#figura_anexo")
    assert "#figura_anexo" in corpo
    # e a buscável NÃO leva a marca
    fig.legenda_origem = "italico"
    limpo = encyclopedia.corpo_nota_figura(fig, "s/s_p0003_f1.webp", "Figuras",
                                           "Livro", "Cap",
                                           tag_anexo="#figura_anexo" if fig.attach_only else "")
    assert "#figura_anexo" not in limpo


# --- âncora de página: as duas pontas TÊM que bater ---------------------------
def test_origem_do_job_e_a_mesma_string_dos_dois_lados():
    job = {"livro": "Enciclopédia", "capitulo": 18, "titulo_cap": "Soil",
           "pagina_inicio": 42, "pagina_fim": 42}
    assert livro_mod.origem_do_job(job) == "Livro 'Enciclopédia' — Soil (p. 42-42)"


def test_origem_do_job_cai_para_o_numero_do_capitulo():
    assert "cap. 3" in livro_mod.origem_do_job({"livro": "L", "capitulo": 3,
                                                "pagina_inicio": 1, "pagina_fim": 1})


# --- precedência entre obras (o Cervantes antigo x a edição nova) -------------
NOVA = "Livro 'The Cannabis Encyclopedia (Jorge Cervantes) — edição web' — Soil (p. 1-1)"
VELHA = "Livro 'Cervantes — Marijuana Horticulture' — Solo (p. 88-90)"


def test_marcas_le_a_lista_separada_por_barra():
    assert obras.marcas(" A | B |") == ("A", "B")
    assert obras.marcas("") == ()


PREF = obras.marcas("The Cannabis Encyclopedia")
SUP = obras.marcas("Marijuana Horticulture")
# Notas que NÃO são a edição velha e que a 1ª versão da regra aposentava.
RAVEN = "Livro 'Biologia Vegetal - Raven (8ª Edição)' — Fotossíntese (p. 12-14)"
MAO = ""            # nota escrita pelo dono: sem frontmatter de origem


def test_a_edicao_preferida_substitui_a_antiga():
    assert obras.substitui(NOVA, VELHA, PREF, SUP)
    assert not obras.substitui(VELHA, NOVA, PREF, SUP)


def test_a_obra_preferida_NAO_aposenta_outro_livro():
    """O defeito que rodou de verdade: 39 das 532 aposentadorias da 1ª passada
    eram de OUTROS livros (35 do Raven, 3 do Amabis). Substituir é relação
    declarada entre duas edições, não 'estar perto o bastante'."""
    assert not obras.substitui(NOVA, RAVEN, PREF, SUP)
    assert not obras.substitui(NOVA, MAO, PREF, SUP)
    assert not obras.substitui(NOVA, "Pesquisa web sobre solo", PREF, SUP)


def test_sem_lista_de_superadas_ninguem_e_aposentado():
    assert not obras.substitui(NOVA, VELHA, PREF, ())


def test_empate_mantem_quem_chegou_primeiro():
    assert not obras.substitui(VELHA, VELHA, PREF, SUP)
    assert not obras.substitui(NOVA, NOVA, PREF, SUP)
    # sem preferência configurada, nada substitui nada
    assert not obras.substitui(NOVA, VELHA, (), SUP)


def test_ordem_da_lista_e_ordem_de_preferencia():
    pref = obras.marcas("Encyclopedia|Horticulture")
    assert obras.precedencia(NOVA, pref) > obras.precedencia(VELHA, pref)
    assert obras.precedencia("Livro 'Outro'", pref) == 0
