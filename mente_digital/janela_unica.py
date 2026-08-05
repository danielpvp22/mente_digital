"""
Instância única — clicar no atalho com o app aberto TRAZ a janela, não sobe outra.

O pedido (dono, 2026-08-05): "comportamento de app, não de um código genérico
rodando em python puro". Um app de verdade, clicado duas vezes, aparece; não abre
uma segunda cópia de si mesmo.

O QUE JÁ EXISTIA — e o que faltava
-----------------------------------
Metade do mecanismo já estava de pé e NÃO foi duplicada: `rede.porta_em_uso`
(teste por BIND, ~0,2 ms) e o ramo do `app.main` que, vendo a porta ocupada,
recusa subir um segundo servidor. Isso já impedia o pior — o zumbi de VRAM que o
`rede.py` documenta. O que faltava é que, depois de recusar o servidor, ele ainda
CRIAVA uma janela nova apontada para o servidor velho: dois botões na barra, duas
webviews, ~200 MB à toa, e o dono achando que o app "abriu de novo".

Este módulo só acrescenta o passo que faltava: achar a janela viva e trazê-la.
A decisão fica em `decidir`, PURA — o `app.py` continua dono do fluxo.

⚠ POR QUE NÃO PELA BANDEJA. `bandeja.py` sabe restaurar a janela (o item "Abrir"
chama `janela.show()/restore()`), e seria o caminho elegante — mas ele só funciona
DENTRO do processo vivo: o pystray é um laço em thread, sem porta, sem fila, sem
nada que um segundo processo possa chamar. Um processo novo não tem como pedir
nada a ele. Por isso o caminho é o HWND, que é justamente a interface que o
Windows expõe entre processos.

⚠ A JANELA PODE ESTAR ESCONDIDA NA BANDEJA, não só minimizada — são estados
DIFERENTES no Win32 e o conserto de um não serve para o outro. Fechar no X chama
`janela.hide()` (o `_ao_fechar` do `app.py`), que é `ShowWindow(SW_HIDE)`: a
janela some da barra e `IsIconic` responde FALSE, porque ela não está minimizada,
está invisível. Tratar só o minimizado deixaria o caso mais comum — o dono fecha
no X o dia inteiro — sem conserto. Daí os dois ramos em `trazer_para_frente`.

⚠ FOCO NÃO SE TOMA, SE CEDE. O Windows recusa `SetForegroundWindow` vindo de um
processo que não está em primeiro plano: o botão só pisca laranja. Quem acabou de
ser lançado pelo usuário TEM esse direito por um instante — então o processo NOVO
(este) chama `AllowSetForegroundWindow` cedendo a vez e só depois manda a janela
velha subir. É o sentido contrário do que parece: quem cede é quem chegou.
"""
from __future__ import annotations

import os

#: `ShowWindow`. SW_SHOW tira do escondido (bandeja); SW_RESTORE desminimiza.
_SW_SHOW, _SW_RESTORE = 5, 9
#: `AllowSetForegroundWindow(ASFW_ANY)` — libera qualquer processo a roubar o foco
#: DESTE. Só faz sentido porque este processo está de saída.
_ASFW_ANY = -1


def e_windows() -> bool:
    """Seam de plataforma. Quem simula Windows troca ISTO, nunca o `os.name` —
    ver `inicializacao.e_windows` para o INTERNALERROR que aquilo causa no CI."""
    return os.name == "nt"


def decidir(porta_ocupada: bool, tem_janela: bool, quer_janela: bool) -> str:
    """O que fazer ao subir. PURA — é aqui que o comportamento é testável sem
    Windows, sem servidor e sem janela.

    - `"subir"`   — ninguém de pé: sobe servidor e janela (o caminho normal).
    - `"restaurar"` — já tem app COM janela e queremos uma visível: traz a dele e sai.
    - `"adotar"`  — servidor de pé mas sem janela (alguém rodou `python main.py`, ou
      subimos com `--sem-janela`): cria a janela apontada nele, que é o que o
      `app.py` já fazia. Também é o destino de quem NÃO quer janela visível
      (`--standby`, `--oculto`): ali trazer nada para a frente é o certo — quem
      pediu está em outro cômodo.
    """
    if not porta_ocupada:
        return "subir"
    if quer_janela and tem_janela:
        return "restaurar"
    return "adotar"


def achar_janela(titulo: str) -> int:
    """O HWND da janela do app, ou 0.

    Por TÍTULO porque é o que se tem: a janela é do backend WinForms do pywebview,
    cuja classe é gerada (`WindowsForms10.Window.8.app.0.…`, com sufixo que muda
    entre execuções) e portanto pior identificador que o título. O título é
    estável: o pywebview o fixa uma vez (`winforms.py:197`) e NÃO o sincroniza com
    o `<title>` da página — não há handler de DocumentTitleChanged no backend
    (conferido em 2026-08-05), então navegar no app não o altera.

    `FindWindowW` acha inclusive janela ESCONDIDA, que é o caso da bandeja.

    Seguro contra achar a si mesmo: quem chama isto ainda não criou janela nenhuma
    (o `app.py` decide antes do `create_window`).
    """
    if not e_windows() or not titulo:
        return 0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        # ⚠ restype OBRIGATÓRIO: HWND é ponteiro e o ctypes assume `c_int`,
        # truncando em 64 bits — o handle chegaria corrompido e o
        # `ShowWindow` seguinte agiria sobre nada. Mesma classe do que
        # `energia.py:51` documenta para `GetCurrentProcess`.
        user32.FindWindowW.restype = ctypes.c_void_p
        return int(user32.FindWindowW(None, titulo) or 0)
    except Exception as exc:                       # noqa: BLE001 - conveniência, nunca requisito
        print(f"[APP] não consegui procurar a janela existente: {exc}")
        return 0


def trazer_para_frente(hwnd: int) -> bool:
    """Mostra, desminimiza e põe em primeiro plano. Devolve se conseguiu.

    Fail-soft: qualquer erro devolve False e quem chama segue para o caminho
    antigo (criar a janela). Uma janela que não veio para a frente é chateação;
    um app que não abre é defeito.
    """
    if not e_windows() or not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        janela = ctypes.c_void_p(hwnd)
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        if not user32.IsWindow(janela):
            return False                           # morreu entre achar e usar

        # Cedemos o foco ANTES de pedir — ver o cabeçalho do módulo.
        user32.AllowSetForegroundWindow.argtypes = [ctypes.c_int]
        user32.AllowSetForegroundWindow(_ASFW_ANY)

        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        # Minimizada e escondida são estados distintos — ver o cabeçalho.
        user32.ShowWindow(janela, _SW_RESTORE if user32.IsIconic(janela) else _SW_SHOW)

        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        if user32.SetForegroundWindow(janela):
            return True
        # Recusado (outro app segurava o foco). `SwitchToThisWindow` é a porta que
        # o próprio Alt+Tab usa e não depende do direito de primeiro plano; a
        # janela já está visível de qualquer forma, então o pior caso aqui é ela
        # aparecer sem o foco do teclado.
        user32.SwitchToThisWindow.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        user32.SwitchToThisWindow(janela, True)
        return True
    except Exception as exc:                       # noqa: BLE001
        print(f"[APP] não consegui trazer a janela existente para a frente: {exc}")
        return False
