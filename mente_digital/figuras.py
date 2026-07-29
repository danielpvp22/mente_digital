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
from typing import Dict, List, Optional, Set

from mente_digital import textutils

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
    from mente_digital import figuras_recorte  # tardio: evita ciclo de import

    if not figuras:
        return ""
    linhas = ["", "## Figuras", ""]
    for f in figuras:
        legenda = f.get("legenda") or f"página {f.get('pagina', '?')}"
        # O EXTRATOR GRAVA UMA PASTA POR LIVRO, e o job carrega só o NOME do
        # arquivo. Montar `Figuras/<nome>` produz um link que não resolve —
        # medido em 2026-07-28: as 307 notas-síntese da Cannabis Encyclopedia
        # saíram com TODAS as figuras quebradas ("não foi encontrado" no
        # Obsidian), enquanto as notas de figura, que montam o caminho completo,
        # estavam certas. O slug sai do PRÓPRIO nome do arquivo, então isto vale
        # para qualquer livro, inclusive os que ainda nem foram processados.
        alvo = f"{subpasta}/{f['arquivo']}"
        linhas.append(f"- ![[{figuras_recorte.corrigir_alvo(alvo, subpasta) or alvo}]] "
                      f"— {legenda}")
    return "\n".join(linhas)


# --- as DUAS cópias da mesma legenda (2026-07-29) -----------------------------
# A legenda de uma figura vive em dois lugares: nesta linha de bloco (que a síntese
# do capítulo carrega) e na NOTA da figura. Elas nasceram iguais e DERIVARAM: na
# Cannabis Encyclopedia, medido em 2026-07-29, o bloco está em português e a nota
# continua no inglês do site — 1.452 das 1.818 notas. As funções abaixo leem uma
# ponta e escrevem na outra, sem inventar texto: a tradução já está no vault.
_LINHA_BLOCO_RE = re.compile(r"^-\s*!\[\[([^\]]+)\]\]\s*[—–-]\s*(.+?)\s*$")
_FM_RE = re.compile(r"^(---\r?\n)(.*?)(\r?\n---\r?\n)", re.DOTALL)

# O MESMO corte que `encyclopedia.py` e `figuras_recorte.py` aplicam ao CRIAR a
# nota (`## {titulo[:120]}`). Por isso o título é um PREFIXO do corpo em 606 das
# 1.452 notas afetadas: a legenda inteira vive no corpo, e o título é a versão
# cortada dela. Trocar só o título deixaria o inglês na linha de baixo.
TETO_TITULO = 120


def legendas_do_bloco(texto: str) -> Dict[str, str]:
    """`{nome da imagem sem extensão: legenda}` das linhas escritas por
    `bloco_markdown`. Casa o par pelo ARQUIVO, nunca por semelhança de texto —
    é a mesma âncora exata que a co-locação de figura já usa."""
    achados: Dict[str, str] = {}
    for linha in (texto or "").split("\n"):
        m = _LINHA_BLOCO_RE.match(linha.strip())
        if m:
            achados[Path(m.group(1)).stem] = m.group(2).strip()
    return achados


def _indice_do_corpo(linhas: List[str], idx_titulo: int, titulo: str) -> int:
    """A linha do CORPO que repete a legenda, ou -1 se não houver.

    É a primeira linha de conteúdo depois do título. Ela é aceita só quando de
    fato repete o título (igual, ou o título é prefixo dela — o caso do corte em
    `TETO_TITULO`); qualquer outro texto é conteúdo alheio e fica intocado, mesmo
    que isso deixe o conserto pela metade em algumas notas. Reescrever a linha
    errada é pior do que não reescrever.
    """
    for i in range(idx_titulo + 1, len(linhas)):
        s = linhas[i].strip()
        if not s:
            continue
        if s.startswith(("![[", "**Malha", "#")):
            return -1               # chegou na parte estrutural: não há corpo
        return i if s == titulo or s.startswith(titulo) else -1
    return -1


def _radical(palavra: str) -> str:
    """Tira só o "s" do plural (palavra > 3 letras). Não é stemmer, e não deve
    virar um: aqui basta empatar "testes"/"teste" dos dois lados da comparação."""
    return palavra[:-1] if len(palavra) > 3 and palavra.endswith("s") else palavra


def combina_com_legenda(trecho: str, chaves: Set[str], minimo: int = 2) -> bool:
    """A frase que acabou de ser dita fala DESTA figura?

    É o que permite pôr a imagem no meio da resposta em vez de empilhar tudo no
    fim: a cada frase emitida, pergunta-se de qual figura ela está falando.

    `minimo` é 2 de propósito. Uma palavra só casa por acaso — quase toda frase
    de uma resposta sobre cultivo contém "solo" ou "planta", e a legenda também,
    então o teto de 1 grudaria a primeira figura na primeira frase. Duas palavras
    distintas da legenda já exigem que a frase fale do MESMO assunto. É a mesma
    ideia do aterramento léxico do gate, uma escala abaixo.
    """
    if not trecho or not chaves or minimo <= 0:
        return False
    # LIMIAR PROPORCIONAL À LEGENDA, os dois lados medidos em turno real
    # (2026-07-29). Legenda de rótulo tem 2 palavras ("Testes de Solo") e NUNCA
    # alcança 2 casamentos, então com o teto fixo ela ia sempre para o fim; com
    # teto 1 ela grudou na frase certa. Legenda descritiva tem 8+ palavras e aí 2
    # é o que separa "fala disto" de "usou a mesma palavra". A régua: metade da
    # legenda, no máximo `minimo`. Quanto MENOS a legenda diz, menos evidência dá
    # para exigir — e rótulo de seção erra menos, porque ele É o tema da página.
    minimo = max(1, min(minimo, (len(chaves) + 1) // 2))
    # Plural: a legenda diz "Testes de Solo" e a frase diz "teste de solo" — sem
    # isto, o casamento cai de 2 para 1 e a figura vai para o fim. Medido em turno
    # real. O corte do "s" fica AQUI, e não no `textutils.tokens`, que é o mesmo
    # dos gates de recuperação: mexer lá mudaria aterramento e dedup de uma vez.
    alvo = {_radical(t) for t in textutils.tokens(trecho)}
    return len(alvo & {_radical(c) for c in chaves}) >= minimo


def legenda_da_nota(nota: str) -> str:
    """A legenda INTEIRA de uma nota de figura — o corpo quando ele existe.

    O título nasce cortado em `TETO_TITULO` e o corpo guarda a frase completa;
    julgar a nota pelo título é julgar por menos evidência. Custou uma passada:
    a régua de idioma rodando só no título deixou passar 14 legendas inteiramente
    em inglês, porque o pedaço cortado é que carregava as palavras que denunciam.
    """
    linhas = (nota or "").split("\n")
    i = next((k for k, ln in enumerate(linhas) if ln.startswith("## ")), -1)
    if i < 0:
        return ""
    titulo = linhas[i][3:].strip()
    j = _indice_do_corpo(linhas, i, titulo)
    return linhas[j].strip() if j >= 0 else titulo


def trocar_legenda(nota: str, nova: str) -> Optional[str]:
    """Troca a legenda de uma nota de figura, guardando a original no frontmatter.

    `None` quando não há o que fazer — nota já tocada (`legenda_original:`, o que
    torna a passada idempotente), sem título `##`, sem frontmatter, ou a nova
    legenda é igual à atual. Devolver None em vez do texto intacto deixa o
    chamador CONTAR o que pulou, que é o número que diz se a passada fez sentido.

    A legenda aparece DUAS vezes — título `##` (cortado em `TETO_TITULO`) e corpo
    (inteiro) — e as duas trocam, cada uma no seu formato: o título é metade do
    que a busca enxerga, e o corpo é o que entra no embedding. O que fica intocado
    é tudo o mais, em especial o embed da imagem, a linha de proveniência e a
    **Malha Neural**: renomear um [[conceito]] reescreveria arestas do grafo sem
    como reapontá-las (mesma regra da passada de tradução de títulos).
    """
    if not nota or "legenda_original:" in nota:
        return None

    linhas = nota.split("\n")
    idx_titulo = next((i for i, ln in enumerate(linhas) if ln.startswith("## ")), -1)
    if idx_titulo < 0:
        return None

    titulo = linhas[idx_titulo][3:].strip()
    nova = (nova or "").strip()
    if not titulo or not nova:
        return None

    idx_corpo = _indice_do_corpo(linhas, idx_titulo, titulo)
    # A original a guardar é a do CORPO quando ela existe: é a legenda inteira, e
    # é ela que serve de PROVA para `idioma.aplicar_correcoes` depois.
    original = linhas[idx_corpo].strip() if idx_corpo >= 0 else titulo
    if original == nova:
        return None

    linhas[idx_titulo] = f"## {nova[:TETO_TITULO]}"
    if idx_corpo >= 0:
        linhas[idx_corpo] = nova
    novo = "\n".join(linhas)

    m = _FM_RE.match(novo)
    if not m:                       # sem frontmatter não há onde guardar o original
        return None
    guardada = original.replace("\n", " ").replace('"', "'")
    return (m.group(1) + m.group(2) + f"\nlegenda_original: {guardada}"
            + m.group(3) + novo[m.end():])


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
