"""Figuras de livro ESCANEADO — detecção por `<|grounding|>` (2026-07-25, 2ª versão).

O buraco que este módulo tapa: `figuras.py` extrai a imagem EMBUTIDA do PDF, o
que só funciona em livro digital. Nos dois livros escaneados da base a "imagem
embutida" é a PÁGINA FOTOGRAFADA inteira (Amabis 1 imagem/página de 1011x1546,
Cervantes 1/página de 783x1236), e a guarda do `extrair_de_pdf` apaga tudo — com
razão: retrato de página não ilustra nada. Aqui a figura é ACHADA dentro da
página e RECORTADA.

POR QUE O GROUNDING, e não heurística de pixel (a lápide da 1ª versão): a versão
anterior reduzia a página a uma grade de tinta/cor/textura e caçava componentes
conexos. Três calibrações a olho depois, ela continuava errando nos DOIS
sentidos — perdia as duas figuras anatômicas da p550 do Amabis (traço fino sobre
papel claro: pouca "cor", pouca "textura") e o gráfico de pH da p250 do Cervantes
(cor chapada: reprovado pelo filtro de textura), enquanto promovia tarja de
design a figura. É troca precisão×recall inerente: sem entender a PÁGINA, não há
limiar que separe "diagrama de traço preto" de "coluna de texto".

O DeepSeek-OCR que a Fase 3 já usa resolve isso de graça. Com o token
`<|grounding|>` ele devolve o LAYOUT rotulado da página:

    image[[166, 99, 831, 370]]
    image_caption[[508, 858, 822, 881]]
    <center>▲ Figura 19.2 • Ação dos músculos bíceps e tríceps…</center>
    sub_title[[109, 394, 319, 407]]
    text[[108, 416, 481, 659]]

Coordenadas NORMALIZADAS em 0–999 nos dois eixos (não pixels — `Caixa.para_pixels`
faz a conversão, conferida recortando de verdade). E a legenda vem PAREADA com a
figura pelo próprio modelo, o que dispensa a busca geométrica por "texto abaixo".

DUAS PASSADAS, de propósito (o requisito é "não deixar uma única imagem passar"):
o prompt de markdown gasta tokens transcrevendo o corpo do texto e, numa página
densa, pode ser CORTADO pelo limite de tokens antes de chegar às figuras do fim
da página. O prompt de localização (`Locate <|ref|>figure<|/ref|>…`) devolve só
caixas — 300 chars contra 6.000 — e portanto nunca trunca. Eles também discordam:
medido na p157 do Cervantes, um achou uma caixa que o outro não. A UNIÃO das duas
(`unir_figuras`, por IoU) é o que fecha o recall; o custo é ~0,6s/página, contra
~2,5s do markdown.

Puro e sem numpy de propósito: a detecção agora é parsing de texto, e a suíte roda
no CI leve. Todo o IO (fitz/Pillow/HTTP) fica no script.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

# Escala das coordenadas do grounding do DeepSeek-OCR: 0–999 em CADA eixo,
# independentemente da proporção da página (conferido recortando).
NORM = 999

# Rótulos que valem RECORTE. `image` é o que o modelo emite em ~todos os casos;
# `figure` apareceu no prompt de localização (p60 do Cervantes) e os demais são
# rede de segurança — o vocabulário do modelo não é contratual, e um rótulo novo
# custaria uma figura perdida.
TIPOS_FIGURA = ("image", "figure", "picture", "photo", "figura", "imagem",
                "chart", "diagram", "illustration")

# Rótulos de legenda de figura. `table_caption` fica DE FORA: a tabela é
# transcrita como texto pelo pipeline de OCR, não recortada como imagem.
TIPOS_LEGENDA = ("image_caption", "figure_caption", "caption")

# Rótulos que são TEXTO da página. Servem de contraprova: uma caixa `image` que
# está por baixo de parágrafos é engano do modelo, não figura (medido na auditoria
# de 2026-07-26: p294 do Cervantes e p249 do Amabis, ambas 100% texto, saíram com
# uma "figura" que era um bloco de prosa).
# `image_caption` fica DE FORA de propósito: a legenda encosta e às vezes entra na
# caixa da figura, e contá-la como cobertura reprovaria a figura de verdade.
TIPOS_TEXTO = ("text", "title", "sub_title", "header", "footer", "page_number",
               "list", "table", "table_caption", "formula", "paragraph")

# Uma detecção do grounding: `rotulo[[x0, y0, x1, y1]]` no INÍCIO da linha. A
# âncora de início de linha não é cosmética — no modo "OCR this image" o modelo
# emite `<|ref|>Fêmur<|/ref|><|det|>[[169,103,188,116]]`, e sem a âncora o
# `[a-z_]+` casaria o final da palavra ("emur") como se fosse um rótulo.
_TAG_RE = re.compile(
    r"^[ \t]*([a-z][a-z_]*)\[\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\]",
    re.MULTILINE)

# Início de legenda: "Figura 4.1", "Fig. 12", "Quadro 3", "Tabela 2.1". Só o
# COMEÇO da linha — é onde o livro põe o rótulo, e casar no meio pegaria qualquer
# menção corrida ("...como mostra a figura 4.1"), que é referência, não legenda.
_LEGENDA_INICIO_RE = re.compile(
    r"^\s*(?:figuras?|fig\.?|quadros?|tabelas?|foto|gr[áa]fico)\s*[\dA-Z]",
    re.IGNORECASE)

# Nome de arquivo gerado por `figuras.nome_arquivo`: <slug>_p0039_f2.webp. O slug
# volta daqui, o que deixa o conserto de link independente de lista de livros.
_NOME_FIGURA_RE = re.compile(r"^(?P<slug>.+)_p(?P<pagina>\d{4})_f(?P<indice>\d+)\.webp$")


class Caixa(NamedTuple):
    """Retângulo em coordenadas NORMALIZADAS 0–999, fim inclusivo (como o modelo
    entrega). Só vira pixel em `para_pixels`."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def largura(self) -> int:
        return max(0, self.x1 - self.x0)

    @property
    def altura(self) -> int:
        return max(0, self.y1 - self.y0)

    @property
    def area(self) -> int:
        return self.largura * self.altura

    @property
    def centro(self) -> Tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    def intersecao(self, outra: "Caixa") -> int:
        dx = min(self.x1, outra.x1) - max(self.x0, outra.x0)
        dy = min(self.y1, outra.y1) - max(self.y0, outra.y0)
        return dx * dy if dx > 0 and dy > 0 else 0

    def iou(self, outra: "Caixa") -> float:
        """Interseção sobre união — o critério de "é a mesma figura" entre as duas
        passadas. As caixas de passadas diferentes nunca batem no pixel (medido:
        `[166,99,831,370]` contra `[166,98,831,370]`), então comparar por
        igualdade duplicaria tudo."""
        inter = self.intersecao(outra)
        uniao = self.area + outra.area - inter
        return inter / uniao if uniao > 0 else 0.0

    def contem(self, outra: "Caixa") -> bool:
        return (self.x0 <= outra.x0 and self.y0 <= outra.y0
                and self.x1 >= outra.x1 and self.y1 >= outra.y1)

    def unir(self, outra: "Caixa") -> "Caixa":
        return Caixa(min(self.x0, outra.x0), min(self.y0, outra.y0),
                     max(self.x1, outra.x1), max(self.y1, outra.y1))

    def vao(self, outra: "Caixa") -> int:
        """Distância entre as bordas (0 se as caixas se tocam ou se sobrepõem).
        É o que decide se uma legenda solta pertence a esta figura."""
        dx = max(self.x0 - outra.x1, outra.x0 - self.x1, 0)
        dy = max(self.y0 - outra.y1, outra.y0 - self.y1, 0)
        return max(dx, dy)

    def para_pixels(self, largura: int, altura: int,
                    margem_frac: float = 0.0) -> Tuple[int, int, int, int]:
        """0–999 → retângulo em pixels da página, preso aos limites da imagem.

        `margem_frac` devolve um respiro proporcional ao TAMANHO DA FIGURA (e não
        um número fixo de pixels): a caixa do modelo encosta no traço, e os dois
        livros são rasterizados em resoluções bem diferentes (1241 e 1773 px de
        largura), então margem fixa seria generosa num e rente no outro."""
        ex = int(self.largura * largura / NORM * margem_frac)
        ey = int(self.altura * altura / NORM * margem_frac)
        x0 = max(0, int(self.x0 * largura / NORM) - ex)
        y0 = max(0, int(self.y0 * altura / NORM) - ey)
        x1 = min(largura, int(self.x1 * largura / NORM) + ex)
        y1 = min(altura, int(self.y1 * altura / NORM) + ey)
        return x0, y0, x1, y1


class Deteccao(NamedTuple):
    """Uma linha `rotulo[[...]]` do grounding, com o texto que veio logo depois."""

    tipo: str
    caixa: Caixa
    conteudo: str


class Figura(NamedTuple):
    """Uma figura pronta para recorte: onde está e o que a descreve."""

    caixa: Caixa
    legenda: str


def parse_grounding(bruto: str) -> List[Deteccao]:
    """Saída crua do modo `<|grounding|>` → detecções, na ORDEM em que vieram.

    A ordem importa: o modelo emite `image_caption` logo APÓS o `image` a que ela
    pertence, e essa adjacência é um pareamento que o modelo já resolveu — bem
    mais confiável que adivinhar "o texto mais próximo abaixo" na geometria
    (medido na p346 do Amabis, onde a legenda fica AO LADO da figura, não abaixo).

    Coordenadas fora de 0–999 são grampeadas em vez de descartadas: o modelo
    ocasionalmente estoura por poucas unidades, e jogar a figura fora por causa
    disso é exatamente o erro que esta fase existe para não cometer."""
    achados: List[Tuple[str, Caixa, int, int]] = []
    for m in _TAG_RE.finditer(bruto or ""):
        vals = [max(0, min(NORM, int(v))) for v in m.group(2, 3, 4, 5)]
        x0, y0, x1, y1 = vals
        achados.append((m.group(1), Caixa(min(x0, x1), min(y0, y1),
                                          max(x0, x1), max(y0, y1)),
                        m.start(), m.end()))
    saida = []
    for i, (tipo, caixa, _ini, fim) in enumerate(achados):
        prox = achados[i + 1][2] if i + 1 < len(achados) else len(bruto or "")
        saida.append(Deteccao(tipo, caixa, (bruto or "")[fim:prox].strip()))
    return saida


def limpar_legenda(texto: str, max_chars: int = 300) -> str:
    """Achata o que veio junto da caixa numa linha só.

    O modelo embrulha a legenda em `<center>…</center>` e usa markdown; título,
    marcador e quebra viram ruído dentro de uma legenda de uma linha. O `▲`/`◀`
    é o triangulinho impresso no próprio livro apontando para a figura."""
    plano = re.sub(r"</?[a-z][a-z0-9]*[^>]*>", " ", texto or "", flags=re.IGNORECASE)
    plano = plano.replace("#", " ").replace("*", " ").replace("`", " ")
    plano = plano.strip(" \t\n▲▼◀▶►◄•·-—–")
    return " ".join(plano.split())[:max_chars].strip()


def parece_legenda(texto: str) -> bool:
    """True quando a linha COMEÇA como rótulo de figura ('Figura 4.1 —')."""
    return bool(_LEGENDA_INICIO_RE.match(texto or ""))


def figuras_da_passada(dets: Sequence[Deteccao],
                       tipos_figura: Sequence[str] = TIPOS_FIGURA,
                       tipos_legenda: Sequence[str] = TIPOS_LEGENDA) -> List[Figura]:
    """Figuras de UMA passada, já com a legenda que o modelo pareou por adjacência.

    Regra: a legenda de uma figura é a detecção IMEDIATAMENTE seguinte, se for de
    um tipo de legenda. Nada de "a mais próxima abaixo" — a p550 do Amabis tem uma
    figura no topo SEM legenda e outra no rodapé COM, e a busca geométrica daria a
    legenda da segunda para a primeira."""
    figuras: List[Figura] = []
    for i, d in enumerate(dets):
        if d.tipo not in tipos_figura:
            continue
        legenda = ""
        if i + 1 < len(dets) and dets[i + 1].tipo in tipos_legenda:
            legenda = limpar_legenda(dets[i + 1].conteudo)
        figuras.append(Figura(d.caixa, legenda))
    return figuras


def legendas_da_passada(dets: Sequence[Deteccao],
                        tipos_legenda: Sequence[str] = TIPOS_LEGENDA
                        ) -> List[Figura]:
    """Todas as legendas soltas da página (caixa + texto), para o preenchimento
    geométrico de quem ficou sem par."""
    return [Figura(d.caixa, limpar_legenda(d.conteudo)) for d in dets
            if d.tipo in tipos_legenda]


def descartar_borrao(figuras: Sequence[Figura], textos: Sequence[Caixa],
                     max_fracao_pagina: float = 0.95) -> List[Figura]:
    """Tira a caixa do tamanho da folha ANTES da união. Puro.

    O defeito mais caro que a auditoria de 2026-07-26 desenterrou, e ele era
    invisível na saída de cada passada isolada: na p510 do Amabis a passada de
    markdown ACHA a Figura 17.4 (`image[[502,325,880,803]]`), mas a passada de
    localização devolve `image[[0,0,999,999]]` — e a regra de contenção do
    `mesma_figura` então funde a caixa boa dentro do borrão, que em seguida morre
    no filtro de página inteira levando a figura junto. Mesma história com as três
    figuras da p325 do Cervantes. Somar duas passadas só ajuda se o lixo de uma
    não puder apagar o acerto da outra.

    A prova de que é lixo vem do TEXTO: se qualquer passada viu parágrafos na
    página, então a página não é uma foto — a caixa gigante é desistência do
    modelo. Sem texto nenhum, a folha PODE ser a fotografia (p97 do Cervantes) e
    a caixa fica."""
    if not textos:
        return list(figuras)
    limite = max_fracao_pagina * NORM * NORM
    return [f for f in figuras if f.caixa.area < limite]


def mesma_figura(a: Caixa, b: Caixa, iou_min: float = 0.35,
                 contido_min: float = 0.8) -> bool:
    """As duas caixas são o MESMO objeto visto por passadas diferentes?

    IoU sozinho não serve, e o caso que provou isso é o galão da p385 do
    Cervantes: uma passada enquadrou a foto inteira, a outra só o rótulo (45% da
    altura de fora). O IoU disso é 0,35 — bem no limiar, ou seja, decidido no
    acaso. A segunda condição resolve: quando uma caixa está quase toda DENTRO da
    outra, é a mesma figura recortada com menos generosidade, não outra figura."""
    inter = a.intersecao(b)
    if inter <= 0:
        return False
    menor = min(a.area, b.area)
    return a.iou(b) >= iou_min or (menor > 0 and inter / menor >= contido_min)


def unir_figuras(principal: Sequence[Figura], extra: Sequence[Figura],
                 iou_min: float = 0.35) -> List[Figura]:
    """União das duas passadas: tudo de `principal` + o que `extra` achou a mais.

    É o coração do "não deixar nada passar". As duas passadas quase sempre
    concordam, mas quando discordam a discordância é assimétrica e imprevisível
    (medido: a passada de markdown perdeu uma caixa na p157 do Cervantes; a de
    localização devolveu ZERO na p300 do Amabis). Somar é sempre mais seguro que
    escolher, porque uma caixa a mais é auditável e uma a menos é invisível.

    `iou_min` baixo de propósito: o mesmo objeto sai com enquadramento diferente
    em cada passada, e o risco de duplicar a figura é muito menor que o de
    perdê-la. A contenção de duplicata é o `absorver_aninhadas` logo abaixo."""
    saida = list(principal)
    for cand in extra:
        for i, ja in enumerate(saida):
            if mesma_figura(cand.caixa, ja.caixa, iou_min):
                # Mesma figura vista duas vezes: fica a UNIÃO das duas caixas.
                # Não é detalhe — na auditoria de 2026-07-26 uma passada cortou
                # 45% de uma foto (o galão da p385 do Cervantes saiu só com o
                # rótulo) enquanto a outra a enquadrava inteira. Ficar com a
                # primeira é apostar na sorte; unir recupera o que faltou, e o
                # custo de sobrar é um pedaço de página no recorte.
                saida[i] = Figura(ja.caixa.unir(cand.caixa),
                                  ja.legenda or cand.legenda)
                break
        else:
            saida.append(cand)
    return saida


def cobertura_por_texto(caixa: Caixa, textos: Sequence[Caixa]) -> float:
    """Fração da caixa que está sob blocos de TEXTO da mesma página (0–1).

    A contraprova do falso positivo: o modelo às vezes rotula um parágrafo como
    `image`, e sem isto o recorte vira uma "figura" que é prosa. Uma figura de
    verdade não tem parágrafos por cima — os rótulos internos de um diagrama não
    são emitidos como blocos de texto (conferido na p550 do Amabis).

    Soma simples das interseções, com teto em 1.0: blocos de texto de layout não
    se sobrepõem entre si, então a contagem dupla não acontece na prática."""
    if caixa.area <= 0:
        return 0.0
    return min(1.0, sum(caixa.intersecao(t) for t in textos) / caixa.area)


def absorver_aninhadas(figuras: Sequence[Figura]) -> List[Figura]:
    """Descarta a caixa CONTIDA em outra (uma prancha de várias fotos às vezes sai
    inteira numa passada e em pedaços na outra). Fica a maior, com a legenda de
    quem tiver — recortar o todo e as partes encheria o vault de repetição."""
    ordenadas = sorted(figuras, key=lambda f: f.caixa.area, reverse=True)
    saida: List[Figura] = []
    for f in ordenadas:
        for i, maior in enumerate(saida):
            if maior.caixa.contem(f.caixa):
                if not maior.legenda and f.legenda:
                    saida[i] = Figura(maior.caixa, f.legenda)
                break
        else:
            saida.append(f)
    return saida


def completar_legendas(figuras: Sequence[Figura], legendas: Sequence[Figura],
                       vao_max: int = 60) -> List[Figura]:
    """Dá uma legenda, POR GEOMETRIA, a quem a adjacência não pareou.

    Existe por um caso real (p220 do Amabis): duas figuras lado a lado e UMA
    legenda que descreve as duas ("A. Ápice caulinar… B. Representação…"). A
    adjacência dá a legenda só para a primeira; a segunda encosta na mesma caixa
    de legenda e merece a mesma descrição.

    `vao_max` (em unidades de 0–999, ~6% da página) é apertado de propósito: a
    legenda da figura de baixo NÃO pode vazar para a figura do topo — medido na
    p550 do Amabis, onde o vão entre elas é 488."""
    saida = []
    for f in figuras:
        if f.legenda or not legendas:
            saida.append(f)
            continue
        perto = [(f.caixa.vao(le.caixa), le.legenda) for le in legendas
                 if le.legenda and f.caixa.vao(le.caixa) <= vao_max]
        saida.append(Figura(f.caixa, min(perto)[1]) if perto else f)
    return saida


def filtrar_figuras(figuras: Sequence[Figura], textos: Sequence[Caixa] = (), *,
                    min_lado: int = 35, min_area: int = 5000,
                    max_proporcao: float = 8.0, max_fracao_pagina: float = 0.95,
                    max_cobertura_texto: float = 0.75
                    ) -> Tuple[List[Figura], List[Tuple[Figura, str]]]:
    """Separa figura de não-figura. Devolve (aceitas, [(rejeitada, motivo)]).

    O motivo VOLTA junto, e não é luxo: o requisito desta fase é não perder
    nenhuma imagem, então toda caixa descartada precisa ser auditável — é o
    relatório de rejeição que prova que o filtro não comeu figura de verdade.

    Os motivos, todos medidos:
    * `pequena` — ícone e filete de design;
    * `tira` — faixa de proporção extrema: a passada de localização devolveu
      `image[[0, 0, 999, 46]]` na p157 do Cervantes, que é o cabeçalho corrido;
    * `texto` — a caixa está por baixo dos parágrafos da página (ver
      `cobertura_por_texto`). O limiar é FROUXO (0,75) porque a auditoria de
      2026-07-26 pegou ele comendo figura: na p288 do Cervantes a caixa do modelo
      grudou a folha de maconha ao parágrafo vizinho (~40% figura) e a série de
      três estágios foi para o vault com dois; na p315 a caixa cobria EXATAMENTE
      a ilustração e mesmo assim caiu. Nesta fase o custo dos dois erros é
      assimétrico — nota inútil é auditável, imagem perdida é invisível;
    * `pagina_inteira` — a caixa do tamanho da folha SOBRE uma página que tem
      texto, ou seja, uma detecção que degenerou;
    * `degenerada` — caixa sem área.

    A PÁGINA INTEIRA NÃO É MAIS DESCARTE AUTOMÁTICO. Era, e a auditoria de
    2026-07-26 mostrou o preço: a p97 do Cervantes é uma fotografia sangrada de
    página inteira, sem uma linha de texto, e o filtro a jogou fora — falso
    negativo do pior tipo. Agora a decisão usa a contraprova do texto: caixa
    gigante numa página SEM texto é uma foto de página inteira legítima; numa
    página COM texto é engano, e o chamador ainda pode pedir resgate."""
    aceitas: List[Figura] = []
    rejeitadas: List[Tuple[Figura, str]] = []
    total = float(NORM * NORM)
    for f in figuras:
        c = f.caixa
        menor, maior = min(c.largura, c.altura), max(c.largura, c.altura)
        cobertura = cobertura_por_texto(c, textos)
        if c.area <= 0:
            rejeitadas.append((f, "degenerada"))
        elif menor < min_lado or c.area < min_area:
            rejeitadas.append((f, "pequena"))
        elif max_proporcao and maior > max_proporcao * max(1, menor):
            rejeitadas.append((f, "tira"))
        elif cobertura > max_cobertura_texto:
            rejeitadas.append((f, "texto"))
        elif c.area >= max_fracao_pagina * total and textos:
            rejeitadas.append((f, "pagina_inteira"))
        else:
            aceitas.append(f)
    return aceitas, rejeitadas


def precisa_resgate(aceitas: Sequence[Figura],
                    rejeitadas: Sequence[Tuple[Figura, str]] = (),
                    truncou: bool = False) -> bool:
    """True quando a página merece uma passada extra, coluna por coluna.

    O sintoma (auditoria de 2026-07-26): em vez de marcar as três figuras da
    coluna direita da p325 do Cervantes, o modelo devolveu UMA caixa do tamanho
    da folha; o mesmo na p510 do Amabis, que perdeu a Figura 17.4 inteira. A
    detecção não errou de pouco — ela DESISTIU de segmentar, e o filtro só viu o
    borrão. Uma página sobre a qual não se sabe nada vale reperguntar por partes,
    onde a coluna vira uma "página" de proporção normal.

    O GATILHO É PÁGINA SECA, e não "página com rejeição suspeita" como na 1ª
    versão. A auditoria mostrou por quê: a p165 do Amabis saiu com ZERO caixas e
    ZERO rejeições — o modelo simplesmente não emitiu nada — e a Figura 6.17, um
    composto de quatro painéis ocupando meia página, foi perdida sem deixar
    rastro. Exigir uma rejeição para resgatar é exigir que a falha se anuncie;
    a falha que importa é justamente a silenciosa.

    `truncou` também resgata mesmo com figuras aceitas: a saída foi cortada, então
    o que faltou está por definição no FIM da página, depois da última caixa
    emitida (custou uma tira de anúncio na p49 do Cervantes e o esquema de filos
    da p306 do Amabis)."""
    if truncou:
        return True
    return not aceitas


def remapear(caixa: Caixa, faixa: Caixa) -> Caixa:
    """Caixa detectada DENTRO de um recorte da página → coordenadas da página.

    As duas estão na escala 0–999; a faixa diz que pedaço da página foi enviado
    ao modelo, então basta reescalar para dentro dela."""
    fx, fy = faixa.largura / NORM, faixa.altura / NORM
    return Caixa(int(faixa.x0 + caixa.x0 * fx), int(faixa.y0 + caixa.y0 * fy),
                 int(faixa.x0 + caixa.x1 * fx), int(faixa.y0 + caixa.y1 * fy))


# Colunas com SOBREPOSIÇÃO, para o resgate: os dois livros são de duas colunas, e
# é sempre uma coluna que o modelo deixa de segmentar. A sobreposição de ~8%
# existe para a figura que sangra por cima da divisão não ser cortada ao meio.
FAIXAS_RESGATE = (Caixa(0, 0, 540, NORM), Caixa(460, 0, NORM, NORM))


def em_ordem_de_leitura(figuras: Sequence[Figura],
                        tolerancia: int = 40) -> List[Figura]:
    """Ordena para o índice do nome do arquivo ser estável entre reprocessamentos.

    Coluna antes de linha seria errado num livro de duas colunas; linha antes de
    coluna quebra quando duas figuras do mesmo "andar" saem com y0 diferindo por
    um par de unidades. Daí a tolerância: y0 é arredondado numa faixa antes de
    desempatar por x0."""
    return sorted(figuras, key=lambda f: (f.caixa.y0 // max(1, tolerancia),
                                          f.caixa.x0, f.caixa.y0))


def caixas_de_texto(dets: Sequence[Deteccao],
                    tipos: Sequence[str] = TIPOS_TEXTO) -> List[Caixa]:
    return [d.caixa for d in dets if d.tipo in tipos]


def consolidar(bruto_principal: str, bruto_extra: str = "",
               resgates: Sequence[Tuple[str, Caixa]] = (), *,
               iou_min: float = 0.35, vao_max: int = 60,
               **filtros) -> Tuple[List[Figura], List[Tuple[Figura, str]]]:
    """O pipeline inteiro de UMA página: as saídas cruas → figuras a recortar.

    parse → figuras por adjacência (cada passada) → união → absorve aninhadas →
    completa legenda por geometria → filtra contra o texto → ordem de leitura.

    `resgates` são pares (saída crua, faixa da página) da 2ª rodada, feita só nas
    páginas em que a 1ª degenerou; as caixas voltam remapeadas para a página.
    Devolve (aceitas, rejeitadas_com_motivo)."""
    dets_p = parse_grounding(bruto_principal)
    dets_e = parse_grounding(bruto_extra)
    legendas = legendas_da_passada(dets_p) + legendas_da_passada(dets_e)
    # O texto é evidência CONJUNTA: a passada que devolve só um borrão devolve
    # zero blocos de texto junto, então perguntar "esta passada viu texto?" nunca
    # desmascararia o borrão dela mesma.
    textos = caixas_de_texto(dets_p) + caixas_de_texto(dets_e)
    figuras = unir_figuras(descartar_borrao(figuras_da_passada(dets_p), textos),
                           descartar_borrao(figuras_da_passada(dets_e), textos),
                           iou_min)
    if resgates:
        # A página degenerou — é por isso que há resgate. O borrão do tamanho da
        # folha que a 1ª passada produziu NÃO pode entrar na união: ele contém
        # tudo, então absorveria a figura resgatada e morreria junto com ela na
        # rejeição. Fica só o que já passava no filtro.
        figuras = filtrar_figuras(figuras, textos, **filtros)[0]
    for bruto, faixa in resgates:
        dets_r = parse_grounding(bruto)
        figuras = unir_figuras(
            figuras,
            [Figura(remapear(f.caixa, faixa), f.legenda)
             for f in descartar_borrao(figuras_da_passada(dets_r),
                                       caixas_de_texto(dets_r))], iou_min)
        legendas += [Figura(remapear(le.caixa, faixa), le.legenda)
                     for le in legendas_da_passada(dets_r)]
        textos += [remapear(c, faixa) for c in caixas_de_texto(dets_r)]
    # FILTRA ANTES de absorver aninhadas, e a ordem não é gosto: a caixa do
    # tamanho da folha CONTÉM todas as outras, então absorver primeiro fazia a
    # figura resgatada ser engolida pelo borrão e morrer junto com ele na
    # rejeição — foi assim que a p510 do Amabis continuava perdendo a Figura 17.4
    # mesmo depois do resgate.
    aceitas, rejeitadas = filtrar_figuras(figuras, textos, **filtros)
    aceitas = completar_legendas(absorver_aninhadas(aceitas), legendas, vao_max)
    return em_ordem_de_leitura(aceitas), rejeitadas


def texto_do_grounding(bruto: str) -> str:
    """A prosa da página, sem as anotações de caixa. Serve de FONTE local para
    `legenda_ancorada` quando o livro não tem texto de capítulo disponível."""
    return "\n".join(
        d.conteudo for d in parse_grounding(bruto)
        if d.tipo not in TIPOS_FIGURA).strip()


# --- ancoragem da legenda (anti-alucinação do OCR) ---------------------------
def _palavras(texto: str) -> set:
    """Palavras de conteúdo, sem acento e sem as curtas. Normalizador local (e não
    o `textutils`) de propósito: este módulo roda no CI leve, sem dependências."""
    tabela = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ",
                           "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC")
    limpo = "".join(c if c.isalnum() or c.isspace() else " "
                    for c in (texto or "").translate(tabela).lower())
    return {p for p in limpo.split() if len(p) > 3}


def legenda_ancorada(legenda: str, texto_fonte: str, min_frac: float = 0.5) -> str:
    """Devolve a legenda só se ela estiver ANCORADA no texto do próprio livro.

    Por que existe (medido 2026-07-25, amostra do Amabis): pedir ao modelo de OCR
    para transcrever uma faixa quase vazia faz ele INVENTAR. Numa página de
    biologia em português ele produziu "2023-2024 Academic Year Calendar — a
    comprehensive tool designed to help students, parents and educators…". Rodar
    628 páginas assim injetaria centenas de legendas fabricadas num vault cujo
    projeto inteiro é anti-alucinação.

    Continua valendo com o grounding: a caixa passou a ser confiável, o TEXTO
    dentro dela não — é o mesmo decoder gerando token a token.

    A trava: o texto REAL do capítulo já existe (os jobs do worker de OCR). Uma
    legenda de verdade é feita das palavras daquele capítulo; uma alucinação, não.
    Exige que `min_frac` das palavras de conteúdo apareçam na fonte. Devolve ""
    quando reprova — o chamador cai no fallback honesto "Figura da página N",
    que é pior como rótulo e infinitamente melhor que ficção."""
    palavras = _palavras(legenda)
    if not palavras:
        return ""
    fonte = _palavras(texto_fonte)
    if not fonte:
        return legenda            # sem fonte para comparar, não há o que reprovar
    casadas = len(palavras & fonte) / len(palavras)
    return legenda if casadas >= min_frac else ""


# --- notas e links ------------------------------------------------------------
_CAP_GENERICO_RE = re.compile(r"^\s*(p[áa]ginas?|bloco|parte)\s*\d", re.IGNORECASE)


def capitulo_util(titulo: str) -> str:
    """"" quando o 'capítulo' é só um rótulo de janela de páginas.

    O worker de OCR fatia livro SEM sumário em janelas e as nomeia "páginas
    85-96". Isso é proveniência, não assunto: escrever "Figura da página 95 —
    páginas 85-96" no título da nota não ajuda ninguém a achar nada."""
    limpo = (titulo or "").strip()
    return "" if _CAP_GENERICO_RE.match(limpo) else limpo


# Stopwords de INGLÊS. Ficam aqui, e NÃO em `textutils.STOP`: aquele conjunto é o
# do aterramento léxico do RAG, e engordá-lo mudaria o gate de relevância de toda
# pergunta do sistema. O problema é local — o Cervantes é um livro em inglês, e
# `palavras_chave` só conhece stopword em português, então os temas da figura
# saíam "soil, water, WITH, roots, THAT, WHEN, MORE, growing" (medido nas 1.020
# notas da primeira passada).
_STOP_EN = frozenset("""
about above after again against also although always among another any are around
because been before being below between both but came can cannot come could did
does doing done down during each either else even ever every few for from further
had has have having here hers herself him himself his how however into its itself
just like made make many may might more most much must myself near need next nor
not now off once one only onto other others ought our ours ourselves out over own
per rather same shall she should since some such than that the their theirs them
themselves then there these they this those though through thus too under until
upon use used using very was way were what when where whether which while who
whom whose why will with within without would you your yours yourself
""".split())


# Palavras de ESTRUTURA do livro. Não são stopwords de língua — são frequentes e
# perfeitamente "significativas", e é justamente esse o problema: num livro
# didático toda legenda começa com "Figura N", então `figura` sobe ao topo da
# contagem de frequência de quase toda página e viraria `[[Figura]]`, um hub com
# centenas de arestas e zero significado. Medido nas 692 notas do Amabis.
_STOP_ESTRUTURA = frozenset((
    "figura", "figuras", "tabela", "tabelas", "quadro", "quadros", "capitulo",
    "capítulo", "pagina", "página", "paginas", "páginas", "volume", "secao",
    "seção", "item", "itens", "acima", "abaixo", "seguir", "exemplo", "exemplos",
))


def temas_do_texto(texto: str, quantos: int = 12) -> List[str]:
    """As palavras mais frequentes e significativas do trecho, em ordem.

    Reusa `textutils.palavras_chave` (mesma limpeza de acento/stopword do
    aterramento léxico do RAG) e ordena por frequência — a palavra que mais
    aparece no trecho é a que melhor o descreve."""
    from collections import Counter

    from mente_digital import textutils

    chaves = textutils.palavras_chave(texto or "", min_len=4) - _STOP_EN - _STOP_ESTRUTURA
    if not chaves:
        return []
    # `not t.isdigit()`: numeral de figura/página ("3", "129") passa pelo filtro de
    # stopword e não descreve assunto nenhum — vira ruído em milhares de notas.
    freq = Counter(t for t in textutils.tokens(texto)
                   if t in chaves and not t.isdigit())
    return [p for p, _ in freq.most_common(quantos)]


def malha_da_figura(capitulo: str, temas: Sequence[str], livro: str = "",
                    quantos: int = 5) -> List[str]:
    """Conceitos da Malha Neural para UMA figura, em ordem de especificidade.

    Por que a figura precisa disto (achado do dono em 2026-07-26, conferido nas
    1.020 notas do Cervantes): TODAS saíam sem um único wikilink de malha — o
    único `[[...]]` era o embed da própria imagem. Num vault cujo grafo é o
    produto, isso deixa mil figuras como ilhas: achá-las dependia inteiramente da
    busca vetorial, e navegar do conceito até a imagem era impossível.

    O átomo de TEXTO ganha a malha do LLM, que lê o trecho e nomeia os conceitos.
    A figura não passa por LLM nenhum — então aqui a malha é DERIVADA do que já se
    sabe de fato: o capítulo (que agrupa a figura com as outras do mesmo capítulo)
    e os temas do trecho. Limite honesto: são termos extraídos por frequência, na
    LÍNGUA DO LIVRO, e portanto não casam necessariamente com os conceitos em
    português que o LLM cunhou para os átomos de texto do mesmo livro."""
    # Tema vira Capitalizado para casar a convenção do vault ([[Ribossomos]],
    # [[Transporte]]); capítulo e livro já vêm com a capitalização do próprio
    # livro e não se mexe neles.
    candidatos = [capitulo] + [t.capitalize() for t in list(temas)[:quantos]]
    if livro:
        candidatos.append(livro)
    conceitos: List[str] = []
    vistos = set()
    for bruto in candidatos:
        limpo = _limpar_conceito(bruto)
        if not limpo or limpo.lower() in vistos:
            continue
        vistos.add(limpo.lower())
        conceitos.append(limpo)
    return conceitos


def _limpar_conceito(bruto: str) -> str:
    """Texto → nome de conceito válido dentro de `[[...]]`.

    `[`, `]`, `|`, `#` e `^` têm significado na sintaxe do wikilink e quebrariam o
    link; o prefixo numérico sai porque "9 - Light, Lamps and Electricity" e
    "10 - Soil and Containers" são o MESMO tipo de rótulo — a numeração é ordem no
    livro, não conceito.

    A VÍRGULA sai por um motivo medido: `atomos.normalizar_malha` a trata como
    separador de conceitos (é assim que ela desmonta a lista que o LLM escreve),
    então o capítulo "Light, Lamps and Electricity" chegava ao vault partido em
    `[[Light]]` + `[[Lamps and Electricity]]` — dois nós falsos no lugar de um.
    Parênteses saem pela mesma razão (aquele normalizador os descarta)."""
    limpo = re.sub(r"[\[\]|#^]", " ", (bruto or "").strip())
    limpo = re.sub(r"^\s*\d+(\.\d+)*\s*[-—–:.]\s*", "", limpo)
    limpo = re.sub(r"[,;()]", " ", limpo)
    return " ".join(limpo.split())[:80]


def corpo_da_nota(arquivo_rel: str, legenda: str, pagina: int, subpasta: str,
                  livro: str = "", capitulo: str = "",
                  temas: Sequence[str] = ()) -> str:
    """Corpo da nota de UMA figura — o que a torna achável pelo RAG.

    Este é o ponto da Fase 5c: sem uma nota própria, a figura só existia dentro de
    um bloco '## Figuras' que o atomizador fatiava para um átomo órfão cujo texto
    inteiro era 'página 3' (medido no vault: 819 linhas de figura, ZERO com
    legenda). Com texto no corpo, a figura passa a COMPETIR na busca vetorial pelo
    próprio mérito — é assim que um modelo de TEXTO 'escolhe' a imagem certa.

    E o texto NÃO depende de haver legenda. A legenda falha (nem todo livro
    legenda toda figura — no Cervantes as fotos muitas vezes não têm rótulo), e
    sem ela a nota antiga virava "Figura da página 95" — invisível para qualquer
    busca. Então toda nota carrega também a ÂNCORA DE CONTEXTO: de que página é,
    de que capítulo, e quais os temas daquele trecho do livro. Uma pergunta sobre
    fungos passa a alcançar a foto da página 129 mesmo que ninguém tenha legendado.

    Sai cru de propósito: quem chama passa por `atomos.normalizar_atomo`, que
    impõe título/tags/frontmatter iguais aos do resto do vault."""
    legenda = (legenda or "").strip()
    titulo = legenda or f"Figura da página {pagina}"
    if not legenda and capitulo:
        titulo = f"Figura da página {pagina} — {capitulo}"
    corpo = [f"## {titulo[:120]}"]
    if legenda:
        corpo.append(legenda)
    corpo.append(f"![[{subpasta}/{arquivo_rel}]]")

    onde = f"Figura da página {pagina}"
    if capitulo:
        onde += f", do capítulo '{capitulo}'"
    if livro:
        onde += f", em {livro}"
    corpo.append(onde + ".")
    if temas:
        corpo.append(f"Esta parte do livro trata de: {', '.join(temas)}.")
    malha = malha_da_figura(capitulo, temas, livro)
    if malha:
        # Mesmo rótulo e mesma sintaxe do átomo de texto (`normalizar_malha`), para
        # a figura entrar no MESMO grafo — e não num dialeto paralelo.
        corpo.append("**Malha Neural:** " + " ".join(f"[[{c}]]" for c in malha))
    return "\n".join(corpo)


def slug_do_arquivo(nome: str) -> Optional[str]:
    """'raven_p0039_f2.webp' → 'raven'. None se o nome não é de figura nossa."""
    m = _NOME_FIGURA_RE.match(nome.strip())
    return m.group("slug") if m else None


def corrigir_alvo(alvo: str, subpasta: str = "Figuras") -> Optional[str]:
    """Conserta o wikilink PLANO herdado da convenção antiga.

    Os jobs foram gerados quando as figuras iam todas soltas em `Figuras/`; o
    extrator passou a gravar UMA PASTA POR LIVRO e ninguém reescreveu os átomos —
    resultado medido: 800 de 800 links apontando para lugar nenhum. O slug sai do
    PRÓPRIO nome do arquivo, então isto vale para qualquer livro, inclusive os
    que ainda nem foram processados.

    Devolve o alvo corrigido, ou None quando não há o que fazer (já tem pasta,
    ou não é um link de figura)."""
    limpo = (alvo or "").strip()
    prefixo = f"{subpasta}/"
    if not limpo.startswith(prefixo):
        return None
    resto = limpo[len(prefixo):]
    if "/" in resto:            # já está na convenção nova
        return None
    slug = slug_do_arquivo(resto)
    return f"{prefixo}{slug}/{resto}" if slug else None


_ITEM_EMBED_RE = re.compile(r"^\s*[-*+]\s*!\[\[(?P<alvo>[^\]|#]+?)(?:\|[^\]]*)?\]\]")
_EMBED_RE = re.compile(r"!\[\[(?P<alvo>[^\]|#]+?)(?:\|[^\]]*)?\]\]")


def remover_embeds_orfaos(texto: str, existe: Callable[[str], bool],
                          subpasta: str = "Figuras") -> Tuple[str, int]:
    """Tira do texto os embeds de figura que não têm mais destino em disco.

    Diferente de `corrigir_alvo`, aqui não há conserto possível: o link aponta
    para um recorte que a detecção nova NÃO produz mais. Medido em 2026-07-26:
    71 links das sínteses da Fase 3 apontavam para figuras que a heurística velha
    tirava de páginas que são texto puro — todas confirmadas secas por auditoria
    visual. Reapontar seria inventar; o certo é remover.

    Quando o embed é o item de lista inteiro (`- ![[…]] — página 13`, a forma que
    as sínteses usam), a LINHA sai junto: sem a figura ela é um marcador vazio.
    Embed solto no meio de prosa perde só o link, o texto ao redor fica.

    `existe` é injetado (predicado sobre o alvo) para a função ficar pura e
    testável sem tocar no disco, como `parse_quando` recebe o `agora`."""
    prefixo = f"{subpasta}/"

    def _orfao(alvo: str) -> bool:
        return alvo.startswith(prefixo) and not existe(alvo)

    saida: List[str] = []
    removidos = 0

    def _troca(mm: "re.Match") -> str:
        nonlocal removidos
        if not _orfao(mm.group("alvo")):
            return mm.group(0)
        removidos += 1
        return ""

    for linha in texto.split("\n"):
        m = _ITEM_EMBED_RE.match(linha)
        if m and _orfao(m.group("alvo")):
            removidos += 1
            continue
        saida.append(_EMBED_RE.sub(_troca, linha))
    return "\n".join(saida), removidos


def resumo_contagem(por_pagina: Dict[int, int]) -> str:
    """Uma linha para o log: quantas páginas, quantas figuras, quantas páginas
    secas. A taxa de página seca é o sinal de alarme do recall — um livro didático
    com 90% das páginas sem figura provavelmente teve a detecção falhando."""
    paginas = len(por_pagina)
    figuras = sum(por_pagina.values())
    secas = sum(1 for n in por_pagina.values() if n == 0)
    pct = 100 * secas // max(1, paginas)
    return (f"{figuras} figura(s) em {paginas} página(s) | "
            f"{secas} página(s) sem figura ({pct}%)")


def embed_da_nota(caminho_nota: str, subpasta: str = "Figuras") -> Optional[str]:
    """`…/Figuras/livro/x_p0288_f1.md` → `![[Figuras/livro/x_p0288_f1.webp]]`.

    None quando o caminho não é de uma nota de figura. Puro/testável."""
    partes = str(caminho_nota or "").replace("\\", "/").split("/")
    if len(partes) < 2 or not partes[-1].lower().endswith(".md"):
        return None
    alvo = (subpasta or "").strip("/\\").casefold()
    if not alvo:
        return None
    # o SEGMENTO tem que ser a pasta (partes[:-1]), nunca o nome do arquivo
    idx = next((i for i, p in enumerate(partes[:-1]) if p.casefold() == alvo), None)
    if idx is None:
        return None
    rel = "/".join(partes[idx:-1] + [partes[-1][:-3] + ".webp"])
    return f"![[{rel}]]"


def bloco_de_figuras(fontes: Sequence[str], subpasta: str = "Figuras") -> str:
    """Os embeds das figuras que entraram no contexto, um por linha. Puro/testável.

    Existe porque pedir ao LLM que copie o wikilink não funciona: medido em
    2026-07-26, a pergunta caiu no nível `curto` (teto de 90 tokens), a resposta
    consumiu o teto inteiro e não sobrou espaço — e um caminho de 105 chars ainda
    corre o risco de sair com um caractere trocado, quebrando a <img>. O servidor
    monta o bloco a partir das fontes que a busca JÁ selecionou: determinístico,
    custo zero de token, imune à verbosidade.

    Dedup preservando a ordem (a busca entrega por distância: melhor primeiro)."""
    vistos: set = set()
    saida: List[str] = []
    for f in fontes or ():
        embed = embed_da_nota(f, subpasta)
        if embed and embed not in vistos:
            vistos.add(embed)
            saida.append(embed)
    return "\n".join(saida)
