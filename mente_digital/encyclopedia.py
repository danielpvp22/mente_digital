"""Livro publicado na WEB — extração PURA (2026-07-27).

A Cannabis Encyclopedia (Jorge Cervantes) está publicada de graça, capítulo a
capítulo, em páginas WordPress/Elementor. Diferente do PDF, aqui a legenda vem
ESTRUTURADA: cada `<figure class="wp-block-image">` é seguida do parágrafo em
itálico que a descreve. Isso importa muito para este projeto — medido em
2026-07-27, 57% das figuras que o RAG entrega estão fora do tema, e a causa é a
legenda fraca; aqui ela vem certa da fonte, e a figura nasce COLADA no trecho de
texto que ela ilustra (co-locação exata, não proximidade estimada).

Módulo PURO e testável: recebe HTML como string, devolve dados. Nada de rede,
disco ou conversão de imagem — isso é do `scripts/importar_encyclopedia.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# O corpo do post no tema Elementor. `.entry-content` é o fallback WordPress
# clássico. NÃO usar `article`: a página repete 37 <article> nos cards de
# "posts relacionados" do rodapé, e o primeiro tem 28 chars (medido).
SELETORES_CORPO = (
    ".elementor-widget-theme-post-content",
    ".entry-content",
    "main",
)

# Blocos de navegação/rodapé que não são o livro.
_RUIDO = re.compile(
    r"^\s*(compartilh|share this|related posts?|posts? relacionad|"
    r"leia mais|read more|previous chapter|next chapter|"
    r"©|copyright|all rights reserved)", re.I)

# Uma legenda de figura é curta e descreve a imagem; um parágrafo de corpo em
# itálico inteiro é raro. Teto generoso — legendas do livro chegam a ~300 chars.
MAX_CHARS_LEGENDA = 400


# DEGRAUS DA ESCADA DE LEGENDA, do mais forte ao mais fraco. O degrau fica
# GRAVADO na figura (`legenda_origem`) porque a decisão de manter ou cortar cada
# um é EMPÍRICA: medido em 2026-07-27, 57% das figuras que o RAG entrega estão
# fora do tema, e sem saber de que degrau veio a legenda não dá para cortar só o
# que polui. Registrar agora é o que torna a calibração possível depois.
DEGRAU_FIGCAPTION = "figcaption"     # o site marcou explicitamente
DEGRAU_ITALICO = "italico"           # o parágrafo em itálico abaixo (o normal aqui)
DEGRAU_SECAO = "secao"               # título da seção em que a figura está
DEGRAU_AUSENTE = "ausente"           # nada → attach-only

# Só estes degraus valem como texto BUSCÁVEL. O resto entra attach-only.
DEGRAUS_BUSCAVEIS = (DEGRAU_FIGCAPTION, DEGRAU_ITALICO)


@dataclass
class FiguraWeb:
    url: str
    legenda: str = ""
    pagina: int = 0            # página SINTÉTICA, atribuída no fatiamento
    titulo_secao: str = ""     # cabeçalho vigente onde a figura apareceu
    legenda_origem: str = DEGRAU_AUSENTE
    conceitos: List[str] = field(default_factory=list)

    @property
    def tem_legenda(self) -> bool:
        return bool(self.legenda.strip())

    @property
    def attach_only(self) -> bool:
        """A figura NÃO entra no espaço de busca: ela só é anexada quando o átomo
        da página dela é usado na resposta.

        É o achado central de 2026-07-27: figura sem legenda de verdade não faz
        mal — ela é INVISÍVEL (17,4% do acervo, 3,3% das entregas, 1,9% dos
        erros). Tentar torná-la encontrável foi o que criou as notas genéricas.
        Então não se tenta: ela vira acompanhante do texto, nunca concorrente.
        Assim é impossível repetir o caso 'tricomas' — ela não disputa vaga."""
        return self.legenda_origem not in DEGRAUS_BUSCAVEIS


@dataclass
class BlocoWeb:
    """Um pedaço do capítulo que cabe numa passada de atomização."""
    texto: str
    figuras: List[FiguraWeb] = field(default_factory=list)
    pagina: int = 0


def conceitos_da_figura(fig: "FiguraWeb", titulo_capitulo: str) -> List[str]:
    """A MALHA da figura — os wikilinks que a ligam ao texto do vault.

    Sem isto a figura é uma ilha: ela só seria alcançada por semelhança de
    legenda, que é exatamente o mecanismo que medi falhar (57% fora do tema).
    Com conceito COMPARTILHADO, a expansão da malha (`MalhaIndex.vizinhos`)
    alcança a figura a partir do átomo de texto que já é relevante — que é o
    caminho certo: a figura acompanha uma resposta que existe.

    Determinístico de propósito: seção + capítulo + o assunto do livro, sem
    LLM. Assim uma pergunta genérica ('me mostra um exemplo de planta de
    cannabis') encontra o átomo de texto pelo assunto do livro e a figura vem
    junto pela malha, mesmo que a legenda dela não diga 'cannabis'.

    Puro/testável."""
    vistos, saida = set(), []
    for bruto in (fig.titulo_secao, titulo_capitulo, ASSUNTO_LIVRO):
        c = _rotulo(bruto)
        if c and c.casefold() not in vistos:
            vistos.add(c.casefold())
            saida.append(c)
    return saida


# O livro inteiro é sobre isto. Entra como conceito de TODA figura para que a
# pergunta genérica ("um exemplo de planta") tenha por onde chegar.
ASSUNTO_LIVRO = "Cannabis"

_SUFIXO_CAP = re.compile(r"\s*[-–—]\s*chapter\s*\d+\s*$", re.I)


def _rotulo(texto: str) -> str:
    """Normaliza um cabeçalho em rótulo de wikilink: sem numeração de capítulo,
    sem pontuação de sobra, comprimento sadio."""
    t = _SUFIXO_CAP.sub("", (texto or "").strip())
    t = re.sub(r"\s+", " ", t).strip(" .:–—-")
    if not t or len(t) < 3 or len(t) > 60:
        return ""
    if t.isdigit():
        return ""
    return t


def _limpar(soup) -> None:
    for tag in soup(["script", "style", "noscript", "iframe", "form", "svg"]):
        tag.decompose()


def _url_da_img(img) -> str:
    """O src real. Temas com lazy-load põem um placeholder no `src` e a imagem
    de verdade no `data-src`/`data-lazy-src` — pegar o `src` cru baixaria um GIF
    transparente de 1px em vez da figura."""
    for attr in ("data-lazy-src", "data-src", "data-original", "src"):
        v = (img.get(attr) or "").strip()
        if v and not v.startswith("data:"):
            return v
    return ""


def _inteiro(valor) -> int:
    """Atributo HTML numérico -> int. 0 quando ausente ou não numérico ('100%')."""
    try:
        return int(str(valor or "").strip())
    except ValueError:
        return 0


def _e_legenda(no) -> bool:
    """O parágrafo é a legenda da figura acima?

    A regra é 'o texto do <p> está TODO em itálico'. Não basta 'contém <em>':
    um parágrafo de corpo pode enfatizar uma palavra no meio e viraria legenda
    de uma figura com a qual não tem relação."""
    if getattr(no, "name", None) not in ("p", "div"):
        return False
    texto = no.get_text(" ", strip=True)
    if not texto or len(texto) > MAX_CHARS_LEGENDA:
        return False
    italico = " ".join(
        e.get_text(" ", strip=True) for e in no.find_all(["em", "i"])
    ).strip()
    if not italico:
        return False
    # tolera pontuação/espaço sobrando fora do <em>
    return len(italico) >= 0.9 * len(texto)


# LAYOUT, não conteúdo. Medido no cap. 18: `JorgeCervantes_Banner-1024x280`
# entrou como figura do livro. O nome do arquivo é o sinal mais confiável (o
# tema não marca classe em tudo), e a proporção pega a faixa decorativa.
_LAYOUT_NOME = re.compile(
    r"(banner|logo|header|footer|sidebar|avatar|icon|badge|sprite|"
    r"placeholder|spacer|divider|ads?[-_]|advert)", re.I)
MAX_PROPORCAO_FIGURA = 4.0


def e_conteudo(url: str, largura: int = 0, altura: int = 0) -> bool:
    """A imagem é FIGURA DO LIVRO, e não moldura do site? Puro/testável.

    O extrator não distinguia as duas coisas e o banner do autor virava uma nota
    de figura com legenda de seção — ou seja, ruído permanente no vault, do tipo
    que só se descobre respondendo errado meses depois.

    As dimensões vêm dos atributos do `<img>` quando o tema as declara; 0 = não
    declarou, e aí só o nome decide (nunca reprovar por falta de informação —
    perder figura de verdade é o erro caro)."""
    nome = (url or "").rsplit("/", 1)[-1]
    if not nome:
        return False
    if _LAYOUT_NOME.search(nome):
        return False
    if largura > 0 and altura > 0:
        menor, maior = min(largura, altura), max(largura, altura)
        if menor > 0 and maior > MAX_PROPORCAO_FIGURA * menor:
            return False        # faixa/tarja: proporção de banner, não de figura
    return True


def corpo_nota_figura(fig: "FiguraWeb", arquivo_rel: str, subpasta: str,
                      titulo_livro: str, titulo_capitulo: str,
                      tag_anexo: str = "") -> str:
    """O corpo da nota de UMA figura da web — o que a torna achável E anexável.

    Sem uma nota própria a figura não existe para o sistema: a busca de figura
    roda sobre NOTAS (`where={"tipo":"figura"}`) e o anexo à resposta é montado a
    partir dos caminhos de nota que a busca selecionou. Um `.webp` no vault sem o
    `.md` ao lado é um arquivo que ninguém nunca vai ver.

    Mesma FORMA da nota do livro escaneado (`figuras_recorte.corpo_da_nota`):
    título, legenda, embed, uma linha de proveniência e a Malha Neural. O que
    muda é a procedência dos dados — aqui a legenda vem estruturada da fonte e os
    conceitos são determinísticos (seção + capítulo + assunto), sem LLM.

    `tag_anexo` (quando a figura é attach-only) entra no corpo e é o que
    `rag.metadados_da_nota` lê para tirá-la do espaço de busca. Sai cru: quem
    chama passa por `atomos.normalizar_atomo`."""
    legenda = (fig.legenda or "").strip()
    titulo = legenda or fig.titulo_secao.strip() or f"Figura da página {fig.pagina}"
    corpo = [f"## {titulo[:120]}"]
    if legenda:
        corpo.append(legenda)
    corpo.append(f"![[{subpasta}/{arquivo_rel}]]")
    onde = f"Figura da página {fig.pagina}"
    if fig.titulo_secao.strip():
        onde += f", da seção '{fig.titulo_secao.strip()}'"
    if titulo_capitulo:
        onde += f", do capítulo '{titulo_capitulo}'"
    if titulo_livro:
        onde += f", em {titulo_livro}"
    corpo.append(onde + ".")
    conceitos = fig.conceitos or conceitos_da_figura(fig, titulo_capitulo)
    if conceitos:
        corpo.append("**Malha Neural:** " + " ".join(f"[[{c}]]" for c in conceitos))
    if tag_anexo:
        corpo.append(tag_anexo)
    return "\n".join(corpo)


def extrair_capitulo(html: str, titulo_fallback: str = "") -> tuple:
    """`(titulo, [BlocoWeb])` — sem fatiar ainda: um bloco por sequência
    texto→figura na ORDEM do documento.

    Devolve `("", [])` se a página não tem corpo reconhecível (fail-soft: o
    chamador pula o capítulo e loga, em vez de gravar job vazio)."""
    from bs4 import BeautifulSoup  # import tardio: só o extrator precisa

    soup = BeautifulSoup(html or "", "html.parser")
    _limpar(soup)

    titulo = ""
    h1 = soup.find("h1")
    if h1:
        titulo = h1.get_text(" ", strip=True)
    titulo = titulo or titulo_fallback

    corpo = None
    for sel in SELETORES_CORPO:
        corpo = soup.select_one(sel)
        if corpo is not None and len(corpo.get_text(" ", strip=True)) > 500:
            break
        corpo = None
    if corpo is None:
        return ("", [])

    partes: List[str] = []
    figuras: List[FiguraWeb] = []
    pendente: Optional[FiguraWeb] = None      # figura esperando a legenda abaixo
    secao = ""                                # cabeçalho vigente

    def encerrar(fig: Optional[FiguraWeb]) -> None:
        """Fecha a figura aplicando a ESCADA e a malha. Chamado sempre que a
        janela de legenda se fecha (outra figura, parágrafo comum ou fim)."""
        if fig is None:
            return
        if not fig.legenda.strip():
            if fig.titulo_secao.strip():
                fig.legenda = fig.titulo_secao.strip()
                fig.legenda_origem = DEGRAU_SECAO
            else:
                fig.legenda_origem = DEGRAU_AUSENTE
        fig.conceitos = conceitos_da_figura(fig, titulo)
        figuras.append(fig)

    for no in corpo.find_all(["p", "h2", "h3", "h4", "li", "figure", "img"],
                             recursive=True):
        if no.name in ("figure", "img"):
            img = no if no.name == "img" else no.find("img")
            if img is None:
                continue
            if img.find_parent("figure") is not None and no.name == "img":
                continue                      # já tratado pelo <figure> pai
            url = _url_da_img(img)
            if not url or not e_conteudo(url, _inteiro(img.get("width")),
                                         _inteiro(img.get("height"))):
                continue
            # figura nova encerra a espera da anterior: duas <figure> seguidas
            # significam que a primeira não tem legenda (medido no cap. 18).
            encerrar(pendente)
            pendente = None
            legenda = ""
            cap = no.find("figcaption") if no.name == "figure" else None
            if cap is not None:
                legenda = cap.get_text(" ", strip=True)
            pendente = FiguraWeb(url=url, legenda=legenda, titulo_secao=secao,
                                 legenda_origem=DEGRAU_FIGCAPTION if legenda
                                 else DEGRAU_AUSENTE)
            if legenda:                        # figcaption já resolveu
                encerrar(pendente)
                pendente = None
            continue

        texto = no.get_text(" ", strip=True)
        if not texto or _RUIDO.match(texto):
            continue
        if pendente is not None and _e_legenda(no):
            pendente.legenda = texto
            pendente.legenda_origem = DEGRAU_ITALICO
            encerrar(pendente)
            pendente = None
            continue                           # legenda NÃO entra no corpo
        encerrar(pendente)                     # parágrafo comum fecha a janela
        pendente = None
        if no.name in ("h2", "h3", "h4"):
            secao = texto
            partes.append(f"\n## {texto}\n")
        elif no.name == "li":
            partes.append(f"- {texto}")
        else:
            partes.append(texto)

    encerrar(pendente)

    corpo_txt = "\n".join(p for p in partes if p.strip()).strip()
    if not corpo_txt and not figuras:
        return (titulo, [])
    return (titulo, [BlocoWeb(texto=corpo_txt, figuras=figuras)])


def fatiar(blocos: List[BlocoWeb], max_chars: int,
           pagina_inicial: int = 1) -> List[BlocoWeb]:
    """Fatia em pedaços que cabem numa passada de atomização, numerando PÁGINAS
    sintéticas sequenciais.

    A página sintética não é enfeite: a proveniência do vault e a co-locação
    figura↔átomo são POR PÁGINA (`_p0288_f1` no nome do arquivo, `(p. 3-4)` na
    origem). Numerar aqui faz a figura cair na mesma 'página' do texto que ela
    ilustra — por construção, não por estimativa.

    As figuras são distribuídas proporcionalmente à posição em que apareceram:
    sem coordenada exata na web, a ordem do documento é a melhor âncora."""
    saida: List[BlocoWeb] = []
    pagina = pagina_inicial
    for bloco in blocos:
        pedacos = _quebrar(bloco.texto, max_chars) or [""]
        n = len(pedacos)
        figs = bloco.figuras
        for i, pedaco in enumerate(pedacos):
            ini = (i * len(figs)) // n
            fim = ((i + 1) * len(figs)) // n
            fatia = figs[ini:fim]
            for f in fatia:
                f.pagina = pagina
            saida.append(BlocoWeb(texto=pedaco, figuras=list(fatia), pagina=pagina))
            pagina += 1
    return saida


def _quebrar(texto: str, max_chars: int) -> List[str]:
    """Quebra em fronteira de PARÁGRAFO — cortar no meio de uma frase produz
    átomo truncado, que é justamente o defeito que a triagem editorial barra."""
    texto = (texto or "").strip()
    if not texto:
        return []
    if max_chars <= 0 or len(texto) <= max_chars:
        return [texto]
    saida, atual = [], ""
    for linha in texto.split("\n"):
        if atual and len(atual) + len(linha) + 1 > max_chars:
            saida.append(atual.strip())
            atual = linha
        else:
            atual = f"{atual}\n{linha}" if atual else linha
    if atual.strip():
        saida.append(atual.strip())
    return saida


def estatisticas(blocos: List[BlocoWeb]) -> str:
    """A linha que o dono lê na passada a seco.

    Reporta o DEGRAU de cada legenda e quantas ficaram attach-only: é a única
    coisa que diz se o capítulo vale a pena antes de gravar. Uma queda de
    `italico` para `secao` num capítulo significa que o tema mudou o HTML, e
    isso precisa aparecer no ato, não meses depois numa resposta errada."""
    figs = [f for b in blocos for f in b.figuras]
    degraus: dict = {}
    for f in figs:
        degraus[f.legenda_origem] = degraus.get(f.legenda_origem, 0) + 1
    anexo = sum(1 for f in figs if f.attach_only)
    resumo = " ".join(f"{k}:{v}" for k, v in sorted(degraus.items()))
    return (f"{len(blocos)} bloco(s) | {sum(len(b.texto) for b in blocos)} chars | "
            f"{len(figs)} figura(s) [{resumo}] | {anexo} attach-only")
