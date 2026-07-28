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
