"""
Potência ELÉTRICA da GPU, lida da NVML por ctypes puro.

Por que existe: `energia.py` já responde "quanta memória isto está segurando";
faltava "quanto isto está CUSTANDO agora". A resposta estava a uma DLL de
distância — `nvml.dll` vem com o driver NVIDIA e já está no sistema, então este
módulo não adiciona dependência nenhuma (medido nesta máquina em 2026-08-03:
`nvmlDeviceGetPowerUsage` custa p50 **0,0007 ms**; o `subprocess nvidia-smi` que
seria a alternativa óbvia custa **31 ms** — 44 mil vezes mais caro para o mesmo
número, e a leitura roda a cada 20 s numa rota que o celular também chama).

⚠ NÃO use `torch.cuda.power_draw` para isto. O símbolo EXISTE nesta env e falha
ao ser chamado (`ModuleNotFoundError: pynvml`) — medido no mesmo dia. Símbolo
existir não prova que funciona, e o caminho do torch ainda arrastaria o torch
para dentro de uma leitura que o ctypes faz sozinho.

⚠ HONESTIDADE DE MEDIÇÃO — a razão de a média sair por INTEGRAL de energia:

`nvmlDeviceGetPowerUsage` devolve o INSTANTE. Numa GPU que alterna entre 96 W
parada e 320 W decodificando, o instante sorteado a cada 20 s conta uma história
que depende de quando a régua caiu — duas leituras seguidas podem dizer 96 e 318
sem nada ter mudado no trabalho. `nvmlDeviceGetTotalEnergyConsumption` devolve
JOULES ACUMULADOS desde que o driver subiu; a diferença entre duas leituras
dividida pelo tempo entre elas é a média REAL da janela inteira, e é isso que o
front pede a cada 20 s. Na 1ª chamada não há janela — aí, e só aí, vale o
instante (reportado com origem diferente, para o número não mentir sobre o que é).

⚠ E a honestidade tem preço, medido aqui em 2026-08-03: o contador de energia
custa **p50 1,04 ms**, contra **0,001 ms** da leitura instantânea — mil vezes
mais. A cada 20 s isso é irrelevante (0,005% do tempo), e é a troca certa; num
laço quente NÃO seria. Se alguém um dia quiser watts por turno, meça de novo
antes de chamar isto de dentro do pipeline de resposta.

Igual ao `vram.py`: o ctypes é o ÚNICO ponto de IO; a decisão (integral, janela
curta demais, contador reiniciado) é pura e testável com relógio INJETADO.
"""
from __future__ import annotations

import ctypes
import os
import threading
from dataclasses import dataclass
from typing import Optional

# A NVML fala em MILIjoules e MILIwatts. O erro de fator 1000 aqui não estoura
# nada — só imprime "320000 W" na faixa do app —, então a constante fica nomeada.
_MILI = 1000.0

# Janela mínima para confiar na integral. O contador de energia da NVML é
# amostrado internamente pelo driver: numa janela de milissegundos ele pode não
# ter avançado NENHUM milijoule, e a divisão devolveria 0 W. Zero é exatamente a
# afirmação que este projeto não pode fazer por engano — "medi e não há consumo"
# é forte e seria falso (é a mesma regra que `energia.medir` já segue ao devolver
# None em vez de 0). Abaixo desta janela, cai para o instante.
#
# Importa na prática: a rota `/api/energia` chama `medir()` DUAS vezes seguidas
# (antes/depois de soltar os modelos), com décimos de segundo entre elas.
JANELA_MINIMA_S = 1.0

# Teto de sanidade. Uma placa de consumo doméstico não passa disto; um valor
# acima significa unidade errada ou lixo, não uma GPU de 5 kW.
_WATTS_ABSURDO = 5000.0

_trava = threading.Lock()
_lib: Optional[ctypes.CDLL] = None
_dispositivo: Optional[ctypes.c_void_p] = None
_tentou_carregar = False        # já pagamos o nvmlInit_v2 (8,2 ms medidos) ou já falhou
_avisou_falha = False           # o aviso de "não consigo ler" sai UMA vez, não a cada 20 s
_baseline: Optional[tuple[int, float]] = None   # (milijoules, instante) da última integral


@dataclass(frozen=True)
class Leitura:
    """Um watt com a PROCEDÊNCIA junto.

    A origem não é enfeite: "média de 20 s" e "instante sorteado" são números
    diferentes sobre a mesma placa, e quem lê a faixa do app precisa poder saber
    qual dos dois está vendo antes de tirar conclusão."""

    watts: Optional[float]
    origem: Optional[str]       # "nvml_integral" | "nvml_instantaneo" | None


@dataclass(frozen=True)
class Integral:
    """O resultado de comparar a leitura de energia com a anterior. Puro."""

    watts: Optional[float]                  # None = não dá para afirmar por integral
    baseline: Optional[tuple[int, float]]   # o que guardar para a próxima chamada


# --------------------------------------------------------------------------- #
# Decisão PURA (testável sem GPU, com relógio injetado)                        #
# --------------------------------------------------------------------------- #
def media_integral(
    baseline: Optional[tuple[int, float]],
    milijoules: int,
    agora: float,
    janela_minima: float = JANELA_MINIMA_S,
) -> Integral:
    """Watts médios entre a leitura anterior e esta. Puro — `agora` é injetado,
    como o `agora` do `agenda.parse_quando`.

    Quatro casos, e os três primeiros devolvem `watts=None` de propósito (quem
    chama cai para o instante em vez de inventar um número):

    1. sem baseline (1ª chamada) — não existe janela ainda; guarda e devolve None.
    2. contador ANDOU PARA TRÁS — o driver reiniciou e o acumulador zerou. A
       subtração daria um negativo enorme; rebaseia e devolve None.
    3. janela curta demais — ver `JANELA_MINIMA_S`. Aqui o baseline fica INTACTO
       de propósito: descartá-lo faria a chamada de 0,1 s comer a janela de 20 s
       que estava sendo formada, e a próxima leitura honesta nunca chegaria.
    4. janela válida — (ΔmJ / 1000) / Δt = watts médios do período.
    """
    if baseline is None:
        return Integral(None, (milijoules, agora))

    mj_antes, t_antes = baseline
    if milijoules < mj_antes:
        return Integral(None, (milijoules, agora))

    janela = agora - t_antes
    if janela < janela_minima:
        return Integral(None, baseline)

    watts = (milijoules - mj_antes) / _MILI / janela
    if watts < 0 or watts > _WATTS_ABSURDO:
        # Não confia e não propaga: rebaseia para a próxima janela ser limpa.
        return Integral(None, (milijoules, agora))
    return Integral(watts, (milijoules, agora))


# --------------------------------------------------------------------------- #
# IO — ctypes sobre a NVML                                                     #
# --------------------------------------------------------------------------- #
def _avisar_uma_vez(mensagem: str) -> None:
    """Loga a falha UMA vez. O projeto proíbe engolir erro em silêncio, mas esta
    leitura roda a cada 20 s: numa máquina sem NVIDIA, `telemetry.error` a cada
    passada encheria o log com a mesma linha para sempre e afogaria o que
    importa. Uma vez diz o que precisa ser dito; a repetição não acrescenta."""
    global _avisou_falha
    if _avisou_falha:
        return
    _avisou_falha = True
    try:
        # Import TARDIO: `telemetry` abre o SQLite no import, e este módulo tem de
        # ser importável sozinho (é o que o teste de "sem DLL" faz).
        from mente_digital.telemetry import telemetry

        telemetry.warn("POTENCIA", f"{mensagem} — o watt da GPU sai como None (o app segue).")
    except Exception:      # noqa: BLE001 - avisar nunca pode derrubar quem mede
        pass


def _abrir_nvml() -> Optional[ctypes.CDLL]:
    """Carrega a NVML e paga o `nvmlInit_v2` UMA vez (8,21 ms medidos).

    Sem instalar nada: no Windows a `nvml.dll` vem no System32 junto do driver;
    no Linux é a `libnvidia-ml.so.1` do pacote do driver."""
    global _lib, _dispositivo, _tentou_carregar
    if _tentou_carregar:
        return _lib
    _tentou_carregar = True
    nomes = ("nvml.dll",) if os.name == "nt" else ("libnvidia-ml.so.1", "libnvidia-ml.so")
    for nome in nomes:
        try:
            lib = ctypes.CDLL(nome)
        except OSError:
            continue
        try:
            lib.nvmlInit_v2.restype = ctypes.c_int
            if lib.nvmlInit_v2() != 0:
                continue
            # ⚠ `nvmlDevice_t` é um PONTEIRO. Sem declarar argtypes/restype o
            # ctypes assume `c_int` e TRUNCA o handle de 64 bits — foi assim que a
            # medição de RAM do `energia.py` voltou None calada em 2026-08-02.
            lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [
                ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
            lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
            handle = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
                continue
            for fn, saida in (
                ("nvmlDeviceGetPowerUsage", ctypes.POINTER(ctypes.c_uint)),
                ("nvmlDeviceGetEnforcedPowerLimit", ctypes.POINTER(ctypes.c_uint)),
                ("nvmlDeviceGetTotalEnergyConsumption", ctypes.POINTER(ctypes.c_ulonglong)),
            ):
                metodo = getattr(lib, fn, None)
                if metodo is not None:
                    metodo.argtypes = [ctypes.c_void_p, saida]
                    metodo.restype = ctypes.c_int
            _lib, _dispositivo = lib, handle
            return lib
        except Exception:                      # noqa: BLE001 - DLL de outra versão
            continue
    _avisar_uma_vez("NVML indisponível (sem driver NVIDIA?)")
    return None


def _ler_uint(nome: str) -> Optional[int]:
    """Chama uma função NVML de saída `unsigned int` sobre o device 0."""
    lib = _abrir_nvml()
    if lib is None:
        return None
    metodo = getattr(lib, nome, None)
    if metodo is None:
        return None                            # driver antigo: função não existe
    try:
        saida = ctypes.c_uint()
        if metodo(_dispositivo, ctypes.byref(saida)) != 0:
            return None                        # NOT_SUPPORTED etc. — não é erro nosso
        return int(saida.value)
    except Exception:                          # noqa: BLE001
        _avisar_uma_vez(f"chamada NVML {nome} falhou")
        return None


def _ler_energia_mj() -> Optional[int]:
    """Joules acumulados (em MILIjoules) desde que o driver subiu. None em placa
    anterior a Volta, onde a NVML devolve NOT_SUPPORTED para este contador."""
    lib = _abrir_nvml()
    if lib is None:
        return None
    metodo = getattr(lib, "nvmlDeviceGetTotalEnergyConsumption", None)
    if metodo is None:
        return None
    try:
        saida = ctypes.c_ulonglong()
        if metodo(_dispositivo, ctypes.byref(saida)) != 0:
            return None
        return int(saida.value)
    except Exception:                          # noqa: BLE001
        _avisar_uma_vez("contador de energia da NVML falhou")
        return None


# --------------------------------------------------------------------------- #
# API pública                                                                  #
# --------------------------------------------------------------------------- #
def medida_gpu(agora=None) -> Leitura:
    """Watts da GPU com a origem junto. Fail-soft: `Leitura(None, None)`.

    ⚠ O número é do DISPOSITIVO, não deste processo — a mesma ressalva que
    `energia.medir` já documenta para a VRAM. Numa máquina de trabalho há dezenas
    de programas com contexto na GPU (medido nesta: 22, entre dwm, navegador e
    reprodutor de música), e todos entram nestes watts."""
    relogio = agora or _monotonico
    instante = float(relogio())
    milijoules = _ler_energia_mj()
    if milijoules is not None:
        with _trava:
            global _baseline
            resultado = media_integral(_baseline, milijoules, instante)
            _baseline = resultado.baseline
        if resultado.watts is not None:
            return Leitura(resultado.watts, "nvml_integral")
    miliwatts = _ler_uint("nvmlDeviceGetPowerUsage")
    if miliwatts is None:
        return Leitura(None, None)
    return Leitura(miliwatts / _MILI, "nvml_instantaneo")


def watts_gpu(agora=None) -> Optional[float]:
    """Só o número — a média por integral da janela desde a chamada anterior."""
    return medida_gpu(agora).watts


def limite_gpu() -> Optional[float]:
    """Teto de potência que o driver está IMPONDO (`enforced power limit`), em W.

    É o denominador que dá escala ao watt: 96 W sozinho não diz se a placa está
    parada ou apertada; 96 de 320 diz. Medido nesta máquina: 320 W."""
    miliwatts = _ler_uint("nvmlDeviceGetEnforcedPowerLimit")
    return (miliwatts / _MILI) if miliwatts is not None else None


def _monotonico() -> float:
    """Relógio default. Import tardio de `time` não vale a pena; a indireção
    existe só para o teste poder injetar o seu sem tocar em `time`."""
    import time

    return time.monotonic()


def reiniciar() -> None:
    """Esquece tudo que foi cacheado (lib, handle, baseline, aviso).

    Existe para o TESTE: o módulo guarda estado de propósito (o baseline é a
    memória da janela), e teste que compartilha baseline com o vizinho falha por
    ordem de execução — o tipo de flake que custa uma tarde para achar."""
    global _lib, _dispositivo, _tentou_carregar, _avisou_falha, _baseline
    with _trava:
        _lib = None
        _dispositivo = None
        _tentou_carregar = False
        _avisou_falha = False
        _baseline = None
