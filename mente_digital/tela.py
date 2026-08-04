"""A tela está apagada? — o sinal que faltava para a conta dos monitores.

Por que existe: em `tomada.py`, a parcela dos monitores é a MAIOR fonte de incerteza
da estimativa da parede — medido em 2026-08-04, **89 dos 164 W** de largura da faixa
saem só dela, porque o modelo não sabe se estão acesos (0,6 W em standby, 90 W no
talo, e a faixa cobria os dois). O `Cenario.monitores_ligados` sempre existiu e já
sabia usar essa informação; o que faltava era alguém INFORMAR.

O pedido do dono (2026-08-04): "leva isso em consideração na hora de fazer as
estimativas de consumo, se a tela tá desligada ou não". E o contexto tornou isso
valioso: ele passou a apagar as telas em 5 min para poder codar pelo celular com o
PC ligado e a tela apagada — ou seja, o caso "monitor apagado com a máquina
trabalhando" deixou de ser exceção e virou o normal do uso remoto.

⚠ ISTO É INFERÊNCIA, NÃO LEITURA DE SENSOR. O Windows não entrega "o monitor está
energizado?" por uma chamada barata: `WmiMonitorBrightness` não existe em monitor de
mesa (medido nesta máquina) e o estado de HDR/energia por registro não está onde o
Win11 o guarda (esta máquina é Win10 19045). O que dá para saber barato é há quanto
tempo NÃO HÁ ENTRADA (`GetLastInputInfo`, user32 — sem driver, sem GPU, µs) e qual o
timeout configurado. Se o ocioso passou do timeout, o Windows já apagou.

O erro possível é conhecido e assimétrico DE PROPÓSITO: mexer o mouse acende a tela
na hora, então "ocioso < timeout" pode dizer ACESO um instante depois de o dono
apagar a tela pelo botão do monitor — e superestimar consumo é melhor que fingir
economia. O contrário (dizer APAGADO com a tela acesa) não acontece, porque o
Windows só apaga depois do mesmo timeout que a gente compara.

PURO NA DECISÃO: `apagada(ocioso_s, timeout_s)` não toca em nada e é onde a regra
mora. Só `segundos_ocioso()` fala com o Windows, e ela devolve None fora dele —
quem chama trata a ausência como "não sei", que é a faixa larga de hoje.
"""
from __future__ import annotations

import ctypes
import os
from typing import Optional


class _UltimaEntrada(ctypes.Structure):
    """`LASTINPUTINFO` do user32. `cbSize` tem de ser preenchido ANTES da chamada —
    a API valida o tamanho e devolve falso silencioso se ele estiver zerado."""

    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


def _windows() -> bool:
    """Seam de plataforma, como em `inicializacao.e_windows` — e pelo MESMO motivo
    documentado lá: trocar `os.name` no teste envenena o `pathlib` do processo
    inteiro e mata a sessão do pytest num runner POSIX. O teste troca ISTO."""
    return os.name == "nt"


def segundos_ocioso() -> Optional[float]:
    """Há quantos segundos não há entrada de teclado/mouse nesta sessão.

    `None` fora do Windows ou se a API falhar — nunca 0.0, que seria indistinguível
    de "o dono acabou de mexer no mouse" e faria a conta afirmar tela acesa.

    ⚠ É por SESSÃO, e é isso que queremos: quando o dono coda pelo celular, não há
    entrada local nenhuma e o ocioso cresce — que é exatamente a verdade sobre os
    monitores dele, ainda que a máquina esteja trabalhando."""
    if not _windows():
        return None
    try:
        info = _UltimaEntrada()
        info.cbSize = ctypes.sizeof(_UltimaEntrada)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        agora = ctypes.windll.kernel32.GetTickCount64()
        # GetTickCount64 e dwTime estão na MESMA base (ms desde o boot), mas dwTime é
        # 32 bits: passados ~49,7 dias de uptime ele dá a volta e a subtração crua
        # sairia negativa ou gigante. Máscara de 32 bits nos dois lados resolve, e é
        # o motivo de o retorno passar por `max(0.0, ...)`.
        delta_ms = (agora & 0xFFFFFFFF) - (info.dwTime & 0xFFFFFFFF)
        if delta_ms < 0:
            delta_ms += 0x100000000
        return max(0.0, delta_ms / 1000.0)
    except Exception:
        # Fail-soft e silencioso: isto roda a cada amostra de um processo que
        # atravessa o dia. Um log por falha encheria o arquivo, e a consequência de
        # não saber já é segura (a faixa larga).
        return None


def apagada(ocioso_s: Optional[float], timeout_s: Optional[float]) -> Optional[bool]:
    """A tela já apagou? PURA — é aqui que a regra mora.

    `None` quando falta qualquer um dos dois, ou quando o timeout é 0/negativo (que
    no Windows significa "nunca apagar"): sem informação, quem chama mantém a faixa
    standby..aceso e o resultado é o de sempre. Não devolver um palpite é o ponto.
    """
    if ocioso_s is None or timeout_s is None:
        return None
    if timeout_s <= 0:
        return False            # configurado para NUNCA apagar: estão acesos
    return ocioso_s >= timeout_s


def monitores_ligados(timeout_s: Optional[float]) -> Optional[bool]:
    """O que o `Cenario.monitores_ligados` espera: True/False/None.

    Inverte `apagada` porque o campo do modelo pergunta o contrário — e a inversão
    fica AQUI, num lugar só, em vez de em cada chamador. `None` propaga."""
    estado = apagada(segundos_ocioso(), timeout_s)
    return None if estado is None else (not estado)
