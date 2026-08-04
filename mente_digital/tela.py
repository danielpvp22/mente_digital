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

DUAS FONTES, NESTA ORDEM: primeiro a LEITURA (`acesa()`, o estado que o próprio
Windows publica), e só na falta dela a INFERÊNCIA por tempo ocioso (`apagada()`).
A inferência continua aqui porque a leitura pode não existir — fora do Windows, ou
se o registro da notificação falhar — e "não sei" tem de degradar para o palpite
seguro, não para nada.

⚠ POR QUE A INFERÊNCIA SOZINHA NÃO BASTA — medido em 2026-08-04, e é o motivo de
este módulo ter mudado. A 1ª versão comparava ocioso vs. timeout e AFIRMAVA aqui
que o erro só acontecia para o lado seguro: "o contrário (dizer APAGADO com a tela
acesa) não acontece, porque o Windows só apaga depois do mesmo timeout que a gente
compara". A premissa era "só o timeout apaga a tela" — e ela é FALSA. Qualquer
processo pode segurar uma *power request* de DISPLAY (`powercfg /requests`), e ela
não disputa com o timeout: SUSPENDE o contador de ocioso inteiro. O dono saiu de
casa, voltou horas depois e achou os dois monitores acesos com o timeout em 5 min,
porque o app do Claude Desktop segurava um DISPLAY com o motivo "Capturing". Nessa
janela o módulo afirmou APAGADA o tempo todo, subcontando 35,4 a 88,0 W (0,074 a
0,185 kWh em ~2,1 h) — exatamente o erro que o cabeçalho jurava ser impossível, e
na direção que corrompe a conta. Inferência não vê power request; leitura vê.

A LEITURA é `GUID_CONSOLE_DISPLAY_STATE` via `PowerSettingRegisterNotification` com
`DEVICE_NOTIFY_CALLBACK` (powrprof). Provado nesta máquina antes de entrar aqui:
registra num processo de CONSOLE — sem HWND e sem message loop, que é o que a
tornava viável no plantão —, sem admin, e o registro entrega o valor ATUAL, não só
as mudanças. Isso importa: se ele só avisasse em mudança, a largada ficaria cega e
um plantão que sobe com a tela já apagada nunca saberia. A entrega é ASSÍNCRONA
(~11,5 ms), e é por isso que `_registrar_leitura` espera por ela — ver o ⚠ lá.

`WmiMonitorBrightness` segue fora (não existe em monitor de mesa — medido), e é por
isso que a leitura é do estado do CONSOLE, não do painel.

PURO NA DECISÃO: `apagada(ocioso_s, timeout_s)` não toca em nada e é onde a regra da
inferência mora. Quem fala com o Windows é `segundos_ocioso()` e `acesa()`, e as
duas devolvem None fora dele — quem chama trata a ausência como "não sei", que é a
faixa larga de sempre.
"""
from __future__ import annotations

import ctypes
import os
import time
from typing import Optional

#: Teto da espera pelo primeiro valor no registro (medido: chega em ~11,5 ms). É
#: generoso de propósito — acontece UMA vez por processo, e estourá-lo não quebra
#: nada: só faz a primeira amostra cair na inferência.
_ESPERA_INICIAL_S = 0.5


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


# --------------------------------------------------------------------------- #
# A LEITURA — o estado que o Windows publica, não o que a gente deduz.          #
# --------------------------------------------------------------------------- #
#: `GUID_CONSOLE_DISPLAY_STATE` — {6FE69556-704A-47A0-8F24-C28D936FDA47}. Escrito
#: campo a campo em vez de `CLSIDFromString` para não depender do ole32 por um valor
#: que é constante.
_GUID_DISPLAY = (0x6FE69556, 0x704A, 0x47A0,
                 (0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))
_PBT_POWERSETTINGCHANGE = 0x8013
_DEVICE_NOTIFY_CALLBACK = 0x00000002

#: Estado mais recente publicado pelo Windows: 0=apagada, 1=acesa, 2=esmaecida.
#: `None` enquanto não houver registro vivo.
_estado: Optional[int] = None
#: O callback e os parâmetros PRECISAM sobreviver ao fim da função que registra: o
#: Windows guarda o ponteiro e chama depois. Se o GC os levasse, a próxima mudança
#: de estado da tela executaria memória liberada — trava o processo, não levanta
#: exceção. São globais por isso, não por conveniência.
_vivos: list = []
#: Registro é uma vez só. Sem esta marca, uma falha (fora do Windows, powrprof
#: ausente) seria retentada a cada amostra — o dia inteiro, a cada 20 s.
_tentado = False


def _registrar_leitura() -> bool:
    """Assina a notificação de estado da tela. Idempotente; True se há leitura viva.

    Todo o ctypes específico do Windows mora AQUI DENTRO de propósito: `WINFUNCTYPE`
    não existe fora do Windows, e uma referência a ele no corpo do módulo quebraria
    o import no CI Linux. É a mesma armadilha que `ctypes.wintypes` já causou neste
    projeto — módulo que importa bem no Windows e explode no runner."""
    global _estado, _tentado
    if _tentado:
        return bool(_vivos)
    _tentado = True
    if not _windows():
        return False
    try:
        class _Guid(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        class _Aviso(ctypes.Structure):
            """`POWERBROADCAST_SETTING`. `Data` é declarado com 4 bytes porque o dado
            desta GUID é um DWORD; `DataLength` ainda é conferido antes de ler."""
            _fields_ = [("PowerSetting", _Guid), ("DataLength", ctypes.c_ulong),
                        ("Data", ctypes.c_ubyte * 4)]

        assinatura = ctypes.WINFUNCTYPE(
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)

        class _Parametros(ctypes.Structure):
            _fields_ = [("Callback", assinatura), ("Context", ctypes.c_void_p)]

        def _mudou(_contexto, tipo, aviso):
            # Roda numa thread do pool do Windows, não na nossa. O que ele faz é
            # atribuir um int a um global — atômico sob o GIL, então não há lock.
            global _estado
            try:
                if tipo == _PBT_POWERSETTINGCHANGE and aviso:
                    s = ctypes.cast(aviso, ctypes.POINTER(_Aviso)).contents
                    if 0 < s.DataLength <= 4:
                        _estado = int.from_bytes(
                            bytes(s.Data[:s.DataLength]), "little")
            except Exception:
                pass        # nunca deixar uma exceção atravessar de volta para o Win32
            return 0

        cb = assinatura(_mudou)
        params = _Parametros()
        params.Callback = cb
        params.Context = None

        guid = _Guid()
        guid.Data1, guid.Data2, guid.Data3 = _GUID_DISPLAY[:3]
        guid.Data4 = (ctypes.c_ubyte * 8)(*_GUID_DISPLAY[3])

        handle = ctypes.c_void_p()
        rc = ctypes.windll.powrprof.PowerSettingRegisterNotification(
            ctypes.byref(guid), _DEVICE_NOTIFY_CALLBACK,
            ctypes.byref(params), ctypes.byref(handle))
        if rc != 0:                      # ERROR_SUCCESS
            return False
        _vivos.extend((cb, params, handle))
        # ⚠ O registro entrega o estado atual, mas ASSÍNCRONO — numa thread do pool,
        # ~11,5 ms depois de a chamada acima retornar (medido com `perf_counter`).
        # Ler `_estado` aqui sem esperar devolveria None e a PRIMEIRA amostra do
        # processo cairia na inferência, que é justamente a fonte com o defeito.
        # A espera é uma vez por processo, e o `sleep` solta o GIL — que é o que
        # permite ao callback rodar.
        #
        # A primeira medição disto disse "0,0 ms" e estava errada: era
        # `time.monotonic()`, cuja resolução no Windows é ~15 ms, arredondando os
        # 11,5 para zero. Ficou escrito porque um relógio grosso não avisa que é
        # grosso — ele só concorda com a hipótese.
        limite = time.monotonic() + _ESPERA_INICIAL_S
        while _estado is None and time.monotonic() < limite:
            time.sleep(0.002)
        return True
    except Exception:
        # Mesmo fail-soft de `segundos_ocioso`: sem leitura, a inferência assume.
        return False


def acesa() -> Optional[bool]:
    """A tela está acesa, segundo o Windows? `None` quando não há leitura.

    ESMAECIDA (2) conta como ACESA: o painel continua energizado e puxando quase o
    mesmo, e na dúvida este módulo superestima. Só o 0 é apagada."""
    if not _registrar_leitura():
        return None
    estado = _estado
    return None if estado is None else (estado != 0)


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

    ORDEM DAS FONTES, e ela é a correção de 2026-08-04: a LEITURA vem primeiro e,
    quando existe, a inferência nem é consultada. Só na ausência dela (fora do
    Windows, powrprof indisponível, registro recusado) cai para o ocioso vs.
    timeout, que é o comportamento antigo com o defeito antigo — melhor que nada,
    e é por isso que continua aqui.

    Inverte `apagada` porque o campo do modelo pergunta o contrário — e a inversão
    fica AQUI, num lugar só, em vez de em cada chamador. `None` propaga."""
    lido = acesa()
    if lido is not None:
        return lido
    estado = apagada(segundos_ocioso(), timeout_s)
    return None if estado is None else (not estado)
