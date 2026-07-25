"""Triagem do que NÃO vira átomo — ingestão de livros (2026-07-25).

Um livro não é só conteúdo: tem capa, ficha catalográfica, sumário, índice
remissivo, créditos de fotos e listas de referências. Medido no Amabis (628p, 53
capítulos): o capítulo 52 é 53 mil caracteres de índice remissivo puro
("Esporulação, (em bactérias) 65-66, 65i"), o 53 são créditos de fotos e o 1 é
capa + expediente. Atomizar isso gastaria ~10% da GPU do livro para produzir
átomos como "Esporulação aparece na página 65" — ruído permanente no vault, que
depois ainda apareceria em respostas.

A decisão é PURA e por SINAL MEDIDO, não por lista de páginas (que muda a cada
livro). Três sinais que separam prosa de aparato editorial:

- prosa: fração do texto em linhas LONGAS. Corpo de livro é parágrafo; índice e
  crédito são linhas curtas empilhadas.
- números: dígitos por letra. Índice remissivo é uma metralhadora de páginas.
- vocabulário de aparato: "créditos das fotos", "ISBN", "coordenação editorial"…
  contado por densidade, não por presença — o corpo do livro pode citar "ISBN"
  uma vez sem ser expediente.

Erra para que lado: na dúvida MANTÉM. Descartar conteúdo real é perda permanente
(o dono não relê 628 páginas para conferir); manter um pouco de ruído é barato e
a consolidação (Fase 2) ainda vai fundir o que sobrar de repetido.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# Vocabulário de APARATO editorial (não de conteúdo). Comparado sobre texto
# normalizado em minúsculas; o peso está na DENSIDADE, não na presença.
_TERMOS_APARATO = (
    "creditos das fotos", "credito das fotos", "coordenacao editorial",
    "edicao de texto", "assistencia editorial", "preparacao de texto",
    "todos os direitos reservados", "isbn", "dados internacionais de catalogacao",
    "impressao e acabamento", "revisao tecnica", "projeto grafico",
    "diagramacao", "editoracao eletronica", "indice remissivo",
    "ficha catalografica", "sumario", "referencias bibliograficas",
    "bibliografia recomendada", "sobre os autores",
)

# Entrada de índice remissivo: "Palavra, 65-66, 65i" / "Esqueleto humano, (partes) 538-540"
_ENTRADA_INDICE = re.compile(
    r"^\s*[A-ZÀ-Ú][^,\n]{1,60},\s*(?:\(|ver\b|\d)", re.MULTILINE)

_SEM_ACENTO = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ",
                            "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC")


def razao_prosa(texto: str, minimo_linha: int = 80) -> float:
    """Fração dos caracteres que está em linhas longas (prosa). 0..1."""
    linhas = [ln.strip() for ln in (texto or "").splitlines() if ln.strip()]
    total = sum(len(ln) for ln in linhas)
    if not total:
        return 0.0
    return sum(len(ln) for ln in linhas if len(ln) >= minimo_linha) / total


def razao_numerica(texto: str) -> float:
    """Dígitos por letra. Índice remissivo é uma metralhadora de números."""
    digitos = sum(c.isdigit() for c in texto or "")
    letras = sum(c.isalpha() for c in texto or "")
    return digitos / letras if letras else 1.0


def densidade_indice(texto: str) -> float:
    """Entradas de índice remissivo por 1000 caracteres."""
    if not texto:
        return 0.0
    return 1000.0 * len(_ENTRADA_INDICE.findall(texto)) / len(texto)


def densidade_barras(texto: str) -> float:
    """Barras por 1000 caracteres. Lista de CRÉDITOS é atribuição atrás de
    atribuição ("Tony Craddock/SPL-Stock Photos", "Luiz Antonio/CID"): medido no
    Amabis, 17,0 no capítulo de créditos contra 0,0 num capítulo de conteúdo. É o
    sinal que separa crédito de prosa — os dois têm linhas longas, então `prosa`
    sozinha não distingue."""
    if not texto:
        return 0.0
    return 1000.0 * texto.count("/") / len(texto)


def termos_aparato(texto: str) -> int:
    """Quantos termos de aparato editorial distintos aparecem."""
    n = (texto or "").translate(_SEM_ACENTO).lower()
    return sum(1 for t in _TERMOS_APARATO if t in n)


def avaliar(texto: str) -> Tuple[bool, str]:
    """(descartavel, motivo). Puro — os limiares foram calibrados contra os 53
    capítulos reais do Amabis (ver o script de medição no commit)."""
    if len((texto or "").strip()) < 400:
        return True, "texto curto demais para render ideia"
    prosa = razao_prosa(texto)
    indice = densidade_indice(texto)
    numeros = razao_numerica(texto)
    aparato = termos_aparato(texto)
    barras = densidade_barras(texto)
    if indice >= 1.0:
        return True, f"índice remissivo (entradas/1k={indice:.1f})"
    if barras >= 5.0:
        return True, f"lista de créditos (barras/1k={barras:.1f})"
    if prosa < 0.30 and numeros > 0.05:
        return True, f"aparato editorial (prosa={prosa:.2f}, num={numeros:.3f})"
    if aparato >= 3 and prosa < 0.55:
        return True, f"expediente/créditos ({aparato} termos, prosa={prosa:.2f})"
    return False, "conteúdo"


def filtrar_lotes(lotes: List[str]) -> Tuple[List[str], List[str]]:
    """Separa (aproveitáveis, motivos_dos_descartados). Usado ANTES de gastar o
    LLM: um lote descartado é uma chamada de GPU economizada e um átomo-ruído a
    menos no vault."""
    bons, motivos = [], []
    for lote in lotes:
        descartavel, motivo = avaliar(lote)
        (motivos if descartavel else bons).append(motivo if descartavel else lote)
    return bons, motivos
