"""Ingestão de livros — Fase 1 (2026-07-25): as partes PURAS.

O fluxo inteiro: scripts/ingerir_livro.py extrai um PDF DIGITAL (PyMuPDF) e grava
JOBS DE CAPÍTULO em dados/ingestao/pendentes/; o scheduler os consome NO IDLE
(restrição do dono: coleta/atomização nunca competem com a conversa) via
EtlProcessor.ingestao_livros — átomos com proveniência livro/capítulo/página +
UMA nota-síntese por capítulo (hierárquico: a atomização pura fragmenta o
argumento longo; a síntese preserva a tese).

Este módulo segue o padrão de agenda.py/verbalizar.py: puro, sem IO, sem settings
— tudo injetado, tudo testável sem GPU. Livro ESCANEADO (só imagem) é detectado e
recusado aqui; ele espera o worker OCR da Fase 3 (Unlimited-OCR GGUF em venv
própria), que vai desembocar NESTES MESMOS jobs — o pipeline não muda.
"""
from __future__ import annotations

import re
import statistics
from typing import Dict, List, Tuple

# Abaixo disto de texto por página (mediana), o PDF é imagem escaneada — não há o
# que extrair sem OCR. 200 chars ≈ um parágrafo curto; página digital real tem >1k.
ESCANEADO_CHARS_POR_PAGINA = 200


def parece_escaneado(chars_por_pagina: List[int],
                     limiar: int = ESCANEADO_CHARS_POR_PAGINA) -> bool:
    """True se o PDF não tem texto selecionável (mediana de chars/página < limiar).
    Mediana, não média: capa/sumário digitais num livro escaneado não podem mascarar."""
    if not chars_por_pagina:
        return True
    return statistics.median(chars_por_pagina) < limiar


def slug(texto: str, max_len: int = 40) -> str:
    """Nome seguro de arquivo (Windows incluso) a partir de um título livre."""
    s = re.sub(r"[^\w\s-]", "", texto, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:max_len].strip("-") or "livro"


def fatiar_lotes(texto: str, max_chars: int) -> List[str]:
    """Fatia o capítulo em lotes <= max_chars SEM perder conteúdo, cortando em
    fronteira de parágrafo (parágrafo-monstro leva corte duro). Cada lote é uma
    chamada do LLM de atomização — max_chars calibrado para caber no n_ctx."""
    paras = [p.strip() for p in texto.split("\n\n") if p.strip()]
    lotes: List[str] = []
    atual = ""
    for p in paras:
        while len(p) > max_chars:
            if atual:
                lotes.append(atual)
                atual = ""
            lotes.append(p[:max_chars])
            p = p[max_chars:].strip()
        if not p:
            continue
        if atual and len(atual) + 2 + len(p) > max_chars:
            lotes.append(atual)
            atual = p
        else:
            atual = f"{atual}\n\n{p}" if atual else p
    if atual:
        lotes.append(atual)
    return lotes


def _job(livro: str, num: int, titulo_cap: str, ini: int, fim: int,
         paginas: List[str]) -> Dict:
    return {
        "livro": livro,
        "capitulo": num,
        "titulo_cap": titulo_cap,
        "pagina_inicio": ini + 1,   # 1-based: é o que o leitor vê no PDF
        "pagina_fim": fim,
        "texto": "\n\n".join(paginas[ini:fim]).strip(),
    }


def montar_jobs(titulo: str, paginas: List[str],
                toc: List[Tuple[int, str, int]],
                paginas_por_bloco: int = 12) -> List[Dict]:
    """Segmenta o livro em jobs de capítulo. Com TOC (PyMuPDF get_toc) de nível 1,
    os capítulos são REAIS — proveniência fiel. Sem TOC utilizável, cai para
    janelas fixas de páginas: proveniência de página segue exata, só o rótulo do
    "capítulo" que fica sintético. Jobs vazios (páginas sem texto) são descartados."""
    caps = [(t.strip() or f"cap. {i+1}", max(0, p - 1))
            for i, (nivel, t, p) in enumerate(toc) if nivel == 1]
    caps = [c for c in caps if 0 <= c[1] < len(paginas)]
    jobs: List[Dict] = []
    if len(caps) >= 2:
        for i, (titulo_cap, ini) in enumerate(caps):
            fim = caps[i + 1][1] if i + 1 < len(caps) else len(paginas)
            if fim <= ini:
                continue
            jobs.append(_job(titulo, len(jobs) + 1, titulo_cap, ini, fim, paginas))
    else:
        n = max(1, paginas_por_bloco)
        for ini in range(0, len(paginas), n):
            fim = min(ini + n, len(paginas))
            jobs.append(_job(titulo, len(jobs) + 1,
                             f"páginas {ini + 1}-{fim}", ini, fim, paginas))
    return [j for j in jobs if j["texto"]]
