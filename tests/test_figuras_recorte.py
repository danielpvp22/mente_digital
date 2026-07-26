"""Fase 5c: detecção de figura pelo `<|grounding|>` do DeepSeek-OCR (partes puras).

As saídas cruas usadas aqui são RECORTES LITERAIS de páginas reais dos dois
livros (medidas em 2026-07-25) — cada teste diz de qual página veio, para o
próximo a mexer saber o que quebraria de verdade.
"""
from mente_digital import figuras_recorte as fr

# p550 do Amabis: figura no TOPO sem legenda + figura no RODAPÉ com legenda. O
# caso que enterra a busca geométrica ingênua ("a legenda é o texto abaixo").
AMABIS_P550 = """image[[166, 99, 831, 370]]


sub_title[[109, 394, 319, 407]]
## Antagonismo muscular

text[[108, 416, 481, 659]]
As extremidades dos músculos estriados esqueléticos são geralmente afiladas.

image[[522, 600, 858, 850]]


image_caption[[508, 858, 822, 881]]
<center>▲ Figura 19.2 • Ação dos músculos bíceps e tríceps na contração.</center>
"""

# p220 do Amabis: duas figuras lado a lado e UMA legenda descrevendo as duas.
AMABIS_P220 = """image[[117, 639, 494, 840]]

image_caption[[112, 846, 592, 881]]
<center>▲ Figura 7.16 • A. Ápice caulinar de Coleus. B. Organização geral de um caule.</center>

image[[522, 647, 880, 878]]
"""


# --- parsing ------------------------------------------------------------------
def test_parse_le_rotulo_caixa_e_conteudo():
    dets = fr.parse_grounding(AMABIS_P550)
    assert [d.tipo for d in dets] == ["image", "sub_title", "text", "image",
                                      "image_caption"]
    assert dets[0].caixa == fr.Caixa(166, 99, 831, 370)
    assert "Figura 19.2" in dets[-1].conteudo


def test_parse_ignora_caixa_que_nao_comeca_a_linha():
    # No modo "OCR this image" o modelo emite <|ref|>Fêmur<|/ref|><|det|>[[...]]:
    # sem a âncora de início de linha, o final da palavra viraria um rótulo.
    bruto = "<|ref|>Femur<|/ref|><|det|>[[169, 103, 188, 116]]<|/det|>"
    assert fr.parse_grounding(bruto) == []


def test_parse_grampeia_coordenada_fora_da_escala():
    # Estourar por poucas unidades acontece; perder a figura por isso, não pode.
    dets = fr.parse_grounding("image[[-5, 10, 1200, 400]]")
    assert dets[0].caixa == fr.Caixa(0, 10, 999, 400)


def test_parse_de_texto_vazio_ou_sem_caixa():
    assert fr.parse_grounding("") == []
    assert fr.parse_grounding("só prosa, sem grounding nenhum") == []


# --- geometria ----------------------------------------------------------------
def test_para_pixels_converte_0_999_e_prende_nos_limites():
    # Conferido recortando de verdade: o gráfico de pH da p250 do Cervantes.
    ret = fr.Caixa(52, 465, 469, 904).para_pixels(1773, 2584)
    assert ret == (92, 1202, 832, 2338)
    assert fr.Caixa(0, 0, 999, 999).para_pixels(100, 200) == (0, 0, 100, 200)


def test_para_pixels_da_margem_proporcional_ao_tamanho():
    ret = fr.Caixa(100, 100, 500, 500).para_pixels(1000, 1000, margem_frac=0.05)
    assert ret == (80, 80, 520, 520)


def test_iou_reconhece_a_mesma_figura_vista_por_duas_passadas():
    a = fr.Caixa(166, 99, 831, 370)          # passada de markdown
    b = fr.Caixa(166, 98, 831, 370)          # passada de localização
    assert a.iou(b) > 0.99
    assert a.iou(fr.Caixa(522, 600, 858, 850)) == 0.0


def test_vao_e_zero_quando_as_caixas_se_tocam():
    figura = fr.Caixa(117, 639, 494, 840)
    assert figura.vao(fr.Caixa(112, 846, 592, 881)) == 6
    assert figura.vao(fr.Caixa(120, 700, 300, 800)) == 0     # sobreposta


# --- pareamento da legenda ----------------------------------------------------
def test_legenda_vem_da_ADJACENCIA_e_nao_da_proximidade():
    figuras = fr.figuras_da_passada(fr.parse_grounding(AMABIS_P550))
    assert len(figuras) == 2
    assert figuras[0].legenda == ""                      # a do topo não tem
    assert figuras[1].legenda.startswith("Figura 19.2")  # a do rodapé tem


def test_limpar_legenda_tira_center_e_triangulo_do_livro():
    assert fr.limpar_legenda("<center>▲ Figura 19.2 • Ação  dos músculos</center>") == \
        "Figura 19.2 • Ação dos músculos"


def test_completar_legendas_compartilha_a_legenda_de_figura_em_duas_partes():
    # p220 do Amabis: a legenda "A. … B. …" descreve as duas figuras.
    dets = fr.parse_grounding(AMABIS_P220)
    figuras = fr.completar_legendas(fr.figuras_da_passada(dets),
                                    fr.legendas_da_passada(dets))
    assert len(figuras) == 2
    assert all("Figura 7.16" in f.legenda for f in figuras)


def test_completar_legendas_NAO_vaza_para_figura_distante():
    # p550: o vão entre a figura do topo e a legenda do rodapé é 488.
    dets = fr.parse_grounding(AMABIS_P550)
    figuras = fr.completar_legendas(fr.figuras_da_passada(dets),
                                    fr.legendas_da_passada(dets))
    assert figuras[0].legenda == ""


# --- união das duas passadas --------------------------------------------------
def test_unir_soma_a_caixa_que_so_UMA_passada_achou():
    # p157 do Cervantes: a passada de localização achou uma 3ª caixa.
    md = [fr.Figura(fr.Caixa(479, 72, 931, 456), "vento e chuva"),
          fr.Figura(fr.Caixa(479, 519, 931, 740), "cristais de polímero")]
    loc = [fr.Figura(fr.Caixa(478, 72, 933, 456), ""),        # a mesma da 1ª
           fr.Figura(fr.Caixa(100, 800, 400, 950), "")]       # inédita
    unidas = fr.unir_figuras(md, loc)
    assert len(unidas) == 3
    assert unidas[0].legenda == "vento e chuva"               # legenda preservada


def test_unir_RECUPERA_o_pedaco_que_uma_passada_cortou():
    # p385 do Cervantes: uma passada enquadrou só o rótulo do galão (45% da foto
    # de fora), a outra pegou a foto inteira. Ficar com a primeira perde metade.
    so_rotulo = [fr.Figura(fr.Caixa(500, 400, 700, 500), "Clorox")]
    inteira = [fr.Figura(fr.Caixa(490, 250, 710, 510), "")]
    unida = fr.unir_figuras(so_rotulo, inteira)[0]
    assert unida.caixa == fr.Caixa(490, 250, 710, 510)
    assert unida.legenda == "Clorox"


def test_unir_aproveita_a_legenda_que_a_outra_passada_trouxe():
    md = [fr.Figura(fr.Caixa(10, 10, 200, 200), "")]
    loc = [fr.Figura(fr.Caixa(11, 12, 201, 199), "Figura 3 — algo")]
    assert fr.unir_figuras(md, loc)[0].legenda == "Figura 3 — algo"


def test_absorver_aninhadas_fica_com_a_prancha_inteira():
    inteira = fr.Figura(fr.Caixa(100, 100, 800, 800), "")
    parte = fr.Figura(fr.Caixa(120, 120, 400, 400), "Figura 5 — detalhe")
    saida = fr.absorver_aninhadas([parte, inteira])
    assert len(saida) == 1
    assert saida[0].caixa == inteira.caixa
    assert saida[0].legenda == "Figura 5 — detalhe"


# --- filtros (com o motivo, para a auditoria) ---------------------------------
def test_filtra_a_tira_do_cabecalho_corrido():
    # Caso real: a passada de localização devolveu isto na p157 do Cervantes.
    tira = fr.Figura(fr.Caixa(0, 0, 999, 46), "")
    boa = fr.Figura(fr.Caixa(479, 72, 931, 456), "")
    aceitas, rejeitadas = fr.filtrar_figuras([tira, boa])
    assert aceitas == [boa]
    assert rejeitadas == [(tira, "tira")]


def test_filtra_pequena_e_pagina_inteira_com_motivo():
    icone = fr.Figura(fr.Caixa(10, 10, 30, 30), "")
    folha = fr.Figura(fr.Caixa(0, 0, 999, 999), "")
    texto = [fr.Caixa(10, 100, 480, 900)]         # a página TEM texto
    _, rejeitadas = fr.filtrar_figuras([icone, folha], texto)
    assert dict((m, f) for f, m in rejeitadas).keys() == {"pequena", "pagina_inteira"}


def test_figura_de_pagina_cheia_legitima_ainda_passa():
    # p33 do Cervantes: foto de 68% x 59% da página. Não é retrato de página.
    grande = fr.Figura(fr.Caixa(300, 70, 983, 658), "")
    aceitas, _ = fr.filtrar_figuras([grande])
    assert aceitas == [grande]


def test_foto_SANGRADA_de_pagina_inteira_nao_e_mais_descartada():
    # p97 do Cervantes: a página inteira É a fotografia, sem uma linha de texto.
    # O filtro antigo a jogava fora — o falso negativo mais caro da auditoria.
    foto = fr.Figura(fr.Caixa(0, 0, 999, 999), "")
    aceitas, rejeitadas = fr.filtrar_figuras([foto], [])      # nenhum bloco de texto
    assert aceitas == [foto] and rejeitadas == []


def test_bloco_de_TEXTO_rotulado_como_imagem_e_rejeitado():
    # p294 do Cervantes e p249 do Amabis: páginas 100% texto que saíram com uma
    # "figura" que era um parágrafo.
    falsa = fr.Figura(fr.Caixa(100, 100, 500, 800), "")
    textos = [fr.Caixa(100, 100, 500, 500), fr.Caixa(100, 500, 500, 800)]
    aceitas, rejeitadas = fr.filtrar_figuras([falsa], textos)
    assert aceitas == [] and rejeitadas == [(falsa, "texto")]


def test_figura_de_verdade_perto_de_texto_NAO_e_rejeitada():
    # A contraprova não pode virar falso negativo: a coluna de texto ao lado da
    # figura não a cobre.
    figura = fr.Figura(fr.Caixa(520, 100, 950, 600), "")
    textos = [fr.Caixa(50, 100, 480, 900), fr.Caixa(520, 620, 950, 700)]
    aceitas, _ = fr.filtrar_figuras([figura], textos)
    assert aceitas == [figura]


def test_borrao_de_UMA_passada_nao_apaga_o_acerto_da_outra():
    # p510 do Amabis / p325 do Cervantes: a passada de markdown acha a figura, a
    # de localização devolve a folha inteira. Sem descartar o borrão ANTES, a
    # contenção funde a caixa boa nele e as duas morrem no filtro.
    md = "image[[502, 325, 880, 803]]\ntext[[110, 100, 480, 900]]\n"
    loc = "image[[0, 0, 999, 999]]\n"
    aceitas, _ = fr.consolidar(md, loc)
    assert [f.caixa for f in aceitas] == [fr.Caixa(502, 325, 880, 803)]


def test_mas_a_folha_INTEIRA_sobrevive_quando_nao_ha_texto_nenhum():
    aceitas, _ = fr.consolidar("image[[0, 0, 999, 999]]", "image[[0, 0, 999, 999]]")
    assert len(aceitas) == 1


# --- resgate por coluna -------------------------------------------------------
def test_precisa_resgate_em_TODA_pagina_seca():
    gigante = [(fr.Figura(fr.Caixa(0, 0, 999, 999), ""), "pagina_inteira")]
    assert fr.precisa_resgate([], gigante)
    # p165 do Amabis: zero caixas E zero rejeições — a falha silenciosa. Exigir
    # uma rejeição para resgatar é exigir que a falha se anuncie.
    assert fr.precisa_resgate([], [])
    assert not fr.precisa_resgate([fr.Figura(fr.Caixa(1, 1, 300, 300), "")], gigante)


def test_pagina_TRUNCADA_resgata_mesmo_tendo_achado_figura():
    # O que faltou está por definição depois da última caixa emitida.
    achou = [fr.Figura(fr.Caixa(1, 1, 300, 300), "")]
    assert fr.precisa_resgate(achou, [], truncou=True)
    assert not fr.precisa_resgate(achou, [], truncou=False)


def test_caixa_majoritariamente_FIGURA_com_texto_grudado_sobrevive():
    # p288 do Cervantes: o modelo grudou o parágrafo vizinho na folha de maconha.
    # Com o limiar antigo (0,55) a série de 3 estágios foi ao vault com 2.
    figura = fr.Figura(fr.Caixa(100, 100, 500, 500), "")
    texto = [fr.Caixa(100, 100, 500, 340)]        # ~60% da caixa
    aceitas, _ = fr.filtrar_figuras([figura], texto)
    assert aceitas == [figura]


def test_remapear_traz_a_caixa_da_coluna_de_volta_para_a_pagina():
    coluna_direita = fr.Caixa(460, 0, 999, 999)
    # uma figura no meio da coluna, em coordenadas do recorte
    assert fr.remapear(fr.Caixa(0, 100, 999, 500), coluna_direita) == \
        fr.Caixa(460, 100, 999, 500)


def test_consolidar_usa_o_resgate_para_achar_o_que_a_pagina_perdeu():
    # p510 do Amabis: a 1a passada devolveu so a caixa da folha inteira; a
    # coluna direita, reperguntada sozinha, entrega a Figura 17.4.
    principal = "image[[0, 0, 999, 999]]\ntext[[10, 100, 450, 900]]\n"
    resgate = ("image[[20, 100, 950, 700]]\nimage_caption[[20, 710, 950, 760]]\n"
               "<center>Figura 17.4 - sistema linfatico humano</center>")
    aceitas, _ = fr.consolidar(principal, "", [(resgate, fr.Caixa(460, 0, 999, 999))])
    assert len(aceitas) == 1
    assert "17.4" in aceitas[0].legenda
    assert aceitas[0].caixa.x0 >= 460           # remapeada para a coluna direita


# --- ordem e consolidação -----------------------------------------------------
def test_ordem_de_leitura_e_estavel_para_o_indice_do_arquivo():
    esquerda = fr.Figura(fr.Caixa(100, 640, 400, 800), "")
    direita = fr.Figura(fr.Caixa(520, 647, 880, 878), "")    # mesmo "andar"
    topo = fr.Figura(fr.Caixa(500, 90, 800, 300), "")
    ordenadas = fr.em_ordem_de_leitura([direita, topo, esquerda])
    assert [f.caixa.x0 for f in ordenadas] == [500, 100, 520]


def test_consolidar_faz_o_caminho_inteiro_de_uma_pagina():
    aceitas, rejeitadas = fr.consolidar(AMABIS_P550, "image[[0, 0, 999, 40]]")
    assert len(aceitas) == 2
    assert aceitas[0].caixa == fr.Caixa(166, 99, 831, 370)
    assert rejeitadas == [(fr.Figura(fr.Caixa(0, 0, 999, 40), ""), "tira")]


def test_consolidar_com_uma_passada_so():
    aceitas, _ = fr.consolidar(AMABIS_P220)
    assert len(aceitas) == 2


def test_texto_do_grounding_devolve_a_prosa_sem_as_caixas():
    texto = fr.texto_do_grounding(AMABIS_P550)
    assert "Antagonismo muscular" in texto
    assert "[[" not in texto


def test_resumo_contagem_denuncia_pagina_seca():
    assert "2 página(s) sem figura (50%)" in fr.resumo_contagem({1: 2, 2: 0, 3: 1, 4: 0})


# --- nota e conserto de link --------------------------------------------------
def test_corpo_da_nota_leva_legenda_para_o_texto_pesquisavel():
    corpo = fr.corpo_da_nota("raven/raven_p0039_f2.webp",
                             "Figura 4.1 — Ciclo de Calvin", 39, "Figuras")
    assert corpo.startswith("## Figura 4.1 — Ciclo de Calvin")
    assert "![[Figuras/raven/raven_p0039_f2.webp]]" in corpo
    assert corpo.count("Ciclo de Calvin") == 2      # título e corpo


def test_corpo_da_nota_SEM_legenda_ainda_e_achavel_pelo_contexto():
    # O caso que motivou a âncora: o livro não legendou a figura. A nota não pode
    # ficar invisível — página, capítulo e temas dão ao RAG o que casar.
    corpo = fr.corpo_da_nota("amabis/amabis_p0129_f1.webp", "", 129, "Figuras",
                             livro="Biologia dos Organismos", capitulo="Fungos",
                             temas=["hifas", "esporos", "micelio"])
    assert "## Figura da página 129 — Fungos" in corpo
    assert "do capítulo 'Fungos'" in corpo
    assert "Biologia dos Organismos" in corpo
    assert "hifas, esporos, micelio" in corpo


def test_temas_do_texto_tira_stopword_de_INGLES():
    # O Cervantes é em inglês e `palavras_chave` só conhece stopword em português:
    # as 1.020 notas da 1ª passada saíram com "with, that, when, more" como TEMA.
    texto = ("Soil texture is governed by the size of the mineral particles that "
             "are with the roots when there is more water in the soil.")
    temas = fr.temas_do_texto(texto, quantos=8)
    assert "soil" in temas
    assert all(t not in temas for t in ("with", "that", "when", "more", "there"))


def test_temas_do_texto_tira_palavra_de_ESTRUTURA_do_livro():
    # Toda legenda do Amabis começa com "Figura N", então `figura` subia ao topo
    # da frequência e virava `[[Figura]]` — um hub com centenas de arestas e zero
    # significado.
    texto = ("A figura mostra os rins. A figura 17.4 detalha os rins e a pressão "
             "sanguínea. Veja a tabela e a figura da página seguinte sobre rins.")
    temas = fr.temas_do_texto(texto, quantos=6)
    assert temas[0] == "rins"
    assert all(t not in temas for t in ("figura", "tabela", "pagina", "seguir"))


def test_malha_da_figura_junta_capitulo_temas_e_livro():
    malha = fr.malha_da_figura("10 - Soil and Containers", ["soil", "roots"],
                               "Cervantes — Marijuana Horticulture")
    assert malha[0] == "Soil and Containers"        # numeração do capítulo some
    assert malha[1:3] == ["Soil", "Roots"]          # tema capitalizado
    assert malha[-1].startswith("Cervantes")


def test_malha_da_figura_nao_repete_e_nao_quebra_o_wikilink():
    malha = fr.malha_da_figura("Fungos", ["fungos", "hifas"], "Fungos")
    assert malha == ["Fungos", "Hifas"]             # dedup insensível a caixa
    assert fr._limpar_conceito("Cap [3] | nota #tag") == "Cap 3 nota tag"


def test_conceito_sem_virgula_para_a_malha_nao_partir_o_capitulo():
    # `normalizar_malha` trata vírgula como separador: sem isto, o capítulo
    # "9 - Light, Lamps and Electricity" chegava ao vault como DOIS nós falsos.
    assert fr._limpar_conceito("9 - Light, Lamps and Electricity") == \
        "Light Lamps and Electricity"
    assert "(" not in fr._limpar_conceito("Cervantes (Medical Grower's Bible)")


def test_corpo_da_nota_TEM_malha_neural():
    # O defeito que o dono achou: 1.020 de 1.020 notas sem um único wikilink de
    # malha — o único [[...]] era o embed da imagem, e a figura virava uma ilha.
    corpo = fr.corpo_da_nota("cerv/cerv_p0250_f2.webp", "", 250, "Figuras",
                             livro="Cervantes", capitulo="10 - Soil and Containers",
                             temas=["soil", "water"])
    assert "**Malha Neural:** [[Soil and Containers]] [[Soil]] [[Water]] [[Cervantes]]" \
        in corpo


def test_temas_do_texto_ordena_por_frequencia_e_tira_stopword():
    texto = ("Os fungos possuem hifas. As hifas dos fungos formam o micélio. "
             "Os fungos são heterotróficos e as hifas crescem.")
    temas = fr.temas_do_texto(texto, quantos=3)
    assert temas[:2] == ["fungos", "hifas"]     # os mais frequentes, nessa ordem
    assert all(t not in temas for t in ("os", "as", "dos", "são"))


def test_slug_do_arquivo():
    assert fr.slug_do_arquivo("meu-livro_p0039_f2.webp") == "meu-livro"
    assert fr.slug_do_arquivo("qualquer.png") is None


def test_corrigir_alvo_insere_a_pasta_do_livro():
    assert fr.corrigir_alvo("Figuras/raven_p0003_f1.webp") == \
        "Figuras/raven/raven_p0003_f1.webp"


def test_corrigir_alvo_nao_mexe_no_que_ja_esta_certo():
    assert fr.corrigir_alvo("Figuras/raven/raven_p0003_f1.webp") is None
    assert fr.corrigir_alvo("Outra/coisa.webp") is None
    assert fr.corrigir_alvo("Figuras/foto-solta.webp") is None


# --- ancoragem da legenda (anti-alucinacao do OCR) ---------------------------
def test_legenda_ancorada_reprova_alucinacao_do_ocr():
    # Caso real medido: faixa quase vazia -> o modelo inventou um calendario.
    inventada = ("Title: 2023-2024 Academic Year Calendar - Description: comprehensive "
                 "tool designed to help students, parents and educators plan")
    fonte = "Os fungos apresentam hifas cenocíticas e reprodução por esporos."
    assert fr.legenda_ancorada(inventada, fonte) == ""


def test_legenda_ancorada_aprova_legenda_de_verdade():
    real = "Fruto da planta pente-de-macaco, aberto para mostrar as sementes aladas"
    fonte = ("A dispersão das sementes: o fruto da planta pente-de-macaco abre e "
             "mostra sementes aladas revestidas de fibras.")
    assert fr.legenda_ancorada(real, fonte) == real


def test_legenda_ancorada_ignora_acento_e_caixa():
    assert fr.legenda_ancorada("SEMENTES ALADAS", "sementes aladas do pinus") != ""


def test_legenda_ancorada_sem_fonte_nao_reprova():
    # Livro sem texto de capítulo disponível: não há base para acusar invenção.
    assert fr.legenda_ancorada("qualquer coisa", "") == "qualquer coisa"


def test_capitulo_util_descarta_rotulo_de_janela_de_paginas():
    # O worker de OCR nomeia janelas assim quando o livro nao tem sumario.
    assert fr.capitulo_util("páginas 85-96") == ""
    assert fr.capitulo_util("paginas 85-96") == ""
    assert fr.capitulo_util("5 - Fungos") == "5 - Fungos"
    assert fr.capitulo_util("") == ""


def test_parece_legenda():
    assert fr.parece_legenda("Figura 4.1 — algo")
    assert not fr.parece_legenda("como mostra a figura 4.1, o ciclo")
