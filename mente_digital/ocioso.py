"""
Idle automático por INATIVIDADE DO USUÁRIO — não por inatividade do chat.

O pedido do dono (2026-08-02): deixar o app aberto o dia inteiro, inclusive
jogando. Quando ele para de mexer no teclado e no mouse, o app pergunta se pode
consolidar; sem resposta em 15 s, consolida. Quando ele VOLTA, o trabalho para e
devolve o que ocupou — o app segue pronto, não desliga.

Duas decisões de desenho que valem o arquivo:

1. **O ócio é medido no SISTEMA, não na janela.** Um detector em JavaScript só
   enxerga eventos dentro da própria janela, então jogar em tela cheia pareceria
   exatamente igual a estar longe do teclado — e o idle roubaria a GPU no meio da
   partida, que é o oposto do que a função existe para fazer. O
   `GetLastInputInfo` do Windows responde "há quanto tempo NINGUÉM tocou em nada
   nesta máquina", contando input de qualquer aplicativo.

2. **A máquina de estados é pura.** Ela não lê relógio, não chama Windows e não
   dispara nada: recebe (segundos sem input, houve resposta?, agora) e devolve o
   próximo estado. É o que torna testável um comportamento cujo bug natural é
   temporal — "perguntou duas vezes", "nunca mais perguntou", "rodou mesmo o dono
   tendo dito não".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Estado(str, Enum):
    ATIVO = "ativo"              # o dono está usando a máquina
    PERGUNTANDO = "perguntando"  # ociosidade detectada, aguardando sim/não
    RODANDO = "rodando"          # o idle está consolidando
    RECUSADO = "recusado"        # ele disse não; só volta a perguntar após voltar e sair de novo


@dataclass
class MaquinaOciosa:
    """Decide QUANDO perguntar, rodar e parar. Pura: sem relógio, sem I/O.

    `segundos_para_perguntar` conta do último input. `segundos_de_resposta` é a
    janela de 15 s do dono: passou sem sim nem não, roda — o silêncio de quem não
    está lá é consentimento; o de quem está vira um "não" com um clique.
    """

    segundos_para_perguntar: float = 300.0
    segundos_de_resposta: float = 15.0
    estado: Estado = Estado.ATIVO
    perguntou_em: Optional[float] = None
    # Guarda o quanto de ócio havia quando ele recusou. Só perguntamos de novo
    # depois que ele VOLTAR (ócio zera) e ficar ocioso outra vez — senão a recusa
    # viraria uma pergunta a cada tique, que é assédio, não automação.
    recusou_com: Optional[float] = None
    _acoes: list = field(default_factory=list, repr=False)

    def tick(self, sem_input: float, agora: float) -> Optional[str]:
        """Um passo. Devolve a AÇÃO a executar, ou None.

        Ações: "perguntar", "rodar", "parar". Quem executa é o chamador — assim
        esta classe nunca precisa de mock para ser testada.
        """
        voltou = sem_input < self.segundos_para_perguntar

        if voltou:
            # O dono está de volta ao teclado. Se havia trabalho rodando, ele para
            # AGORA e devolve o que pegou. Este é o único caminho que emite "parar".
            anterior, self.estado = self.estado, Estado.ATIVO
            self.perguntou_em = None
            self.recusou_com = None
            return "parar" if anterior == Estado.RODANDO else None

        if self.estado == Estado.ATIVO:
            self.estado = Estado.PERGUNTANDO
            self.perguntou_em = agora
            return "perguntar"

        if self.estado == Estado.PERGUNTANDO:
            if agora - (self.perguntou_em or agora) >= self.segundos_de_resposta:
                self.estado = Estado.RODANDO
                return "rodar"
            return None

        return None       # RODANDO e RECUSADO só saem daqui quando ele volta

    def responder(self, sim: bool, agora: float) -> Optional[str]:
        """O dono clicou. Só vale enquanto a pergunta está no ar — um clique que
        chega depois do timeout não pode desfazer o que já começou por conta
        própria (ele veria o trabalho já rodando e usaria o botão de parar)."""
        if self.estado != Estado.PERGUNTANDO:
            return None
        if sim:
            self.estado = Estado.RODANDO
            return "rodar"
        self.estado = Estado.RECUSADO
        self.recusou_com = agora
        return None


# --------------------------------------------------------------------------- #
# Leitura do ócio no sistema                                                   #
# --------------------------------------------------------------------------- #
def segundos_sem_input() -> Optional[float]:
    """Há quanto tempo ninguém toca teclado/mouse NESTA MÁQUINA. None fora do
    Windows ou se a API falhar — e None significa "não sei", nunca "está ocioso":
    quem chama deve tratar a ignorância como presença, não como ausência."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
        user32.GetLastInputInfo.restype = wintypes.BOOL
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        # GetTickCount64 e dwTime têm bases diferentes de largura: dwTime é 32 bits
        # e dá a volta a cada ~49,7 dias. Mascarar os 32 bits baixos do tick de 64
        # faz a subtração continuar certa depois da volta.
        agora32 = k32.GetTickCount64() & 0xFFFFFFFF
        delta = (agora32 - info.dwTime) & 0xFFFFFFFF
        return delta / 1000.0
    except Exception:                              # noqa: BLE001 - nunca fatal
        return None
