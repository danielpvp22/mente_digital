"""
Botão de energia do aplicativo — medir e enxugar o consumo, com números reais.

Por que existe: o app foi feito para ficar ABERTO o dia inteiro (decisão do dono,
2026-08-02). Entre uma conversa e outra, os ~5 GB de VRAM e os ~7 GB de RAM dos
modelos ficam parados segurando a máquina. O `AppContext.liberar_vram` já sabia
descarregar tudo (era o preparo para o OCR); aqui ele vira um botão, com a medição
ANTES e DEPOIS para o dono ver o efeito em vez de acreditar.

⚠ HONESTIDADE DE MEDIÇÃO — o motivo de este módulo reportar DOIS números de RAM:

`working set` é o que o Gerenciador de Tarefas mostra, e o `enxugar()` abaixo o
derruba de imediato ao mandar o Windows devolver as páginas. Mas devolver página
não é liberar memória: o que sai da RAM física vai para o arquivo de paginação e
volta quando for tocado. O número que diz quanta memória o processo AINDA exige do
sistema é o `commit` (PrivateUsage). Reportar só o working set daria uma queda
espetacular e enganosa. Os dois saem juntos, sempre.
"""
from __future__ import annotations

import gc
import os
import sys
from typing import Optional

from mente_digital import vram

_MB = 1024 * 1024


# --------------------------------------------------------------------------- #
# Medição                                                                      #
# --------------------------------------------------------------------------- #
def _handle_do_processo():
    """HANDLE do processo atual, com `restype` DECLARADO.

    Sem o restype, o ctypes assume `c_int` e trunca o pseudo-handle de 64 bits
    (0xFFFF_FFFF_FFFF_FFFF) — a chamada seguinte falha calada e a medição volta
    None. Foi exatamente o que aconteceu no primeiro teste, em 2026-08-02."""
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    return k32, k32.GetCurrentProcess()


def _ram_windows() -> Optional[tuple[int, int]]:
    """(working_set, commit) em bytes via GetProcessMemoryInfo. None se falhar."""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMCEX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        contadores = _PMCEX()
        contadores.cb = ctypes.sizeof(_PMCEX)
        _, handle = _handle_do_processo()
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_PMCEX), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(contadores), contadores.cb):
            return None
        return int(contadores.WorkingSetSize), int(contadores.PrivateUsage)
    except Exception:                              # noqa: BLE001 - medição nunca é fatal
        return None


def _ram_posix() -> Optional[tuple[int, int]]:
    """(working_set≈RSS, commit≈VmData) do /proc — o caminho do container Linux."""
    try:
        campos: dict[str, int] = {}
        with open("/proc/self/status", encoding="utf-8") as fh:
            for linha in fh:
                if linha.startswith(("VmRSS:", "VmData:")):
                    chave, valor = linha.split(":", 1)
                    campos[chave] = int(valor.strip().split()[0]) * 1024
        if "VmRSS" not in campos:
            return None
        return campos["VmRSS"], campos.get("VmData", campos["VmRSS"])
    except Exception:                              # noqa: BLE001
        return None


def medir() -> dict:
    """Fotografia do consumo AGORA. Campo ausente vira None, nunca zero — zero
    seria indistinguível de "medi e não há consumo", que é uma afirmação forte."""
    ram = _ram_windows() if os.name == "nt" else _ram_posix()
    gpu = vram.ler_uso()
    return {
        "ram_mb": round(ram[0] / _MB) if ram else None,
        "ram_commit_mb": round(ram[1] / _MB) if ram else None,
        # `ler_uso` mede o DISPOSITIVO, não o processo: inclui o desktop e qualquer
        # outro programa na GPU. É o número que o dono vê no nvidia-smi, então é o
        # número certo para ele conferir — mas não atribua a queda inteira a nós.
        "vram_mb": round(gpu["usado"] / _MB) if gpu else None,
        "vram_total_mb": round(gpu["total"] / _MB) if gpu else None,
    }


def delta(antes: dict, depois: dict) -> dict:
    """Quanto caiu em cada métrica. None de qualquer lado propaga None."""
    saida = {}
    for chave in ("ram_mb", "ram_commit_mb", "vram_mb"):
        a, d = antes.get(chave), depois.get(chave)
        saida[chave] = (a - d) if (a is not None and d is not None) else None
    return saida


# --------------------------------------------------------------------------- #
# Enxugar                                                                      #
# --------------------------------------------------------------------------- #
def enxugar() -> None:
    """Devolve ao sistema o que dá, DEPOIS de os modelos já terem sido soltos.

    Três passos, do mais real ao mais cosmético:

    1. `gc.collect()` — os ciclos que seguram tensores já descarregados. Roda
       duas vezes de propósito: a primeira pode ressuscitar objetos com
       `__del__`, que só somem no ciclo seguinte.
    2. `vram.liberar_cache_gpu()` — devolve o cache do alocador do torch. Sem
       isto a VRAM fica "usada" mesmo com o modelo fora, porque o torch guarda
       os blocos para reusar.
    3. Working-set trim (só Windows) — pede ao SO para tirar as páginas frias da
       RAM física. É o passo que faz o Gerenciador de Tarefas despencar, e o
       menos real dos três: as páginas vão para o arquivo de paginação e voltam
       quando tocadas. Legítimo para um app que fica horas ocioso; mentiroso se
       reportado sozinho — por isso `medir()` sempre devolve o commit junto.
    """
    gc.collect()
    gc.collect()
    vram.liberar_cache_gpu()
    if os.name == "nt":
        try:
            import ctypes

            k32, handle = _handle_do_processo()
            k32.SetProcessWorkingSetSize.argtypes = [
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
            k32.SetProcessWorkingSetSize.restype = ctypes.c_bool
            # (-1, -1) = "escolha você, SO": o valor documentado para pedir o trim
            # sem fixar limites rígidos de working set no processo.
            k32.SetProcessWorkingSetSize(handle, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
        except Exception:                          # noqa: BLE001 - melhor esforço
            pass
    elif sys.platform.startswith("linux"):
        try:
            import ctypes

            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:                          # noqa: BLE001
            pass
