"""
A marca do Mente Digital — desenhada em código, não versionada como binário.

Por que desenhar em vez de commitar um `.ico`: a paleta do app vive em TRÊS
lugares que precisam combinar (o gradiente do `index.html`, a tela de boot do
`app.py` e o ícone da bandeja). Um binário no repo é o único deles que ninguém
consegue editar depois — e que silenciosamente deixa de combinar quando a paleta
muda. Aqui a cor do ícone É a cor da interface, por construção. É a mesma razão
que a `bandeja.py` já dava para desenhar o orbe dela em PIL; este módulo passa a
ser a casa única daquele desenho.

O arquivo `.ico` que o Windows exige (o pywebview só aceita CAMINHO de arquivo —
`winforms.py:243`) é GERADO em `dados/marca/` na subida, não guardado no git.
Custa ~40 ms e mantém o repo sem binário.

⚠ Qualidade de ícone pequeno vem de SUPERAMOSTRAGEM: tudo é desenhado numa tela
`SUPER`× maior e reduzido com LANCZOS. Desenhar direto em 16×16 dá serrilhado —
o PIL não antialiasa `ellipse`/`polygon`, só a redução faz isso.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

# --------------------------------------------------------------------------- #
# Paleta — a MESMA do gradiente da interface                                   #
# --------------------------------------------------------------------------- #
# index.html: linear-gradient(100deg, #4285F4 0%, #9B72CB 52%, #D96570 100%)
AZUL = (66, 133, 244)
ROXO = (155, 114, 203)
ROSA = (217, 101, 112)
GRAFITE = (11, 11, 13)           # --bg do tema escuro
BRANCO = (255, 255, 255)
CINZA = (110, 114, 120)          # a mesma marca, apagada, quando descansando
CINZA_FUNDO = (60, 62, 66)

PARADAS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, AZUL), (0.52, ROXO), (1.0, ROSA),
)
PARADAS_APAGADAS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, CINZA), (1.0, CINZA_FUNDO),
)

GRAUS_CSS = 100                  # o mesmo ângulo do `linear-gradient` da interface
SUPER = 8                        # fator de superamostragem (ver docstring)

# Os tamanhos que o Windows realmente pede: 16 na barra de tarefas e no título,
# 32 no Alt+Tab, 48 na janela de arquivos, 256 no modo "ícones extra grandes".
# Os intermediários existem porque em telas a 125%/150% o Windows escolhe o mais
# próximo ACIMA e reduz — ter 20/24/40 evita que ele reduza o de 32 num tamanho
# quebrado, que é o que faz ícone parecer borrado a 125% (a escala do dono).
TAMANHOS_ICO: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)

VARIANTES = ("letras", "letras_faisca", "monograma", "assinado",
             "vazado", "faisca", "neural")

# A marca oficial (escolha do dono, 2026-08-02): o "MD" solto, sem azulejo,
# preenchido pelo degradê. `MENTE_APP_ICONE=<variante>` troca sem tocar em código.
VARIANTE_PADRAO = "letras"

# Entra no NOME do arquivo gerado. Sem isto, mexer no desenho não teria efeito
# nenhum em quem já rodou o app uma vez: `caminho_ico` só gera quando o arquivo
# não existe, e o `.ico` velho ficaria para sempre. Suba ao mudar o traçado.
VERSAO_DESENHO = 1


def variante_configurada() -> str:
    """A variante em uso. Fonte única para a janela, a bandeja e o favicon —
    os três precisam concordar, e concordar por construção."""
    import os

    escolhida = (os.environ.get("MENTE_APP_ICONE") or "").strip().lower()
    return escolhida if escolhida in VARIANTES else VARIANTE_PADRAO


# --------------------------------------------------------------------------- #
# Primitivas                                                                   #
# --------------------------------------------------------------------------- #
def cor_em(t: float, paradas: Sequence[tuple[float, tuple[int, int, int]]] = PARADAS) -> tuple[int, int, int]:
    """A cor do gradiente na posição `t` (0..1). Puro/testável."""
    t = min(1.0, max(0.0, t))
    for (p0, c0), (p1, c1) in zip(paradas, paradas[1:]):
        if t <= p1:
            span = p1 - p0
            k = 0.0 if span <= 0 else (t - p0) / span
            return tuple(round(a + (b - a) * k) for a, b in zip(c0, c1))  # type: ignore[return-value]
    return tuple(paradas[-1][1])                                          # type: ignore[return-value]


def _gradiente(lado: int, paradas=PARADAS, graus_css: int = GRAUS_CSS):
    """Quadrado `lado`×`lado` preenchido com o gradiente, no ângulo do CSS.

    Truque: uma faixa de 1 px de altura é esticada e ROTACIONADA, em vez de
    calcular a projeção pixel a pixel — evita um laço Python de centenas de
    milhares de iterações a cada tamanho gerado.
    """
    from PIL import Image

    # Diagonal: a tela intermediária precisa caber a rotação sem cantos vazios.
    diag = int(lado * 1.45) + 2
    faixa = Image.new("RGB", (diag, 1))
    faixa.putdata([cor_em(x / max(1, diag - 1), paradas) for x in range(diag)])
    # CSS conta o ângulo no sentido horário a partir de "para cima"; 90° é
    # exatamente a faixa horizontal que acabamos de montar. `rotate` gira no
    # anti-horário, e o eixo Y da imagem aponta para baixo — as duas inversões
    # se cancelam, então o delta entra com sinal direto.
    quadro = faixa.resize((diag, diag), Image.Resampling.NEAREST)
    quadro = quadro.rotate(90 - graus_css, resample=Image.Resampling.BICUBIC)
    borda = (diag - lado) // 2
    return quadro.crop((borda, borda, borda + lado, borda + lado))


def _mascara_squircle(lado: int, raio_rel: float = 0.235):
    """Máscara do azulejo de canto arredondado (a forma de ícone do Windows 11)."""
    from PIL import Image, ImageDraw

    m = Image.new("L", (lado, lado), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, lado - 1, lado - 1],
                                        radius=int(lado * raio_rel), fill=255)
    return m


def _fonte(px: int):
    """A fonte do monograma, com queda em cascata.

    Sem fonte nenhuma o desenho ainda sai (o PIL cai no bitmap embutido), só
    feio — e um ícone feio jamais pode impedir o app de abrir.
    """
    from PIL import ImageFont

    candidatas = (
        r"C:\Windows\Fonts\seguibl.ttf",     # Segoe UI Black
        r"C:\Windows\Fonts\segoeuib.ttf",    # Segoe UI Bold
        r"C:\Windows\Fonts\arialbd.ttf",
        "DejaVuSans-Bold.ttf",
    )
    for caminho in candidatas:
        try:
            return ImageFont.truetype(caminho, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _mascara_texto(lado: int, texto: str, altura_rel: float | None = None,
                   largura_rel: float | None = None, aperto: float = 0.0):
    """Máscara `L` do texto centrado, ajustado à altura E/OU à largura pedida.

    O tamanho é calibrado pela CAIXA REAL do glifo (`textbbox`), não pelo corpo
    da fonte: "MD" em Segoe UI Black ocupa ~72% do corpo, então pedir 60 px de
    fonte daria 43 px de letra e a marca sairia pequena e descentralizada. Duas
    passadas bastam porque a caixa é praticamente linear no corpo da fonte.

    Quando as duas restrições são dadas, vale a MAIS APERTADA — é o que impede
    "MD" (≈2× mais largo que alto) de vazar pela lateral ao ser ajustado só pela
    altura.
    """
    from PIL import Image, ImageDraw

    m = Image.new("L", (lado, lado), 0)
    d = ImageDraw.Draw(m)
    corpo = max(8.0, lado * (altura_rel or largura_rel or 0.4))
    caixa = d.textbbox((0, 0), texto, font=_fonte(int(corpo)))
    for _ in range(2):
        larg, alt = caixa[2] - caixa[0], caixa[3] - caixa[1]
        fatores = []
        if altura_rel and alt > 0:
            fatores.append(lado * altura_rel / alt)
        if largura_rel and larg > 0:
            fatores.append(lado * largura_rel / larg)
        if not fatores:
            break
        corpo = max(8.0, corpo * min(fatores))
        caixa = d.textbbox((0, 0), texto, font=_fonte(int(corpo)))
    fonte = _fonte(int(corpo))
    larg, alt = caixa[2] - caixa[0], caixa[3] - caixa[1]
    espaco = -aperto * corpo
    d.text(((lado - larg) / 2 - caixa[0] + espaco / 2, (lado - alt) / 2 - caixa[1]),
           texto, font=fonte, fill=255)
    return m


# --------------------------------------------------------------------------- #
# Variantes                                                                    #
# --------------------------------------------------------------------------- #
def _v_monograma(lado: int, paradas):
    """Azulejo com o gradiente e o "MD" vazado em branco. É a leitura mais
    robusta em 16 px: o que sobra num ícone minúsculo é a COR e a silhueta."""
    from PIL import Image

    tile = _gradiente(lado, paradas).convert("RGBA")
    tile.putalpha(_mascara_squircle(lado))
    texto = _mascara_texto(lado, "MD", 0.40, aperto=0.02)
    branco = Image.new("RGBA", (lado, lado), BRANCO + (255,))
    return Image.composite(branco, tile, texto)


def _v_vazado(lado: int, paradas):
    """Azulejo grafite com o "MD" PREENCHIDO pelo gradiente — a leitura literal
    do wordmark da tela de boot (`#marca b`, gradiente no texto)."""
    from PIL import Image

    fundo = Image.new("RGBA", (lado, lado), GRAFITE + (255,))
    fundo.putalpha(_mascara_squircle(lado))
    grad = _gradiente(lado, paradas).convert("RGBA")
    # 0,36 e não 0,44: "MD" é ~2× mais largo que alto, então a altura relativa
    # que "parece certa" faz a largura encostar na borda do azulejo.
    grad.putalpha(_mascara_texto(lado, "MD", 0.36, aperto=0.02))
    fora = Image.alpha_composite(fundo, grad)
    # Um fio do gradiente na borda para o azulejo não sumir em fundo escuro.
    aro = _gradiente(lado, paradas).convert("RGBA")
    aro.putalpha(_anel_squircle(lado))
    return Image.alpha_composite(fora, aro)


def _anel_squircle(lado: int, espessura_rel: float = 0.045):
    """Máscara do CONTORNO do azulejo (a máscara cheia menos a encolhida)."""
    from PIL import Image, ImageChops

    e = max(1, int(lado * espessura_rel))
    interno = Image.new("L", (lado, lado), 0)
    from PIL import ImageDraw

    ImageDraw.Draw(interno).rounded_rectangle(
        [e, e, lado - 1 - e, lado - 1 - e],
        radius=max(1, int(lado * 0.235) - e), fill=255)
    return ImageChops.subtract(_mascara_squircle(lado), interno)


def _v_faisca(lado: int, paradas):
    """A faísca de quatro pontas — o glifo que virou vocabulário visual de IA.
    Sem letra nenhuma: sobrevive a 16 px melhor que qualquer monograma."""
    from PIL import Image, ImageDraw

    tile = _gradiente(lado, paradas).convert("RGBA")
    tile.putalpha(_mascara_squircle(lado))
    m = Image.new("L", (lado, lado), 0)
    d = ImageDraw.Draw(m)
    d.polygon(_pontos_faisca(lado / 2, lado / 2, lado * 0.34, 0.30), fill=255)
    # A faísca pequena de apoio, na diagonal — o par assimétrico é o que dá o
    # ar de "cintilação" em vez de estrela de quatro pontas parada.
    d.polygon(_pontos_faisca(lado * 0.735, lado * 0.275, lado * 0.135, 0.30), fill=255)
    branco = Image.new("RGBA", (lado, lado), BRANCO + (255,))
    return Image.composite(branco, tile, m)


def _pontos_faisca(cx: float, cy: float, raio: float, cintura: float) -> list[tuple[float, float]]:
    """A faísca de 4 pontas côncavas, como polígono de 8 vértices. Puro."""
    c = raio * cintura
    d = c * 0.7071                       # o vértice do "vale", na diagonal
    return [
        (cx, cy - raio), (cx + d, cy - d), (cx + raio, cy), (cx + d, cy + d),
        (cx, cy + raio), (cx - d, cy + d), (cx - raio, cy), (cx - d, cy - d),
    ]


def _v_neural(lado: int, paradas):
    """Nó central com três satélites ligados — a leitura "rede/mente". Mais
    literal, e o mais frágil em 16 px (as ligações somem)."""
    from PIL import Image, ImageDraw

    tile = _gradiente(lado, paradas).convert("RGBA")
    tile.putalpha(_mascara_squircle(lado))
    m = Image.new("L", (lado, lado), 0)
    d = ImageDraw.Draw(m)
    import math

    cx = cy = lado / 2
    orbita, rn, rs = lado * 0.275, lado * 0.105, lado * 0.068
    pontos = [(cx + orbita * math.cos(math.radians(a)),
               cy + orbita * math.sin(math.radians(a))) for a in (-90, 30, 150)]
    for px, py in pontos:
        d.line([cx, cy, px, py], fill=255, width=max(1, int(lado * 0.035)))
        d.ellipse([px - rs, py - rs, px + rs, py + rs], fill=255)
    d.ellipse([cx - rn, cy - rn, cx + rn, cy + rn], fill=255)
    branco = Image.new("RGBA", (lado, lado), BRANCO + (255,))
    return Image.composite(branco, tile, m)


def _v_assinado(lado: int, paradas):
    """O monograma com a faísca de IA na quina — as DUAS leituras que o dono
    pediu no mesmo desenho: a sigla identifica o app, a faísca diz o que ele é.

    O "MD" desce e encolhe um pouco para abrir espaço à faísca; sem isso os dois
    elementos brigam e o ícone vira mancha em 16 px.
    """
    from PIL import Image, ImageChops, ImageDraw

    tile = _gradiente(lado, paradas).convert("RGBA")
    tile.putalpha(_mascara_squircle(lado))
    texto = _mascara_texto(lado, "MD", 0.345, aperto=0.02)
    texto = ImageChops.offset(texto, -int(lado * 0.035), int(lado * 0.075))
    faisca = Image.new("L", (lado, lado), 0)
    ImageDraw.Draw(faisca).polygon(
        _pontos_faisca(lado * 0.715, lado * 0.285, lado * 0.165, 0.30), fill=255)
    branco = Image.new("RGBA", (lado, lado), BRANCO + (255,))
    return Image.composite(branco, tile, ImageChops.lighter(texto, faisca))


def _v_letras(lado: int, paradas):
    """SÓ o "MD", preenchido pelo degradê, sobre fundo TRANSPARENTE.

    Sem azulejo o ícone vira glifo: ganha o wordmark colorido da tela de boot
    (`#marca b`) e some o retângulo que competia com os vizinhos da barra. O
    preço é que a marca passa a depender do contraste do fundo alheio — daí o
    ajuste ser por LARGURA (as letras ficam o maior possível) e o degradê ser o
    de meio-tom, que sobrevive em barra clara e escura.
    """
    grad = _gradiente(lado, paradas).convert("RGBA")
    grad.putalpha(_mascara_texto(lado, "MD", largura_rel=0.98, aperto=0.03))
    return grad


def _v_letras_faisca(lado: int, paradas):
    """O mesmo "MD" solto, com a faísca de IA sobrescrita na quina do D."""
    from PIL import Image, ImageChops, ImageDraw

    texto = _mascara_texto(lado, "MD", largura_rel=0.90, aperto=0.03)
    texto = ImageChops.offset(texto, -int(lado * 0.05), int(lado * 0.055))
    faisca = Image.new("L", (lado, lado), 0)
    ImageDraw.Draw(faisca).polygon(
        _pontos_faisca(lado * 0.845, lado * 0.30, lado * 0.15, 0.30), fill=255)
    grad = _gradiente(lado, paradas).convert("RGBA")
    grad.putalpha(ImageChops.lighter(texto, faisca))
    return grad


_DESENHOS = {"letras": _v_letras, "letras_faisca": _v_letras_faisca,
             "monograma": _v_monograma, "assinado": _v_assinado,
             "vazado": _v_vazado, "faisca": _v_faisca, "neural": _v_neural}


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #
def desenhar_marca(tamanho: int = 256, variante: str | None = None, ativo: bool = True):
    """A marca, no tamanho pedido. Puro: devolve uma `PIL.Image`, não toca disco.

    `ativo=False` devolve a MESMA marca em cinza — é o retorno visual de que os
    modelos estão descansando, sem precisar abrir a janela (usado na bandeja).
    """
    from PIL import Image

    variante = variante or variante_configurada()
    desenho = _DESENHOS.get(variante)
    if desenho is None:
        raise ValueError(f"variante desconhecida: {variante!r} (use uma de {VARIANTES})")
    paradas = PARADAS if ativo else PARADAS_APAGADAS
    grande = desenho(tamanho * SUPER, paradas)
    return grande.resize((tamanho, tamanho), Image.Resampling.LANCZOS)


def gerar_ico(destino: Path, variante: str | None = None,
              tamanhos: Sequence[int] = TAMANHOS_ICO) -> Path:
    """Escreve o `.ico` multi-resolução que o Windows exige. Devolve o caminho.

    Um `.ico` com VÁRIAS resoluções não é luxo: com só a de 256, o Windows reduz
    ele mesmo para os 16 px da barra de tarefas com um filtro pobre, e a marca sai
    borrada. Cada tamanho aqui foi reduzido com LANCZOS a partir da tela
    superamostrada.
    """
    from PIL import Image

    destino.parent.mkdir(parents=True, exist_ok=True)
    quadros = [desenhar_marca(n, variante) for n in sorted(tamanhos, reverse=True)]
    maior = quadros[0]
    # `append_images` guarda cada tamanho como um quadro PRÓPRIO; sem ele o
    # Pillow rasteriza os `sizes` a partir do maior, que é justamente a redução
    # ruim que se quer evitar.
    maior.save(destino, format="ICO",
               sizes=[(q.width, q.height) for q in quadros],
               append_images=quadros[1:])
    return destino


def caminho_ico(base: Path, variante: str | None = None) -> Path:
    """O `.ico` em `dados/marca/`, gerado se ainda não existir.

    Fail-soft: qualquer erro devolve um caminho inexistente e quem chama segue
    sem ícone (o pywebview cai no ícone do python.exe, que é o estado anterior).
    Ícone é acabamento; nunca pode impedir a janela de abrir.
    """
    variante = variante or variante_configurada()
    alvo = base / "dados" / "marca" / f"mente_digital_{variante}_v{VERSAO_DESENHO}.ico"
    if alvo.exists():
        return alvo
    try:
        return gerar_ico(alvo, variante)
    except Exception as exc:                       # noqa: BLE001 - cosmético
        print(f"[MARCA] não consegui gerar o ícone (segue sem): {exc}")
        return alvo
