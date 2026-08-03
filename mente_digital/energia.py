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

⚠ A MESMA RESSALVA VALE PARA OS WATTS, e é mais fácil de errar: `gpu_watts` é a
potência do DISPOSITIVO, não deste processo. Numa máquina de trabalho há dezenas
de programas com contexto na GPU (medido aqui em 2026-08-03: 22, entre o
compositor do Windows, o navegador e um reprodutor de música), e todos entram no
número — ler os ~96 W de repouso como "o assistente gasta 96 W parado" é
simplesmente falso. Quem exibe tem de dizer isso (ver o tooltip do index.html).
`cpu_watts` é o mesmo caso, um degrau pior: é o pacote INTEIRO da CPU.
"""
from __future__ import annotations

import gc
import os
import sys
from pathlib import Path
from typing import Optional

from mente_digital import potencia, potencia_cpu, vram

_MB = 1024 * 1024

# O aviso de "ajudante publicando lixo" sai UMA vez. `medir()` roda a cada 20 s
# (o laço do front) e a cada clique no botão de energia: repetir a mesma linha
# para sempre afogaria o log em vez de informar.
_avisou_cpu = False


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


def _watt_da_cpu() -> potencia_cpu.Leitura:
    """O que o ajudante elevado publicou. Ajudante fora do ar é ESTADO NORMAL
    (R1: o servidor não roda elevado, então ele pode simplesmente não existir) —
    por isso "ausente" e "vencido" são silenciosos. O que merece log é o arquivo
    EXISTIR e não prestar: aí há algo instalado e quebrado, e o dono precisa saber
    sem ter de reencenar a instalação inteira."""
    global _avisou_cpu
    try:
        from mente_digital.config import settings

        leitura = potencia_cpu.ler_publicacao(
            Path(settings.potencia_cpu_arquivo) if settings.potencia_cpu_arquivo else None,
            settings.potencia_cpu_validade_seconds,
        )
    except Exception:                              # noqa: BLE001 - medição nunca é fatal
        return potencia_cpu.Leitura(None, None, "ausente")
    if leitura.motivo == "invalido" and not _avisou_cpu:
        _avisou_cpu = True                         # uma vez: isto roda a cada 20 s
        try:
            from mente_digital.telemetry import telemetry

            telemetry.warn("POTENCIA", "O arquivo do ajudante de watts existe mas está "
                                       "inválido — o watt da CPU sai como None.")
        except Exception:                          # noqa: BLE001
            pass
    return leitura


def somar_watts(gpu: Optional[float], cpu: Optional[float]) -> Optional[float]:
    """O total GPU + CPU, ou None se faltar QUALQUER uma das duas parcelas.

    Puro/testável, e a regra é o oposto de "some o que tiver": um total precisa de
    todas as parcelas. Com o ajudante elevado fora do ar (o estado normal — ler a
    potência da CPU no Windows exige driver), somar só a GPU e chamar de "consumo
    total" reportaria ~96 W onde a máquina gasta ~240 W. Seria pior que não medir,
    porque um número plausível não levanta suspeita nenhuma — é a mesma razão pela
    qual `medir()` nunca escreve `0 W` para um campo ausente.

    Quando falta parcela, o front mostra a parcela que TEM, rotulada como tal.
    """
    if gpu is None or cpu is None:
        return None
    return round(gpu + cpu, 1)


def medir() -> dict:
    """Fotografia do consumo AGORA. Campo ausente vira None, nunca zero — zero
    seria indistinguível de "medi e não há consumo", que é uma afirmação forte.

    Vale em dobro para os watts: sem NVML (ou sem o ajudante elevado da CPU) o
    campo some, e "0 W" nunca é escrito."""
    ram = _ram_windows() if os.name == "nt" else _ram_posix()
    gpu = vram.ler_uso()
    # A leitura de watts da GPU é MÉDIA POR INTEGRAL desde a chamada anterior a
    # esta — logo, quem chama `medir()` duas vezes coladas (a rota de energia faz
    # isso, antes/depois de soltar os modelos) recebe o instante, não uma janela
    # de 0,1 s dividida por quase nada. Ver potencia.media_integral.
    watt_gpu = potencia.medida_gpu()
    watt_cpu = _watt_da_cpu()
    limite = potencia.limite_gpu()
    return {
        "ram_mb": round(ram[0] / _MB) if ram else None,
        "ram_commit_mb": round(ram[1] / _MB) if ram else None,
        # `ler_uso` mede o DISPOSITIVO, não o processo: inclui o desktop e qualquer
        # outro programa na GPU. É o número que o dono vê no nvidia-smi, então é o
        # número certo para ele conferir — mas não atribua a queda inteira a nós.
        "vram_mb": round(gpu["usado"] / _MB) if gpu else None,
        "vram_total_mb": round(gpu["total"] / _MB) if gpu else None,
        # Watts: DISPOSITIVO inteiro, não o processo (ver o cabeçalho do módulo).
        # A origem viaja junto porque "média de 20 s" e "instante" são números
        # diferentes sobre a mesma placa, e sem o rótulo não dá para saber qual.
        "gpu_watts": round(watt_gpu.watts, 1) if watt_gpu.watts is not None else None,
        "gpu_watts_origem": watt_gpu.origem,
        # O teto imposto pelo driver — é ele que dá ESCALA ao watt: 96 W sozinho
        # não diz se a placa está parada ou apertada; 96 de 320 diz.
        "gpu_watts_limite": round(limite) if limite is not None else None,
        "cpu_watts": round(watt_cpu.watts, 1) if watt_cpu.watts is not None else None,
        "cpu_watts_origem": watt_cpu.origem,
        # Por que o motivo sobe até a API: "o ajudante não está de pé" e "ele está
        # de pé publicando lixo" aparecem os dois como um campo vazio na tela, e
        # têm consertos diferentes. O campo é o que responde "por quê" sem reencenar.
        "cpu_watts_motivo": watt_cpu.motivo,
        # O que o dono pediu ver: GPU + CPU num número só. None enquanto faltar uma
        # das parcelas — ver `somar_watts` para o porquê de não somar pela metade.
        "watts_total": somar_watts(
            watt_gpu.watts if watt_gpu.watts is not None else None,
            watt_cpu.watts if watt_cpu.watts is not None else None,
        ),
    }


def delta(antes: dict, depois: dict) -> dict:
    """Quanto caiu em cada métrica. None de qualquer lado propaga None."""
    saida = {}
    for chave in ("ram_mb", "ram_commit_mb", "vram_mb"):
        a, d = antes.get(chave), depois.get(chave)
        saida[chave] = (a - d) if (a is not None and d is not None) else None
    return saida


def resumo(d: dict) -> str:
    """O `delta` em uma frase, para o log do watcher que dorme sozinho.

    Só entra o que CAIU: métrica ausente (sem CUDA, sem /proc) ou que subiu no
    intervalo fica de fora em vez de aparecer como "-0,1 GB" — o dono lê esta
    linha para conferir que a economia aconteceu, e ruído negativo faz duvidar de
    uma medição que está certa. Nada mensurável devolve "nada mensurável", que é
    uma afirmação honesta e diferente de "0 GB". Puro/testável."""
    partes = []
    for chave, rotulo in (("vram_mb", "VRAM"), ("ram_commit_mb", "RAM")):
        valor = d.get(chave)
        if valor is not None and valor > 0:
            partes.append(f"{valor / 1024:.1f} GB de {rotulo}".replace(".", ","))
    return " e ".join(partes) if partes else "nada mensurável"


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
