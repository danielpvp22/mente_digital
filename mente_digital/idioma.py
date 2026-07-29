"""Detecção de átomo escrito no idioma ERRADO (2026-07-28). Puro/testável.

Por que existe: livro em inglês atomizado com um prompt que exige PT-BR funciona
em ~97% dos lotes, mas o modelo ESPELHA o idioma da fonte quando o trecho é muito
técnico. Medido na Cannabis Encyclopedia: 185 de 5.907 átomos (3,1%) saíram com
o título em inglês, 141 deles com o corpo junto. A pergunta do dono chega em
português, então esse átomo fica difícil de alcançar — o e5 é multilíngue, mas o
aterramento LÉXICO do gate (`textutils.contem_alguma`) é casamento de palavra, e
ele não casa nada.

MEDIR PELO TÍTULO, E DESCONTANDO A PROVENIÊNCIA. As duas armadilhas, ambas
pagas em medição:
1. Um filtro por palavras inglesas no CORPO acusa 17% da base, mas a maioria é
   átomo em português citando nome de obra ou de cultivar ("o livro *Marijuana
   and Medicine* é citado…"). Medição de 2026-07-25.
2. Descontar a proveniência é obrigatório: o título das notas-síntese é um
   TEMPLATE português que embute o nome do livro em inglês —
   "Síntese — The Cannabis Encyclopedia (…) — edição web: Water – Chapter 20".
   Sem descontar, o detector acusou 477 átomos e 463 eram esse falso positivo.

A regra final é assimétrica de propósito: exige EVIDÊNCIA de inglês e AUSÊNCIA
de português. Reescrever um átomo que já está certo é pior do que deixar um
errado — o primeiro estraga o que funciona, o segundo só continua difícil de
achar.
"""
from __future__ import annotations

import re
from typing import Tuple

# Palavras FUNCIONAIS, não de conteúdo: é a presença delas que denuncia a língua
# da FRASE. Nome próprio e termo técnico em inglês aparecem em título português
# ("Teste de pH com kit químico"), e por isso substantivo não entra nesta lista.
FUNC_EN = frozenset("""
the of and for with from that this these those how what when where why
in on at to by as is are was were be been being do does did
your you their its his her our not no more most than then into over under
""".split())

FUNC_PT = frozenset("""
de da do das dos e para com por que o a os as um uma uns umas
em no na nos nas ao aos à às pelo pela pelos pelas como quando onde porque
é são foi eram ser sendo seu sua seus suas não mais menos entre sobre
até após durante antes depois cada todo toda todos todas
""".split())

_PAL = re.compile(r"[a-zà-ÿ]+")

# Ruído de PROVENIÊNCIA dentro do título: nome da obra, rótulo de capítulo e o
# prefixo do template de síntese. Não é a língua da frase — é etiqueta.
_PROVENIENCIA = re.compile(
    r"(the cannabis encyclopedia|jorge cervantes|marijuana horticulture|"
    r"edi[çc][ãa]o web|s[íi]ntese|[–\-—]\s*chapter\s*\d+)", re.IGNORECASE)


def limpar_proveniencia(titulo: str) -> str:
    """Tira nome de obra, rótulo de capítulo e conteúdo de parênteses.

    Parênteses saem porque é onde moram sigla e termo técnico preservados de
    propósito ("magnésio (Mg)", "receptor CB1 (CB1 receptor)") — contá-los como
    inglês reprovaria justamente o átomo bem traduzido."""
    t = _PROVENIENCIA.sub(" ", titulo or "")
    t = re.sub(r"\([^)]*\)", " ", t)
    return " ".join(t.split())


def contar_funcionais(texto: str) -> Tuple[int, int, int]:
    """`(inglesas, portuguesas, total_de_palavras)`."""
    toks = _PAL.findall((texto or "").lower())
    return (sum(1 for t in toks if t in FUNC_EN),
            sum(1 for t in toks if t in FUNC_PT),
            len(toks))


# Abaixo disto a frase não tem palavra funcional suficiente para decidir, e um
# título de 2 palavras ("Poda apical") não deve ser reescrito por chute.
MIN_PALAVRAS = 3


def em_ingles(titulo: str) -> bool:
    """O título está em inglês? Exige evidência de inglês E ausência de português."""
    ingles, port, n = contar_funcionais(limpar_proveniencia(titulo))
    if n < MIN_PALAVRAS:
        return False
    return ingles >= 1 and port == 0


# --- glossário do domínio (ordem do dono, 2026-07-28) ------------------------
# "Quero manter palavras técnicas do meio em inglês, como 'bud'."
#
# Não é preferência de estilo: é conserto de corrupção. Medido no lote de
# 2026-07-28, o modelo de 4B não conhece a gíria do cultivo e produziu
#   "resinous buds"     -> "bordas resinosas"      (bud virou borda)
#   "Cut Flower Buds"   -> "borboletas de flor"    (bud virou borboleta)
#   "fungus gnat"       -> "formiga de fungo"      (mosquito virou formiga)
#   "flypaper"          -> "papel de fly"          (metade traduzida)
# Palavra que o modelo não conhece ele CHUTA, e o chute entra no vault como se
# fosse fato. A tabela abaixo tira a decisão dele.
#
# Chave = termo em inglês (minúsculo); valor = o que deve aparecer em PT-BR.
# Valor IGUAL à chave significa "não traduza" — é assim que o jargão do meio
# atravessa a tradução intacto.
GLOSSARIO = {
    "bud": "bud", "buds": "buds", "nug": "nug", "nugs": "nugs",
    "cola": "cola", "colas": "colas", "sinsemilla": "sinsemilla",
    "kief": "kief", "hash": "hash", "trim": "trim", "trimming": "trimming",
    "curing": "curing", "topping": "topping", "fimming": "fimming",
    "scrog": "scrog", "sog": "sog", "clone": "clone", "clones": "clones",
    "trichome": "tricoma", "trichomes": "tricomas",
    # insetos e utensílios: aqui o modelo não errou o idioma, errou o BICHO
    "fungus gnat": "fungus gnat", "fungus gnats": "fungus gnats",
    "gnat": "fungus gnat", "gnats": "fungus gnats",
    "mealybug": "cochonilha", "mealybugs": "cochonilhas",
    "flypaper": "papel pega-moscas",
    "seedling": "muda", "seedlings": "mudas",
    # Vistos na passada de tradução com o 8B (2026-07-28): "cuttings" virou
    # "enxotas" (palavra que não existe) e "spirits" virou "Espíritos".
    "cutting": "estaca", "cuttings": "estacas",
    "spirits": "destilados",
}

# Traduções ERRADAS já gravadas no vault: (errado, certo, TERMO EXIGIDO no
# original em inglês). Substituição cirúrgica, porque o corpo original não foi
# preservado (só o título) e retraduzir o corpo seria pedir ao modelo que
# "melhorasse" um texto sem fonte para comparar.
#
# O 3º campo é a PROVA, e ele existe por um erro que o ensaio a seco pegou antes
# de gravar: trocar "borboleta" por "bud" às cegas corrompia notas em que a
# palavra está CERTA — "coevolução borboleta-monarca e Asclepias" (do Raven) e
# "refletor borboleta", um tipo de refletor. A troca só vale quando o inglês
# daquela nota de fato dizia 'bud'. Mesma lição da precedência entre obras:
# relação declarada, nunca inferida da semelhança.
CORRECOES = (
    ("borboletas de flor cortadas", "buds de flor cortados", "bud"),
    # O PLURAL precisa de entrada própria: `\bborboleta\b` não casa "borboletas",
    # e foi exatamente por aí que a regressão escapou da pós-checagem.
    ("borboletas de flor", "buds de flor", "bud"),
    ("borboletas", "buds", "bud"),
    ("borboleta", "bud", "bud"),
    # Palavra INVENTADA pelo modelo de 4B (não existe em português). O 3º campo
    # continua sendo a prova: só troca onde o inglês dizia aquilo.
    ("podre excessivo", "poda excessiva", "pruning"),
    ("atrapações amarelas adesivas", "armadilhas adesivas amarelas", "sticky trap"),
    ("atrapa amarela", "armadilha adesiva amarela", "sticky trap"),
    ("bordas resinosas", "buds resinosos", "bud"),
    ("borda resinosa", "bud resinoso", "bud"),
    # O 8B tem a sua PRÓPRIA tradução errada de "bud" — visto no reparo de
    # 2026-07-28: "heavy buds break branches" virou "os cogumelos pesados quebrem
    # as ramificações". Erro de FATO, não de estilo: cogumelo é fungo.
    ("cogumelos", "buds", "bud"),
    ("cogumelo", "bud", "bud"),
    ("formigas-fungo", "fungus gnats", "gnat"),
    ("formigas de fungo", "fungus gnats", "gnat"),
    ("formiga de fungo", "fungus gnat", "gnat"),
    ("fungo-gnat", "fungus gnat", "gnat"),
    ("papel de fly", "papel pega-moscas", "flypaper"),
)


def linha_glossario(termos=None) -> str:
    """O glossário como linha de prompt: 'bud = bud, gnats = fungus gnats, …'.

    Só os termos PEDIDOS quando `termos` é dado — mandar as 30 entradas numa
    tradução que não usa nenhuma delas só gasta prefill e convida o modelo a
    enfiar jargão onde não havia."""
    itens = GLOSSARIO if termos is None else {
        k: v for k, v in GLOSSARIO.items() if k in set(termos)}
    return ", ".join(f"{k} = {v}" for k, v in itens.items())


def termos_do_glossario(texto: str) -> list:
    """Os termos do glossário presentes no texto (casando palavra inteira)."""
    baixo = (texto or "").lower()
    return [t for t in GLOSSARIO
            if re.search(rf"\b{re.escape(t)}\b", baixo)]


def aplicar_correcoes(texto: str, original_ingles: str) -> tuple:
    """`(texto_corrigido, quantas)` — troca as traduções erradas CONHECIDAS.

    `original_ingles` é a prova: só corrige o que o inglês daquela nota de fato
    dizia. Sem ele nada é trocado — uma nota sem original preservado (as que
    esta passada nunca tocou) fica intacta por definição.

    Case-insensitive e preservando a inicial maiúscula, porque o erro aparece
    tanto no meio da frase quanto abrindo título."""
    fonte = (original_ingles or "").lower()
    if not fonte:
        return texto or "", 0
    saida, n = texto or "", 0
    for errado, certo, exige in CORRECOES:
        if not re.search(rf"\b{re.escape(exige)}", fonte):
            continue
        padrao = re.compile(rf"\b{re.escape(errado)}\b", re.IGNORECASE)

        def _troca(m, certo=certo):
            return certo.capitalize() if m.group(0)[:1].isupper() else certo

        saida, k = padrao.subn(_troca, saida)
        n += k
    return saida, n


def corpo_em_ingles(corpo: str) -> bool:
    """O CORPO está em inglês? Régua mais frouxa que a do título de propósito.

    O corpo é longo e mistura termo técnico com prosa, então exigir zero palavra
    portuguesa reprovaria quase tudo. Aqui basta o inglês PREDOMINAR — e o
    chamador só pergunta isto depois de o título já ter sido reprovado, então a
    decisão nunca depende só desta linha."""
    ingles, port, n = contar_funcionais(corpo)
    if n < MIN_PALAVRAS:
        return False
    return ingles > port
