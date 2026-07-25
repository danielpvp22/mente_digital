"""Colheita acadêmica — Fase 4 (2026-07-25): as partes PURAS.

O que é: no idle, o sistema busca PDFs acadêmicos sobre (a) os temas que o dono
mais REUSA do vault e (b) as LACUNAS que ficaram sem resposta — as duas tabelas
que o projeto já mantém. Os papers aceitos entram na MESMA fila de jobs da
ingestão de livros, então herdam de graça a proveniência e a síntese hierárquica.

Por que via DDGS e não Google Scholar: o Scholar não tem API, proíbe scraping e
bloqueia com CAPTCHA — construir sobre isso é construir sobre areia. `filetype:pdf`
no DDGS é o meio-termo honesto (público, sem burlar nada). O preço: os resultados
NÃO trazem metadados (autor/ano/venue) e vêm com lixo de ranking — por isso os
filtros abaixo são conservadores e a proveniência registra a URL, sem inventar
citação formal. Um upgrade futuro seria a API do OpenAlex para saber o que vale
baixar antes de baixar.

Puro no padrão livro.py/consolidacao.py: strings entram, strings saem.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

# Termos que empurram o resultado para material acadêmico sem restringir a UMA
# fonte (site: fecharia demais e o dono quer amplitude).
_VIES_ACADEMICO = "(research OR paper OR study OR review)"

# Domínios que servem "PDF" que na prática é paywall/landing — não vale o download.
_DOMINIOS_VETADOS = (
    "researchgate.net",     # exige login; o .pdf é interstitial
    "academia.edu",         # idem
    "scribd.com",
    "slideshare.net",
    "books.google",
)


def montar_query(termos: str) -> str:
    """Query de busca de PDF acadêmico a partir dos termos de um tema/lacuna."""
    return f"{termos.strip()} {_VIES_ACADEMICO} filetype:pdf"


def _e_url_pdf(url: str) -> bool:
    caminho = urlparse(url).path.lower()
    return caminho.endswith(".pdf") or "/pdf/" in caminho


def filtrar_pdfs(resultados: List[dict]) -> List[Dict[str, str]]:
    """Fica só com o que é PDF de verdade, sem domínio vetado, sem URL repetida.
    Aceita o formato do ddgs.text() ({'href'|'url', 'title'})."""
    vistos, out = set(), []
    for r in resultados or []:
        url = (r.get("href") or r.get("url") or "").strip()
        if not url or url in vistos or not _e_url_pdf(url):
            continue
        host = (urlparse(url).hostname or "").lower()
        if any(v in host for v in _DOMINIOS_VETADOS):
            continue
        vistos.add(url)
        out.append({"url": url, "titulo": (r.get("title") or "").strip()})
    return out


def titulo_do_paper(titulo_busca: str, url: str) -> str:
    """Nome legível para a proveniência. O título do DDGS costuma vir sujo
    ('[PDF] Foo bar - Journal'); sem título utilizável, cai no nome do arquivo."""
    t = re.sub(r"^\s*\[?pdf\]?\s*", "", titulo_busca or "", flags=re.I).strip()
    t = re.sub(r"\s*[-|–]\s*[^-|–]{0,30}$", "", t).strip() if len(t) > 25 else t
    if len(t) >= 8:
        return t[:120]
    nome = urlparse(url).path.rsplit("/", 1)[-1] or "paper"
    return re.sub(r"\.pdf$", "", nome, flags=re.I).replace("_", " ")[:120] or "paper"


def job_de_paper(titulo: str, url: str, paginas: List[str],
                 min_chars: int) -> Optional[Dict]:
    """Um paper = UM job (não tem capítulo que valha segmentar; o fatiamento por
    lote do ETL já cuida do n_ctx). None se o texto extraído for raso — paywall,
    capa solta ou scan (que é caso do worker OCR, não daqui)."""
    texto = "\n\n".join(p.strip() for p in paginas if p.strip()).strip()
    if len(texto) < min_chars:
        return None
    return {
        "livro": titulo,                 # a proveniência dirá "Livro '<paper>'"
        "capitulo": 1,
        "titulo_cap": f"paper ({urlparse(url).hostname or 'web'})",
        "pagina_inicio": 1,
        "pagina_fim": len(paginas),
        "texto": texto,
        "url": url,                      # rastro da fonte (o job carrega a origem)
    }
