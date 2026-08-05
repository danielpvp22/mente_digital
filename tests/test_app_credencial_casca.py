"""A casca de aplicativo inteira se autentica pela MESMA credencial.

O que isto protege não é uma linha de código, é uma propriedade do `app.py`:
**nenhuma chamada a rota GATEADA pode carregar o `settings.access_token` cru**.

Por que existe. A janela nativa já passava por `credencial_da_janela` — mas ela é
só a metade visível da casca. Em volta rodam a bandeja (`/api/energia`,
`/api/idle`), o laço de ociosidade e o religar do `--standby`, e todos os seis
pontos passavam o segredo legado direto. Ligar `MENTE_APARELHOS_TOKEN_LEGADO=false`
quebraria essa automação em SILÊNCIO: a janela abriria normalmente e só horas
depois o dono notaria que o botão da bandeja não faz nada e que o PC nunca mais
dormiu. Um defeito que não aparece na abertura e não aparece na suíte é o mais
caro de achar — daí a trava ser no SOURCE, e não num comportamento em runtime que
só se reencena subindo o app.

⚠ A verificação analisa a ÁRVORE (`ast`), não o texto: `grep` por
`settings.access_token` casaria as docstrings que explicam justamente esta regra,
e um teste que se satisfaz com o próprio comentário não trava nada.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import app

APP_PY = Path(app.__file__)

# Os ÚNICOS lugares onde o segredo legado pode aparecer como argumento.
#
# - `credencial_da_janela`: a função que DECIDE a precedência; recebê-lo é o
#   trabalho dela (o legado é a queda, não a preferência).
# - `_prontos_remotos`: bate em `/api/health`, a única `/api` sem gate no
#   `main.py`. O servidor ignora o `?token=`, então não há credencial a migrar
#   nesse caminho — desligar o legado não muda nada por ali.
DESTINOS_PERMITIDOS = frozenset({"credencial_da_janela", "_prontos_remotos"})


def _nome_do_alvo(chamada: ast.Call) -> str:
    """Nome da função chamada. `threading.Thread(...)` → `Thread`."""
    alvo = chamada.func
    if isinstance(alvo, ast.Name):
        return alvo.id
    if isinstance(alvo, ast.Attribute):
        return alvo.attr
    return "<expressão>"


def _e_o_token_legado(no: ast.AST) -> bool:
    return (
        isinstance(no, ast.Attribute)
        and no.attr == "access_token"
        and isinstance(no.value, ast.Name)
        and no.value.id == "settings"
    )


def chamadas_com_token_legado(fonte: str) -> list[tuple[str, int]]:
    """(função chamada, linha) para cada uso de `settings.access_token` como argumento.

    Atribui o uso à chamada MAIS INTERNA que o envolve — é ela que decide se o
    segredo vai virar credencial de rota gateada. Assim
    `Thread(target=_api, args=(url, ..., settings.access_token))` é cobrado do
    `Thread`, e não escapa por estar dentro de uma tupla dentro de um keyword.
    """
    arvore = ast.parse(fonte)
    pai: dict[ast.AST, ast.AST] = {}
    for no in ast.walk(arvore):
        for filho in ast.iter_child_nodes(no):
            pai[filho] = no

    achados: list[tuple[str, int]] = []
    for no in ast.walk(arvore):
        if not _e_o_token_legado(no):
            continue
        atual = pai.get(no)
        while atual is not None:
            if isinstance(atual, ast.Call):
                achados.append((_nome_do_alvo(atual), no.lineno))
                break
            atual = pai.get(atual)
    return achados


# --------------------------------------------------------------------------- #
# A regra, no app.py de verdade                                                #
# --------------------------------------------------------------------------- #
def test_nenhuma_chamada_do_app_usa_o_token_legado_cru():
    """A trava. Reintroduzir `_api(url, ..., settings.access_token)` falha aqui."""
    proibidas = [
        (alvo, linha)
        for alvo, linha in chamadas_com_token_legado(APP_PY.read_text(encoding="utf-8"))
        if alvo not in DESTINOS_PERMITIDOS
    ]
    assert not proibidas, (
        "chamada(s) passando `settings.access_token` cru em app.py: "
        + ", ".join(f"{alvo}() na linha {linha}" for alvo, linha in proibidas)
        + " — use `_credencial()`, senão MENTE_APARELHOS_TOKEN_LEGADO=false"
        " quebra a bandeja/standby em silêncio."
    )


def test_o_ponto_unico_existe_e_delega_a_regra_de_precedencia():
    """`_credencial()` não pode virar uma segunda cópia da regra: ele DELEGA.

    Duas implementações da precedência divergiriam no primeiro conserto de uma —
    o mesmo motivo pelo qual `aparelhos.avaliar` chama `acesso.cliente_autorizado`
    em vez de reimplementá-la."""
    fonte = ast.parse(APP_PY.read_text(encoding="utf-8"))
    corpo = [
        no for no in fonte.body
        if isinstance(no, ast.FunctionDef) and no.name == "_credencial"
    ]
    assert corpo, "`_credencial()` sumiu — a casca voltou a ter seis credenciais soltas."
    chamadas = {_nome_do_alvo(n) for n in ast.walk(corpo[0]) if isinstance(n, ast.Call)}
    assert "credencial_da_janela" in chamadas


def test_a_credencial_de_aparelho_chega_a_quem_chama(monkeypatch):
    """De ponta a ponta: `_credencial()` entrega o que `credencial_da_janela` decide."""
    from mente_digital.config import settings

    monkeypatch.setattr(settings, "janela_credencial", "mdk1.abc.segredo")
    monkeypatch.setattr(settings, "access_token", "legado")
    assert app._credencial() == "mdk1.abc.segredo"

    monkeypatch.setattr(settings, "janela_credencial", "")
    assert app._credencial() == "legado"


# --------------------------------------------------------------------------- #
# A trava discrimina? (senão ela passaria verde para sempre)                   #
# --------------------------------------------------------------------------- #
def test_a_trava_PEGA_o_padrao_reintroduzido():
    """Sem isto, um erro no casamento de nós deixaria a regra acima vacuamente
    verde — e ela é justamente a que precisa gritar daqui a seis meses."""
    reincidencia = textwrap.dedent("""
        def f(url, settings):
            _api(url, "/api/energia", "ligar", settings.access_token)
    """)
    assert chamadas_com_token_legado(reincidencia) == [("_api", 3)]


def test_a_trava_enxerga_o_token_escondido_dentro_de_uma_tupla():
    """O caso real mais fácil de escapar: o segredo viaja no `args=` de uma
    `Thread`, dois níveis abaixo do argumento."""
    disfarcado = textwrap.dedent("""
        def f(url, settings):
            threading.Thread(
                target=_api, args=(url, "/api/energia", "ligar", settings.access_token),
            ).start()
    """)
    assert chamadas_com_token_legado(disfarcado) == [("Thread", 4)]


def test_a_trava_nao_se_satisfaz_com_docstring_nem_comentario():
    """As docstrings do `app.py` citam `settings.access_token` para explicar a
    regra. Um `grep` acusaria essas linhas e daria a impressão de cobertura."""
    so_prosa = textwrap.dedent('''
        def f():
            """Não passe `settings.access_token` cru — use `_credencial()`."""
            # nem aqui: settings.access_token
            return 1
    ''')
    assert chamadas_com_token_legado(so_prosa) == []
