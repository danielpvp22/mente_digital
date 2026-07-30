"""Ingestão de livros — Fase 1 (2026-07-25): as partes PURAS.

O fluxo inteiro: scripts/ingerir_livro.py extrai um PDF DIGITAL (PyMuPDF) e grava
JOBS DE CAPÍTULO em dados/ingestao/pendentes/; o scheduler os consome NO IDLE
(restrição do dono: coleta/atomização nunca competem com a conversa) via
EtlProcessor.ingestao_livros — átomos com proveniência livro/capítulo/página +
UMA nota-síntese por capítulo (hierárquico: a atomização pura fragmenta o
argumento longo; a síntese preserva a tese).

Este módulo segue o padrão de agenda.py/verbalizar.py: as funções de decisão são
puras, sem settings — tudo injetado, tudo testável sem GPU. A ÚNICA exceção é
`extrair_pdf` no fim do arquivo (IO + import tardio do PyMuPDF): ela existe aqui
porque TRÊS chamadores precisam da mesma extração (o script manual, a pasta
vigiada e a colheita acadêmica da Fase 4) e duplicá-la seria pior.

Livro ESCANEADO (só imagem) é detectado e recusado; ele espera o worker OCR da
Fase 3 (Unlimited-OCR GGUF em venv própria), que vai desembocar NESTES MESMOS
jobs — o pipeline a jusante não muda.
"""
from __future__ import annotations

import re
import statistics
from typing import Dict, List, Optional, Tuple

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


def origem_do_job(job: Dict) -> str:
    """A PROVENIÊNCIA de um job de capítulo: `Livro 'X' — cap (p. 3-4)`.

    Ponto único de propósito. Esta string é o frontmatter `origem` de cada átomo
    e virou também a ÂNCORA DE PÁGINA que liga a figura attach-only ao texto que
    ela ilustra (`rag._anexos_colocados` casa por igualdade exata). Ela é montada
    em dois lugares distantes — aqui, quando o ETL atomiza, e no importador web,
    quando ele grava a nota da figura. Duas cópias que precisam bater caractere a
    caractere é uma que precisa existir."""
    cap = job.get("titulo_cap") or f"cap. {job.get('capitulo', '?')}"
    return (f"Livro '{job.get('livro', '?')}' — {cap} "
            f"(p. {job.get('pagina_inicio', '?')}-{job.get('pagina_fim', '?')})")


# A metade LEITORA do `origem_do_job` acima: o " (p. N-M)" que ele sempre escreve.
_PAGINA_RE = re.compile(r"\(p\.\s*\d+")


def e_origem_de_pagina(origem: str) -> bool:
    """A `origem` é uma PÁGINA de livro (co-locação real), ou um balaio? Puro/testável.

    A expansão por página e o anexo de figura tratam `origem` como "a mesma página".
    Isso só é verdade para o que o `origem_do_job` escreveu. Para átomo auto-colhido o
    campo guarda o BALDE de onde ele veio — medido no índice de 2026-07-30 (24.918
    chunks): 306 páginas de livro (mediana 37 átomos, máx. 69) contra 95 origens
    não-página com até **1.947 átomos na mesma chave** ('Orquestração-de-IA-…json',
    'Sintese', 'Conversa'). 54,2% do índice cai num balde maior que o teto de irmãos,
    e nesses o "irmão de página" é só a ordem do Chroma — 12 átomos de assunto
    nenhum a ver, os MESMOS em perguntas diferentes.
    """
    return bool(_PAGINA_RE.search(origem or ""))


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


# --- a ÚNICA função com IO (ver docstring do módulo) -------------------------
def extrair_pdf(caminho, fonte: Optional[bytes] = None) -> Tuple[List[str], List[Tuple[int, str, int]]]:
    """Devolve (páginas de texto, TOC) de um PDF. `fonte` (bytes) permite extrair
    um PDF baixado sem gravá-lo antes (colheita acadêmica). Import TARDIO do
    PyMuPDF: o servidor sobe sem ele, e só quem ingere paga o import."""
    import gc

    import fitz  # PyMuPDF

    doc = None
    try:
        doc = fitz.open(stream=fonte, filetype="pdf") if fonte else fitz.open(str(caminho))
        return [p.get_text("text") for p in doc], (doc.get_toc() or [])
    finally:
        # No Windows, um PDF inválido faz o `open` levantar SEGURANDO o handle do
        # arquivo — e aí o `replace()` que move o PDF para falhou/ bate em "arquivo
        # em uso" e ele fica preso na fila PARA SEMPRE, re-tentado a cada ciclo
        # (visto na suíte em 2026-07-25). Fechar o que abriu + coletar o que ficou
        # pendurado é o que garante que o arquivo possa ser movido depois.
        if doc is not None:
            doc.close()
        gc.collect()
