"""Zonas de redimensionar da janela sem moldura — a matemática pura.

O caso que estes testes existem para impedir de voltar: em 2026-08-05 o dono não
conseguia redimensionar a janela ao desmaximizar. `frameless=True` tira a moldura,
e com ela somem as alças que o Windows desenha na área não-cliente — não havia um
pixel para agarrar. `zona` é o que devolve essas alças, respondendo ao
`WM_NCHITTEST` que os pixels da extremidade são borda, não conteúdo.

Nada aqui toca Win32: são coordenadas entrando e constante saindo, então roda no
CI Linux igual à máquina do dono. O que mexe em janela de verdade
(`redimensionar.instalar`) fica atrás da guarda de plataforma e foi medido ao vivo.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mente_digital import redimensionar
from mente_digital.redimensionar import (
    HTBOTTOM,
    HTBOTTOMLEFT,
    HTBOTTOMRIGHT,
    HTCLIENT,
    HTLEFT,
    HTRIGHT,
    HTTOP,
    HTTOPLEFT,
    HTTOPRIGHT,
    parse_cor,
    zona,
)

L, A = 800, 600          # largura e altura de uma janela de teste
M, C = 6, 16             # margem de borda e alcance de canto


def z(x: int, y: int, largura: int = L, altura: int = A) -> int:
    return zona(x, y, largura, altura, M, C)


# --------------------------------------------------------------------------- #
# O miolo é conteúdo                                                           #
# --------------------------------------------------------------------------- #
def test_o_centro_da_janela_nao_e_borda():
    assert z(L // 2, A // 2) == HTCLIENT


@pytest.mark.parametrize("x,y", [(M, M), (L - M - 1, A - M - 1), (M, A // 2), (L // 2, M)])
def test_um_pixel_para_dentro_da_margem_ja_e_conteudo(x, y):
    """A faixa é FINA de propósito: quem clica no conteúdo não pode pegar borda."""
    assert z(x, y) == HTCLIENT


# --------------------------------------------------------------------------- #
# Os quatro lados                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("x,y,esperado", [
    (0, A // 2, HTLEFT),
    (M - 1, A // 2, HTLEFT),
    (L - 1, A // 2, HTRIGHT),
    (L - M, A // 2, HTRIGHT),
    (L // 2, 0, HTTOP),
    (L // 2, M - 1, HTTOP),
    (L // 2, A - 1, HTBOTTOM),
    (L // 2, A - M, HTBOTTOM),
])
def test_os_quatro_lados(x, y, esperado):
    assert z(x, y) == esperado


# --------------------------------------------------------------------------- #
# Os quatro cantos                                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("x,y,esperado", [
    (0, 0, HTTOPLEFT),
    (L - 1, 0, HTTOPRIGHT),
    (0, A - 1, HTBOTTOMLEFT),
    (L - 1, A - 1, HTBOTTOMRIGHT),
])
def test_as_quatro_pontas(x, y, esperado):
    assert z(x, y) == esperado


@pytest.mark.parametrize("x,y,esperado", [
    # o BRAÇO horizontal do "L" (longe da ponta, ainda canto)
    (C - 1, 1, HTTOPLEFT),
    (L - C, 1, HTTOPRIGHT),
    (C - 1, A - 2, HTBOTTOMLEFT),
    (L - C, A - 2, HTBOTTOMRIGHT),
    # o BRAÇO vertical
    (1, C - 1, HTTOPLEFT),
    (L - 2, C - 1, HTTOPRIGHT),
    (1, A - C, HTBOTTOMLEFT),
    (L - 2, A - C, HTBOTTOMRIGHT),
])
def test_o_braco_do_canto_ainda_e_canto(x, y, esperado):
    """O alvo confortável do canto são os BRAÇOS, não a ponta.

    Isto não é capricho: a região arredondada (`app.arredondar_janela`) recorta
    literalmente a ponta da janela — medido em 2026-08-05, o ponto relativo (1,1)
    cai FORA da janela e o clique vai para o que está atrás. Se o canto valesse
    só o quadradinho `margem × margem`, sobraria uma diagonal de 2 px para
    agarrar. É por isso que `CANTO` > `MARGEM`.
    """
    assert z(x, y) == esperado


def test_passado_o_alcance_do_canto_volta_a_ser_lado():
    """O "L" termina: adiante dele a borda é lado puro, senão esticar só na
    horizontal viraria impossível perto do topo."""
    assert z(1, C) == HTLEFT
    assert z(C, 1) == HTTOP


# --------------------------------------------------------------------------- #
# A barra de arrasto continua arrastando                                       #
# --------------------------------------------------------------------------- #
def test_a_borda_vence_a_regiao_de_arrasto_so_na_faixa_fina():
    """`easy_drag=False` + `.pywebview-drag-region` são deliberados (ver o
    docstring do `app.py`): a barra de título é do front. A borda toma dela
    apenas os `margem` primeiros pixels — o que sobra da barra de 42 px continua
    arrastando a janela. Tomar a barra inteira devolveria outro defeito."""
    meio = L // 2
    assert z(meio, M - 1) == HTTOP        # a faixa fina é borda
    for y in range(M, 42):                # o resto da barra é da página
        assert z(meio, y) == HTCLIENT


# --------------------------------------------------------------------------- #
# Bordas do próprio cálculo                                                    #
# --------------------------------------------------------------------------- #
def test_margem_zero_desliga_tudo():
    """O botão de desligar: sem margem, nenhuma zona — a janela volta ao
    comportamento de hoje em vez de responder qualquer coisa."""
    assert zona(0, 0, L, A, 0, C) == HTCLIENT
    assert zona(0, A // 2, L, A, 0, C) == HTCLIENT


@pytest.mark.parametrize("largura,altura", [(0, 0), (-10, 50), (50, -10), (0, 600)])
def test_tamanho_degenerado_nao_explode(largura, altura):
    """`GetWindowRect` de janela minimizada devolve retângulo vazio ou negativo;
    responder zona ali faria o Windows pedir um resize impossível."""
    assert zona(0, 0, largura, altura, M, C) == HTCLIENT


def test_janela_minuscula_nao_vira_so_borda():
    """Com a margem cheia, uma janela de 12 px seria borda de ponta a ponta e não
    sobraria conteúdo clicável. As margens são limitadas ao tamanho real."""
    assert zona(6, 6, 12, 12, M, C) == HTCLIENT


def test_cantos_opostos_nao_se_sobrepoem_em_janela_estreita():
    """Numa janela estreita os dois cantos do topo se encostam. O que NÃO pode
    acontecer é o mesmo pixel valer pelos dois — aí a resposta dependeria da
    ordem dos `if` e o resize trocaria de lado sozinho. O alcance é limitado a
    metade do menor lado, então eles PARTICIONAM a faixa: metade esquerda para um,
    metade direita para o outro, sem sobra nem sobreposição."""
    largura = 30
    zonas = [zona(x, 1, largura, 400, M, C) for x in range(largura)]
    assert set(zonas) == {HTTOPLEFT, HTTOPRIGHT}
    corte = zonas.index(HTTOPRIGHT)
    assert zonas[:corte] == [HTTOPLEFT] * corte           # nada de ida e volta
    assert corte == largura - corte                       # exatamente ao meio


def test_e_simetrica_nos_quatro_lados():
    """Espelhar o ponto tem de espelhar a zona — assimetria aqui é alça que
    existe de um lado só, exatamente a queixa original."""
    espelho = {HTLEFT: HTRIGHT, HTRIGHT: HTLEFT,
               HTTOPLEFT: HTTOPRIGHT, HTTOPRIGHT: HTTOPLEFT,
               HTBOTTOMLEFT: HTBOTTOMRIGHT, HTBOTTOMRIGHT: HTBOTTOMLEFT,
               HTTOP: HTTOP, HTBOTTOM: HTBOTTOM, HTCLIENT: HTCLIENT}
    for x in range(0, L, 7):
        for y in range(0, A, 11):
            assert z(L - 1 - x, y) == espelho[z(x, y)]
            assert z(x, A - 1 - y) == {HTTOP: HTBOTTOM, HTBOTTOM: HTTOP,
                                       HTTOPLEFT: HTBOTTOMLEFT, HTBOTTOMLEFT: HTTOPLEFT,
                                       HTTOPRIGHT: HTBOTTOMRIGHT, HTBOTTOMRIGHT: HTTOPRIGHT,
                                       HTLEFT: HTLEFT, HTRIGHT: HTRIGHT,
                                       HTCLIENT: HTCLIENT}[z(x, y)]


# --------------------------------------------------------------------------- #
# A cor da faixa: a fronteira entre o front e uma API nativa                   #
# --------------------------------------------------------------------------- #
class TestParseCor:
    """`parse_cor` é onde texto vindo do FRONT vira três inteiros.

    A faixa de 6 px é pintada pelo WinForms e não enxerga o CSS, então quem sabe
    a cor é a página — ela manda o `--bg` do tema vigente. Isso significa que uma
    string de fora chega até uma chamada nativa, e este é o filtro."""

    @pytest.mark.parametrize("texto, esperado", [
        ("#0B0B0D", (11, 11, 13)),          # o escuro do app
        ("#FFFFFF", (255, 255, 255)),       # o claro da página
        ("#F7F7F9", (247, 247, 249)),       # o claro da TELA DE BOOT, que é outro
        ("0B0B0D", (11, 11, 13)),           # sem '#' — o getComputedStyle às vezes poda
        ("  #fff  ", (255, 255, 255)),      # curto + espaço: o CSS devolve com folga
        ("#abc", (170, 187, 204)),
    ])
    def test_aceita_o_que_o_css_devolve(self, texto, esperado):
        assert parse_cor(texto) == esperado

    @pytest.mark.parametrize("texto", [
        "white",                            # nome: o CSS aceita, nós não
        "rgb(255,255,255)",                 # função: idem
        "#GGGGGG",                          # hex inválido
        "#12345",                           # tamanho fora
        "", "   ", "#",
        None, 255, ["#fff"],                # nem string é
    ])
    def test_recusa_o_resto(self, texto):
        """Recusa devolve None, não levanta: `pintar_faixa` é fail-soft e um
        friso da cor errada jamais pode derrubar a janela."""
        assert parse_cor(texto) is None

    def test_fora_do_windows_nao_toca_winforms(self, monkeypatch):
        """O seam é `redimensionar.e_windows`, NUNCA o `os.name` do processo —
        trocar `os.name` num runner POSIX faz todo `Path(...)` levantar e mata a
        sessão do pytest em INTERNALERROR (visto no CI em 2026-08-03)."""
        monkeypatch.setattr(redimensionar, "e_windows", lambda: False)
        assert redimensionar.pintar_faixa(object(), "#FFFFFF") is False


def test_import_nao_toca_wintypes(tmp_path):
    """`ctypes.wintypes` NÃO EXISTE fora do Windows — importá-lo no nível do
    módulo quebraria o CI (ubuntu-latest) sem quebrar nada aqui. Mesma régua de
    `test_janela_unica.py`; roda em subprocesso porque outro teste pode já ter
    importado wintypes neste."""
    prova = tmp_path / "prova_wintypes.py"
    prova.write_text(
        "import sys\n"
        "from mente_digital import redimensionar  # noqa: F401\n"
        "print('VAZOU' if 'ctypes.wintypes' in sys.modules else 'ok')\n",
        encoding="utf-8",
    )
    saida = subprocess.run(
        [sys.executable, str(prova)], capture_output=True, text=True,
        cwd=str(tmp_path.parent), env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert "ok" in saida.stdout, (
        f"importar redimensionar puxou ctypes.wintypes — quebra o CI no Linux. "
        f"stdout={saida.stdout!r} stderr={saida.stderr!r}")
