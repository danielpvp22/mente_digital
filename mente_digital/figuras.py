"""Figuras dos livros — Fase 5a (2026-07-25): extrair, comprimir e vincular.

O caso: um átomo sobre fotossíntese fica muito melhor com o diagrama do ciclo ao
lado. As figuras já estão DENTRO do PDF; aqui elas viram arquivos no vault e
wikilinks nas notas — navegáveis no Obsidian e renderizáveis no chat.

FORMATO: WebP q80. Medido numa figura real do Raven (1063x408, JPEG de 251 KB
dentro do PDF): PNG 882 KB · WebP sem perdas 692 · JPEG q85 143 · **WebP q80 114**
· WebP q70 88. Ou seja, WebP q80 é 2,2x menor que o JPEG que já estava lá, com
perda imperceptível em diagrama e foto — é o ponto ótimo pedido.

LIMITE HONESTO: o LLM do projeto é de TEXTO. Ele não "vê" a figura e não julga se
ela ilustra o ponto; ele apenas repassa um wikilink que estava no contexto. Serve
para "a Figura 4.1 mostra o ciclo", não para escolha visual.

ESCANEADO fica de fora desta fase: num PDF de imagem, a "imagem embutida" é a
página inteira fotografada, não a figura. Esse caso pede as caixas do
`<|grounding|>` do OCR (Fase 5c).

Decisões puras aqui; o IO (fitz/Pillow, import tardio) está marcado no fim.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

# "Figura 4.1 — texto", "Fig. 12 -", "FIGURA 3.2:" ... o número é o que casa com o
# corpo do átomo; a legenda é o que dá contexto na nota (e vira texto pesquisável).
_LEGENDA_RE = re.compile(
    r"^\s*(?:figuras?|fig\.?)\s*([\dA-Z]+(?:[.\-]\d+)?)\s*[—\-–:.]\s*(.{4,300})",
    re.IGNORECASE | re.MULTILINE,
)

# MENÇÃO no corpo: "…o processo de fotossíntese (Figura 1.5)." Medido no Raven —
# lá a legenda de verdade está DENTRO da imagem, e a camada de texto só traz estas
# referências (1 de 60 figuras casava a regex de legenda acima). A frase que cita a
# figura descreve o que ela mostra, então serve ao mesmo propósito: dar ao RAG
# palavras para ACHAR a figura. É contexto, não legenda — e o nome do campo diz isso.
_MENCAO_RE = re.compile(
    r"([^.\n]{15,200}?\(?\s*(?:figuras?|fig\.?)\s*([\dA-Z]+(?:[.\-]\d+)?)\s*\)?)",
    re.IGNORECASE,
)


def vale_a_pena(largura: int, altura: int, min_lado: int) -> bool:
    """Descarta ícone, filete e ornamento: só entra o que tem os DOIS lados acima do
    mínimo. Sem isto, um livro didático traria centenas de bulletpoints decorativos."""
    return largura >= min_lado and altura >= min_lado


def nome_arquivo(livro_slug: str, pagina: int, indice: int) -> str:
    """Nome estável e ordenável: reprocessar o livro sobrescreve em vez de duplicar."""
    return f"{livro_slug}_p{pagina:04d}_f{indice}.webp"


def casar_legendas(texto_pagina: str) -> Dict[str, str]:
    """{'4.1': 'legenda...'} a partir do texto da página. Puro.

    Preferência: LEGENDA de verdade (linha "Figura 4.1 — ..."). Onde o livro não
    tem legenda na camada de texto (caso do Raven, em que ela está dentro da
    imagem), cai para a FRASE do corpo que cita a figura — pior como rótulo,
    igualmente útil como pista de busca."""
    achadas = {
        num.strip(): " ".join(legenda.split())[:300]
        for num, legenda in _LEGENDA_RE.findall(texto_pagina or "")
    }
    for frase, num in _MENCAO_RE.findall(texto_pagina or ""):
        achadas.setdefault(num.strip(), " ".join(frase.split())[:300])
    return achadas


def legenda_para(legendas: List[str], indice: int, n_figuras: int) -> str:
    """Escolhe a legenda de UMA figura entre as encontradas na página. Puro.

    Regra medida no Raven: exigir "uma legenda por página" casava 1 de 60 figuras —
    conservador a ponto de ser inútil. Aqui: se a página tem tantas legendas quanto
    figuras, pareia na ORDEM (que é a ordem de leitura do PDF); se os números
    divergem, devolve TODAS as legendas da página. É impreciso de propósito — a
    legenda serve para o RAG ACHAR a figura ("o diagrama do ciclo"), e um pouco de
    contexto a mais acha; contexto nenhum não acha nada."""
    if not legendas:
        return ""
    if len(legendas) == n_figuras:
        return legendas[indice]
    return " | ".join(legendas)[:300]


def figuras_do_intervalo(figuras: List[dict], pagina_inicio: int,
                         pagina_fim: int) -> List[dict]:
    """As figuras cujas páginas caem no intervalo de um capítulo (1-based, inclusivo
    no início e no fim, igual à proveniência dos jobs). Puro."""
    return [f for f in figuras
            if pagina_inicio <= int(f.get("pagina", 0)) <= pagina_fim]


def bloco_markdown(figuras: List[dict], subpasta: str) -> str:
    """Seção de figuras para a nota do capítulo: wikilink do Obsidian + legenda.

    A legenda entra como TEXTO (não só alt) de propósito: é ela que faz a figura
    ser encontrável pelo RAG — "o diagrama do ciclo de Krebs" casa a legenda, e o
    átomo que a contém traz o link junto."""
    if not figuras:
        return ""
    linhas = ["", "## Figuras", ""]
    for f in figuras:
        legenda = f.get("legenda") or f"página {f.get('pagina', '?')}"
        linhas.append(f"- ![[{subpasta}/{f['arquivo']}]] — {legenda}")
    return "\n".join(linhas)


# --- IO: fitz + Pillow, import tardio (o servidor sobe sem eles) --------------
def para_webp(dados: bytes, qualidade: int, max_lado: int) -> Optional[bytes]:
    """Converte a imagem embutida para WebP. None se não for imagem decodificável
    (o PDF guarda máscaras e perfis que o Pillow recusa — não é erro, é sujeira)."""
    import io

    from PIL import Image

    try:
        img = Image.open(io.BytesIO(dados))
        img = img.convert("RGB")
        if max_lado and max(img.size) > max_lado:
            img.thumbnail((max_lado, max_lado))
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=qualidade, method=6)
        return buf.getvalue()
    except Exception:
        return None


def extrair_de_pdf(pdf: Path, destino: Path, livro_slug: str, min_lado: int,
                   qualidade: int, max_lado: int, limite: int = 2000) -> List[dict]:
    """Extrai as figuras do PDF em WebP para `destino/<livro_slug>/` — UMA PASTA POR
    LIVRO dentro do vault, para o Obsidian não virar um depósito único de milhares
    de imagens misturadas. O `arquivo` devolvido já vem com a subpasta
    ("raven/raven_p0059_f1.webp"), então o wikilink aponta certo sozinho.

    Devolve [{arquivo, pagina, legenda}] — a lista que os jobs carregam.

    `limite` é um teto de sanidade: um livro com milhares de imagens não pode
    encher o vault num descuido (o dono é avisado no log de quem chamou)."""
    import gc

    import fitz

    pasta_livro = Path(destino) / livro_slug
    pasta_livro.mkdir(parents=True, exist_ok=True)
    # Stream mode (bytes), NÃO o caminho: no Windows o PyMuPDF segura o handle
    # quando falha em PDF inválido, e o `move` posterior do arquivo bate em
    # "em uso" — o livro ficaria preso na fila. Mesma lição do livro.extrair_pdf.
    doc = fitz.open(stream=Path(pdf).read_bytes(), filetype="pdf")
    achadas: List[dict] = []
    try:
        for pno in range(doc.page_count):
            if len(achadas) >= limite:
                break
            pagina = doc.load_page(pno)
            legendas = list(casar_legendas(pagina.get_text("text")).values())
            # Coleta as VÁLIDAS antes de legendar: só sabendo quantas sobraram dá
            # para decidir se o pareamento 1:1 com as legendas é confiável.
            validas = []
            for info in pagina.get_images(full=True):
                try:
                    bruto = doc.extract_image(info[0])
                except Exception:
                    continue
                if not vale_a_pena(bruto.get("width", 0), bruto.get("height", 0), min_lado):
                    continue
                webp = para_webp(bruto["image"], qualidade, max_lado)
                if webp is not None:
                    validas.append(webp)
            for i, webp in enumerate(validas, start=1):
                nome = nome_arquivo(livro_slug, pno + 1, i)
                (pasta_livro / nome).write_bytes(webp)
                achadas.append({"arquivo": f"{livro_slug}/{nome}", "pagina": pno + 1,
                                "legenda": legenda_para(legendas, i - 1, len(validas))})
        # GUARDA DO LIVRO ESCANEADO (medido 2026-07-25): num PDF de imagem, a
        # "imagem embutida" é a PÁGINA inteira fotografada. Sinal inequívoco: ~1
        # imagem por página, e pesada. Amabis deu 627 "figuras" para 628 páginas a
        # 268 KB cada (168 MB de páginas escaneadas!); o Raven, digital de verdade,
        # deu 1008 para 1637 páginas a 47 KB. Sem esta guarda o vault engordaria
        # centenas de MB com retratos de página que não ilustram nada.
        if doc.page_count and len(achadas) >= 0.8 * doc.page_count:
            for f in achadas:
                (pasta_livro / Path(f["arquivo"]).name).unlink(missing_ok=True)
            achadas = []
    finally:
        doc.close()
        gc.collect()
    return achadas
