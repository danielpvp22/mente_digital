"""
Redimensionar a janela SEM MOLDURA — devolver as bordas que o `frameless` levou.

O DEFEITO (dono, 2026-08-05): "não consigo redimensionar a janela ao desmaximizar".
Não era regressão nem `resizable=False`: a janela nasce `frameless=True` (`app.py`),
e as alças de redimensionar do Windows vivem na área NÃO-CLIENTE, que é justamente
o que a moldura contém. Sem moldura não há um pixel para agarrar. Somando
`shadow=False` e a região arredondada (`arredondar_janela`), não sobra borda
nenhuma. `resizable` era irrelevante — ele já era True.

O CONSERTO é o padrão das barras de título próprias: interceptar `WM_NCHITTEST` e
MENTIR sobre a zona. Quando o cursor está a poucos pixels da extremidade,
respondemos `HTLEFT`/`HTTOPRIGHT`/... em vez de `HTCLIENT`, e o Windows faz o
resto NATIVAMENTE — cursor de seta dupla, arrasto de canto, Aero Snap, duplo
clique na borda para esticar. Zero JS, zero moldura visível.

⚠ SÓ O HIT-TEST NÃO BASTA, E ISSO FOI MEDIDO. O WebView2 não desenha na janela:
ele empilha filhos (`Chrome_WidgetWin_0/1`, `Chrome_RenderWidgetHostHWND`,
`Intermediate D3D Window`) sobre o retângulo INTEIRO, e o `WM_NCHITTEST` do mouse
real chegou ao formulário **zero vez** em 2026-08-05 — o pixel é dos filhos, não
nosso. Pior: três desses filhos vivem em OUTRO PROCESSO (o `msedgewebview2.exe`),
então a saída clássica de subclassá-los e devolver `HTTRANSPARENT` está fechada —
não se instala window procedure em processo alheio.

Por isso `reservar_borda` vem antes: uma faixa de `MARGEM` px de `Padding` no
formulário encolhe o WebView2 e devolve ao Windows uma moldura de pixels que é
NOSSA. É onde o hit-test passa a chegar. O preço é honesto e é o único: essa
faixa não mostra página. Ela é pintada com o `background_color` da janela
(`#0B0B0D`), o MESMO valor de `--bg` do tema escuro em `templates/index.html`, e
portanto some no tema padrão; no tema claro (`--bg: #FFFFFF`) ela aparece como um
friso escuro de 6 px. É a troca que existe: pixel para agarrar é pixel que não
desenha a página.

⚠ POR QUE `WS_THICKFRAME` TAMBÉM ENTRA. Só o hit-test não basta: ao soltar
`HTLEFT`, o `DefWindowProc` manda `WM_SYSCOMMAND SC_SIZE`, e o laço de
dimensionamento do Windows RECUSA a janela que não tem `WS_THICKFRAME` — o
cursor mudaria e o arrasto não moveria nada. `FormBorderStyle.None` (o que o
pywebview aplica no frameless, `winforms.py:271`) tira esse bit. Então
`instalar` o devolve e neutraliza o efeito colateral por `WM_NCCALCSIZE`:
respondendo 0 com `wParam=TRUE`, a área cliente passa a ser a janela INTEIRA e a
moldura volta a ter zero pixel de espessura. A estética não muda; só o
comportamento.

⚠ A REGIÃO ARREDONDADA CLIPA O HIT-TEST, não só o desenho. `SetWindowRgn` diz ao
Windows o que É a janela: o pixel fora da curva não pertence a ela e o
`WM_NCHITTEST` nunca chega ali — o clique atravessa para a janela de trás.
MEDIDO em 2026-08-05 no canto superior esquerdo (raio 20 px físicos, faixa de 8):
o ponto relativo (1,1) cai FORA e vai para a janela de baixo; de (3,3) a (7,7) é
nosso e responde `HTTOPLEFT`. Ou seja, some a PONTA — cerca de 2 px na diagonal —
e sobra o resto do canto. Por isso a zona de canto (`CANTO`) é bem maior que a da
borda (`MARGEM`): os BRAÇOS do "L" — (2,14) e (14,2), medidos como `HTTOPLEFT` —
ficam longe da curva e são o alvo confortável. É também o que o Windows faz na
moldura de verdade: canto mais generoso que lado.

A decisão fica em `zona`, PURA: recebe coordenada e tamanho, devolve a constante.
É ela que carrega os testes e roda no CI Linux. O que toca Win32 (`instalar`)
fica atrás da guarda de plataforma, com `ctypes.windll` DENTRO da função e sem
`ctypes.wintypes` no topo do módulo — ver `inicializacao.e_windows` para o
INTERNALERROR que a alternativa causa no CI.
"""
from __future__ import annotations

import os

#: Zonas do `WM_NCHITTEST` (winuser.h). São o vocabulário com que uma janela
#: responde "que parte de mim é este pixel".
HTCLIENT = 1
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

#: px LÓGICOS de borda. O Windows usa 4-8 (`SM_CXSIZEFRAME` + `SM_CXPADDEDBORDER`);
#: fino de propósito, porque a faixa de cima é roubada da `.pywebview-drag-region`
#: — ver `zona`.
MARGEM = 6
#: px LÓGICOS de canto. Maior que a borda por DOIS motivos: o Windows também faz
#: assim (canto é alvo pequeno), e a região arredondada come a ponta.
CANTO = 16

_WM_NCHITTEST = 0x0084
_WM_NCCALCSIZE = 0x0083
_WM_GETMINMAXINFO = 0x0024
_GWL_STYLE = -16
_GWLP_WNDPROC = -4
_WS_THICKFRAME = 0x00040000
_SWP_NOMOVE, _SWP_NOSIZE, _SWP_NOZORDER, _SWP_FRAMECHANGED = 0x0002, 0x0001, 0x0004, 0x0020

#: hwnd -> (proc novo, proc antigo). O CALLBACK PRECISA DE REFERÊNCIA VIVA: sem
#: isto o ctypes coleta o thunk e o Windows chama memória liberada na primeira
#: mexida do mouse — o app morre sem traceback.
_INSTALADOS: dict[int, tuple] = {}


def e_windows() -> bool:
    """Seam de plataforma. Quem simula Windows troca ISTO, nunca o `os.name` do
    processo — ver `inicializacao.e_windows`."""
    return os.name == "nt"


def zona(x: int, y: int, largura: int, altura: int,
         margem: int = MARGEM, canto: int = CANTO) -> int:
    """Que parte da janela é o ponto `(x, y)`, em coordenadas RELATIVAS a ela.

    PURA — é aqui que o comportamento é testável sem Windows, sem janela e sem
    mouse. `largura`/`altura` e as margens têm de vir na MESMA unidade (o
    chamador Win32 usa pixel físico; ver `instalar`).

    A borda tem `margem` de espessura; o canto vale por `canto` ao longo de cada
    eixo, então a área de canto é um "L" mais generoso que o quadradinho da
    interseção — é o que torna o canto agarrável apesar da curva da região.

    ⚠ ORDEM IMPORTA, e é deliberada: a borda VENCE a região de arrasto. Os
    primeiros `margem` pixels do topo pertencem ao redimensionamento, não à
    `.pywebview-drag-region`; sobram ~32 px de barra para arrastar a janela, que
    é exatamente como se comporta um título nativo. O contrário (arrasto vencendo)
    devolveria o defeito: sem faixa de borda no topo não há como esticar por cima.

    Janela minúscula não vira "só borda": as margens são limitadas a 1/3 e 1/2 do
    menor lado, o que também mantém os cantos opostos disjuntos.
    """
    if largura <= 0 or altura <= 0:
        return HTCLIENT
    lado_menor = min(largura, altura)
    margem = min(max(int(margem), 0), lado_menor // 3)
    if margem <= 0:
        return HTCLIENT
    canto = min(max(int(canto), margem), lado_menor // 2)

    esquerda, direita = x < margem, x >= largura - margem
    topo, base = y < margem, y >= altura - margem
    if not (esquerda or direita or topo or base):
        return HTCLIENT

    c_esquerda, c_direita = x < canto, x >= largura - canto
    c_topo, c_base = y < canto, y >= altura - canto

    if (topo and c_esquerda) or (esquerda and c_topo):
        return HTTOPLEFT
    if (topo and c_direita) or (direita and c_topo):
        return HTTOPRIGHT
    if (base and c_esquerda) or (esquerda and c_base):
        return HTBOTTOMLEFT
    if (base and c_direita) or (direita and c_base):
        return HTBOTTOMRIGHT
    if esquerda:
        return HTLEFT
    if direita:
        return HTRIGHT
    if topo:
        return HTTOP
    return HTBOTTOM


def hwnd_de(janela) -> int:
    """O HWND por trás de uma janela do pywebview, ou 0.

    `Handle` é um `System.IntPtr` do .NET (pythonnet) e `int()` NÃO o converte —
    "int() argument must be … not 'IntPtr'", medido em 2026-08-02. `ToInt64()` é
    o caminho oficial; o `int()` fica de reserva para o dia em que o pywebview
    trocar de backend e devolver inteiro puro.
    """
    try:
        bruto = janela.native.Handle
    except AttributeError:                                # antes do `shown`
        return 0
    return bruto.ToInt64() if hasattr(bruto, "ToInt64") else int(bruto)


def reservar_borda(janela, margem: int = MARGEM) -> bool:
    """Encolhe o WebView2 e devolve uma faixa de `margem` px LÓGICOS ao formulário.

    Sem ela o `instalar` é decorativo: os filhos do WebView2 cobrem cada pixel da
    janela e o `WM_NCHITTEST` nunca chega (ver o cabeçalho do módulo). O
    `Padding` do WinForms é o caminho barato porque o controle está `Dock=Fill`:
    ele passa a ocupar o `DisplayRectangle`, já descontado. E a faixa sai pintada
    de graça com o `BackColor` do formulário — que o `app.py` já define como
    `background_color`, o mesmo `#0B0B0D` do fundo da página.

    Idempotente e barata: sai na hora se o `Padding` já é o pedido. É reaplicada
    no `resized` porque MUDANÇA DE MONITOR muda o DPI, e uma faixa em pixel
    físico calculada para 100% vira alça fina demais numa tela a 150%.
    """
    if not e_windows():
        return False
    try:
        import ctypes

        from System.Windows.Forms import Padding             # pythonnet, tardio

        hwnd = hwnd_de(janela)
        if not hwnd:
            return False
        dpi = 96
        try:
            user32 = ctypes.windll.user32
            user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
            user32.GetDpiForWindow.restype = ctypes.c_uint
            dpi = user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) or 96
        except (AttributeError, OSError):
            pass
        fisico = max(1, int(round(margem * dpi / 96.0)))
        if janela.native.Padding.All == fisico:
            return True
        janela.native.Padding = Padding(fisico)
        return True
    except Exception as exc:                                 # noqa: BLE001
        print(f"[APP] não consegui reservar a borda de redimensionar: {exc}")
        return False


def parse_cor(texto) -> tuple[int, int, int] | None:
    """`#RGB` / `#RRGGBB` -> `(r, g, b)`, ou `None` se não for uma cor.

    PURA — e é a FRONTEIRA. A cor vem do front (é ele quem sabe o tema; ver
    `pintar_faixa`), então entra aqui como texto de fora e só sai como três
    inteiros de 0 a 255. Aceita a forma curta porque o CSS aceita; NÃO aceita
    nome ("white") nem `rgb()`: o front manda o valor de `--bg`, que é sempre
    hex, e ampliar a gramática só ampliaria a superfície de quem chama uma API
    nativa com string de fora.
    """
    if not isinstance(texto, str):
        return None
    limpo = texto.strip().lstrip("#")
    if len(limpo) == 3:
        limpo = "".join(c * 2 for c in limpo)
    if len(limpo) != 6:
        return None
    try:
        return int(limpo[0:2], 16), int(limpo[2:4], 16), int(limpo[4:6], 16)
    except ValueError:
        return None


def pintar_faixa(janela, cor) -> bool:
    """Pinta a faixa reservada por `reservar_borda` com a cor que o front mandar.

    A faixa é do FORMULÁRIO, não da página: quem a desenha é o WinForms, então
    ela não enxerga o `data-tema` do CSS. Nascendo com o `background_color` do
    `app.py` (`#0B0B0D`), no tema claro ela vira um friso quase-preto em volta da
    página branca — e não é caso de canto: quem nunca tocou no botão de tema e
    usa o Windows em modo claro ABRE assim (`index.html` faz
    `localStorage.getItem('mente_tema') || temaDoSistema()`).

    ⚠ A cor vem do FRONT, não de uma tabela aqui, de propósito. O valor certo é o
    `--bg` do tema vigente, e ele já existe num lugar só — o CSS. Uma cópia em
    Python seria uma segunda fonte, que envelheceria calada na próxima troca de
    paleta; e nem daria para acertar com uma tabela só, porque a tela de boot usa
    um claro DIFERENTE do da página (`#F7F7F9` contra `#FFFFFF`). Quem sabe a cor
    é quem a desenha.

    Fail-soft como o resto do módulo: cor inválida ou erro do WinForms deixa a
    faixa como estava. Um friso da cor errada nunca pode derrubar a janela.
    """
    if not e_windows():
        return False
    rgb = parse_cor(cor)
    if rgb is None:
        return False
    try:
        from System.Drawing import Color                       # pythonnet, tardio

        if not hwnd_de(janela):                                # antes do `shown`
            return False
        atual = janela.native.BackColor
        if (atual.R, atual.G, atual.B) == rgb:                 # idempotente e barata
            return True
        janela.native.BackColor = Color.FromArgb(*rgb)
        return True
    except Exception as exc:                                   # noqa: BLE001
        print(f"[APP] não consegui pintar a faixa de redimensionar: {exc}")
        return False


def ativar(janela, margem: int = MARGEM, canto: int = CANTO) -> bool:
    """Liga o redimensionamento da janela sem moldura. Só depois do `shown`.

    Os dois passos são indivisíveis — `reservar_borda` cria os pixels e
    `instalar` os interpreta —, mas quem chama não precisa saber disso.

    O piso vem do `min_size` que o `app.py` já declara: uma fonte só, e o dia em
    que ele mudar lá o arrasto acompanha.
    """
    hwnd = hwnd_de(janela)
    if not hwnd:
        return False
    reservar_borda(janela, margem)
    minimo = getattr(janela, "min_size", None)
    return instalar(hwnd, margem, canto, minimo)


def instalar(hwnd: int, margem: int = MARGEM, canto: int = CANTO,
             minimo: tuple[int, int] | None = None) -> bool:
    """Devolve o redimensionamento nativo ao HWND. Diz se conseguiu.

    ⚠ SÓ DEPOIS DO `shown`: antes disso `janela.native` é None e o HWND não
    existe (medido em 2026-08-02, ver `app.arredondar_janela`).

    `minimo` é o `(largura, altura)` LÓGICO abaixo do qual a janela não encolhe;
    `None` deixa o piso com quem já cuidava dele.

    Idempotente: chamar duas vezes para o mesmo HWND não empilha subclass.

    Fail-soft por completo — qualquer erro deixa a janela exatamente como estava
    (sem resize, que é o estado de hoje) e o app abre do mesmo jeito. Uma janela
    de tamanho fixo é chateação; um app que não abre é defeito.
    """
    if not e_windows() or not hwnd:
        return False
    if hwnd in _INSTALADOS:
        return True
    try:
        import ctypes
        from ctypes import wintypes                      # tardio de propósito

        user32 = ctypes.windll.user32
        # LRESULT/LPARAM são LONG_PTR (com sinal) e WPARAM é UINT_PTR — em 64 bits
        # o default `c_int` do ctypes TRUNCARIA o handle e o lParam.
        proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                                   ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                          ctypes.c_size_t, ctypes.c_ssize_t]
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        user32.IsZoomed.argtypes = [ctypes.c_void_p]
        user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        user32.GetDpiForWindow.restype = ctypes.c_uint

        def _fisico(valor: int) -> int:
            """Margem lógica -> pixel físico. Sem isto, numa tela a 150% a borda
            de 6 px encolheria para 4 px reais e o dono continuaria caçando a
            alça. `GetDpiForWindow` responde pelo monitor ONDE A JANELA ESTÁ,
            então arrastá-la para outra tela continua certo (mesma razão do
            `arredondar_janela`)."""
            dpi = 96
            try:
                lido = user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
                if lido:
                    dpi = lido
            except (AttributeError, OSError):             # Windows < 10 1607
                pass
            return max(1, int(round(valor * dpi / 96.0)))

        def _hit(lparam: int) -> tuple[int, int]:
            """As coordenadas de TELA embutidas no lParam. Os 16 bits são COM
            SINAL: num monitor à esquerda do principal, x é negativo — ler como
            unsigned poria o cursor a 65 mil pixels de distância."""
            x, y = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
            return (x - 0x10000 if x >= 0x8000 else x,
                    y - 0x10000 if y >= 0x8000 else y)

        # Célula, não variável: o proc é instalado ANTES de sabermos o endereço
        # do anterior, e uma mexida de mouse nessa fresta chamaria um nome que
        # ainda não existe. Enquanto a célula estiver vazia, o fallback é o
        # `DefWindowProcW`.
        anterior: dict[str, object] = {"proc": None}

        class _MinMaxInfo(ctypes.Structure):
            _fields_ = [("reservado", wintypes.POINT), ("max_tam", wintypes.POINT),
                        ("max_pos", wintypes.POINT), ("min_arrasto", wintypes.POINT),
                        ("max_arrasto", wintypes.POINT)]

        def _encadear(janela, msg, wparam, lparam):
            antigo = anterior["proc"]
            if antigo is None:
                return user32.DefWindowProcW(ctypes.c_void_p(janela), msg, wparam, lparam)
            return user32.CallWindowProcW(antigo, janela, msg, wparam, lparam)

        def _proc(janela, msg, wparam, lparam):
            if msg == _WM_GETMINMAXINFO and minimo and lparam:
                # ⚠ O PISO PRECISA SER NOSSO. O pywebview converte `min_size` para
                # pixel físico com a escala do monitor onde a janela NASCEU
                # (`winforms.py:210`), e ela nasce escondida, na geometria salva —
                # que pode ser outra tela. Medido em 2026-08-05 na janela real: o
                # arrasto parou em 360x560 FÍSICOS num monitor a 125%, ou seja
                # 288x448 lógicos, abaixo do mínimo que a interface suporta. Aqui
                # o piso é recalculado no DPI de AGORA, a cada pedido.
                # Encadeia primeiro para não perder as métricas de maximizar que o
                # WinForms escreve nesta mesma estrutura.
                r = _encadear(janela, msg, wparam, lparam)
                try:
                    # ⚠ `from_address`, NÃO `ctypes.cast`: aqui o lParam chega como
                    # int Python (o protótipo o declara `c_ssize_t`) e o `cast`
                    # recusa inteiro cru com ArgumentError. Como isto roda DENTRO
                    # de um callback do Windows, a exceção não sobe para lugar
                    # nenhum — o ctypes imprime e devolve 0 —, então o defeito
                    # aparecia só como "o piso não mudou". Foi o que aconteceu na
                    # primeira medição de 2026-08-05.
                    # `max`, nunca atribuição seca: quem já pôs um piso ali sabe de
                    # algo que talvez nós não saibamos, e derrubá-lo seria trocar um
                    # limite certo por um chute. Só SUBIMOS o piso.
                    info = _MinMaxInfo.from_address(lparam)
                    info.min_arrasto.x = max(info.min_arrasto.x, _fisico(minimo[0]))
                    info.min_arrasto.y = max(info.min_arrasto.y, _fisico(minimo[1]))
                except Exception as exc:                      # noqa: BLE001
                    print(f"[APP] piso de tamanho não aplicado: {exc}")
                return r
            maximizada = bool(user32.IsZoomed(ctypes.c_void_p(hwnd)))
            if msg == _WM_NCCALCSIZE and wparam and not maximizada:
                # A moldura que o `WS_THICKFRAME` traz de volta passa a ter
                # espessura ZERO: a área cliente é a janela inteira. É o que
                # preserva a estética sem moldura enquanto o laço de
                # dimensionamento do Windows volta a funcionar.
                # ⚠ MAXIMIZADA, NÃO. Ali o Windows infla o retângulo da janela
                # para além do monitor justamente na espessura da moldura; zerar
                # a conta empurraria a página para fora da tela nos quatro lados.
                # Deixando o `DefWindowProc` fazer o de sempre, a moldura fica
                # fora da tela — invisível de graça.
                return 0
            if msg == _WM_NCHITTEST and not maximizada:
                rect = wintypes.RECT()
                if user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                    x, y = _hit(lparam)
                    achado = zona(x - rect.left, y - rect.top,
                                  rect.right - rect.left, rect.bottom - rect.top,
                                  _fisico(margem), _fisico(canto))
                    if achado != HTCLIENT:
                        return achado
            return _encadear(janela, msg, wparam, lparam)

        # A ordem é: primeiro o subclass, DEPOIS o estilo. O `SWP_FRAMECHANGED`
        # dispara um `WM_NCCALCSIZE` na hora, e ele precisa cair no nosso proc —
        # senão a janela pisca com a moldura de verdade antes de perdê-la.
        novo = proto(_proc)
        antigo = _troca_ponteiro(user32, hwnd, ctypes.cast(novo, ctypes.c_void_p))
        if not antigo:
            return False
        anterior["proc"] = ctypes.c_void_p(antigo)
        _INSTALADOS[hwnd] = (novo, antigo)

        estilo = _ler_estilo(user32, hwnd)
        if estilo and not estilo & _WS_THICKFRAME:
            _escrever_estilo(user32, hwnd, estilo | _WS_THICKFRAME)
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED)
        return True
    except Exception as exc:                              # noqa: BLE001 - conforto, nunca requisito
        print(f"[APP] não consegui devolver o redimensionar da janela: {exc}")
        return False


def _ponteiro_longo(user32, sufixo: str):
    """`SetWindowLongPtrW`/`GetWindowLongPtrW` em 64 bits, `...LongW` em 32.
    O nome com `Ptr` NÃO EXISTE no user32 de 32 bits — procurá-lo lá levanta
    AttributeError, e é por isso que a escolha é feita aqui e não no `import`."""
    return getattr(user32, f"{sufixo}PtrW", None) or getattr(user32, f"{sufixo}W")


def _ler_estilo(user32, hwnd: int) -> int:
    import ctypes

    fn = _ponteiro_longo(user32, "GetWindowLong")
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_ssize_t
    return int(fn(ctypes.c_void_p(hwnd), _GWL_STYLE))


def _escrever_estilo(user32, hwnd: int, estilo: int) -> None:
    import ctypes

    fn = _ponteiro_longo(user32, "SetWindowLong")
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    fn.restype = ctypes.c_ssize_t
    fn(ctypes.c_void_p(hwnd), _GWL_STYLE, estilo)


def _troca_ponteiro(user32, hwnd: int, novo) -> int:
    """Instala o proc e devolve o anterior."""
    import ctypes

    fn = _ponteiro_longo(user32, "SetWindowLong")
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    fn.restype = ctypes.c_void_p
    return int(fn(ctypes.c_void_p(hwnd), _GWLP_WNDPROC, novo) or 0)
