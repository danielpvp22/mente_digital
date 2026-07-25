"""Triagem do aparato editorial (2026-07-25): o que NÃO vira átomo.

Amostras REAIS do Amabis (628p) — foi contra elas que os limiares foram
calibrados. O teste mais importante é o último: prosa de verdade PASSA. Descartar
conteúdo é perda permanente, então o filtro erra para o lado de manter.
"""
from mente_digital import triagem

INDICE = 4 * """Esporozoário, ver apicomplexo
Esporozoito, ver malária
Esporulação, (em bactérias) 65-66, 65i, ver fungo (reprodução assexuada)
Esqueleto hidrostático, 277, 277i
Esqueleto humano, (partes) 538-540, 537i
Esquistossomose, 267i, 315i, (ciclo de vida) 321-322, 322i
Esquizocelomado, 272, ver também celoma
Estame, me 401, 401i, 402i, ver também flor
"""

CREDITOS = 4 * """Créditos das fotos
Parte I — Abertura: Fernando Favoreto/CID
Capitulo 1 — Abertura: Tony Craddock/SPL-Stock Photos • 1.1 (esquerda) Luiz Antonio/CID; (inferiores) Gilberto Mártho • 1.2 (superior, a partir da esquerda) Esgueva/CID, Fabio Colombini, Haroldo Palo Jr./Kino; (meio) Fabio Colombini, Ignacio Sabater/CID, Oliver Strewe
"""

EXPEDIENTE = 4 * """BIOLOGIA DOS ORGANISMOS
Coordenação editorial: José Luiz Carvalho da Cruz
Edição de texto: Elena Versolato
Assistência editorial: Tereza Amorim Costa
Preparação de texto: Maria Aparecida Rodrigues
Projeto gráfico: Ricardo Van Steen
Diagramação: Setup Bureau
Todos os direitos reservados. ISBN 978-85-16-00000-0
Impressão e acabamento: Gráfica
Dados Internacionais de Catalogação na Publicação
"""

CONTEUDO = """A partir desse ancestral comum, teriam surgido dois ramos: um deles continuou como um simples agregado de células pouco especializadas, sem formar tecidos verdadeiros, originando a linhagem da qual descendem os poríferos atuais; o outro desenvolveu células com maior grau de especialização, organizadas em tecidos, originando o ancestral de todos os outros animais.

Os animais desse segundo grupo teriam evoluído, por sua vez, em duas linhagens diferentes: uma delas teria originado os cnidários atuais, cujos adultos têm simetria radial e apenas dois folhetos germinativos; a outra linhagem deu origem aos animais com simetria bilateral e desenvolveu um terceiro folheto germinativo, o mesoderma.
"""


def test_indice_remissivo_e_descartado():
    fora, motivo = triagem.avaliar(INDICE)
    assert fora and "índice remissivo" in motivo


def test_creditos_de_fotos_sao_descartados():
    # Créditos têm linhas LONGAS como a prosa — o que separa é a densidade de "/".
    assert triagem.razao_prosa(CREDITOS) > 0.5
    fora, motivo = triagem.avaliar(CREDITOS)
    assert fora and "créditos" in motivo


def test_expediente_e_descartado():
    fora, motivo = triagem.avaliar(EXPEDIENTE)
    assert fora and ("expediente" in motivo or "aparato" in motivo)


def test_texto_curto_nao_rende_atomo():
    assert triagem.avaliar("Capítulo 4")[0]


def test_CONTEUDO_REAL_PASSA():
    """O teste que importa: descartar conteúdo é perda permanente."""
    fora, motivo = triagem.avaliar(CONTEUDO)
    assert not fora, f"conteúdo real foi descartado como '{motivo}'"
    assert motivo == "conteúdo"


def test_conteudo_com_numeros_e_barras_moderados_passa():
    # Biologia cita unidades e números sem ser índice: não pode disparar o filtro.
    texto = (
        "A concentração de oxigênio dissolvido na água varia entre 5 mg/L e 9 mg/L "
        "conforme a temperatura, e essa variação afeta diretamente a respiração dos "
        "peixes, que precisam bombear mais água pelas brânquias em ambientes quentes. "
        "Em experimentos conduzidos a 25 graus, a taxa metabólica aumentou 30 por cento "
        "em relação ao controle mantido a 15 graus, o que confirma a dependência térmica "
        "do metabolismo dos ectotérmicos descrita nas seções anteriores deste capítulo.\n"
    ) * 3
    assert not triagem.avaliar(texto)[0]


def test_filtrar_lotes_separa_e_explica():
    bons, motivos = triagem.filtrar_lotes([CONTEUDO, INDICE, CREDITOS])
    assert bons == [CONTEUDO]
    assert len(motivos) == 2 and all(isinstance(m, str) for m in motivos)
