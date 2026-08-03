"""Detecta jogo em execucao e decide quando soltar o kernel — PURO/testavel.

Por que existe: o ajudante de watts (o unico processo elevado do projeto) le a
potencia da CPU por MSR, e isso exige o driver de kernel PawnIO. Anti-cheat de
kernel — o BattlEye do Escape from Tarkov e o caso concreto aqui — divide o mesmo
andar. Em vez de escolher entre "medir watt" e "jogar", o ajudante SAI DA FRENTE
enquanto o jogo roda e volta sozinho depois.

Mesma forma de `standby.py`: a decisao e uma funcao pura de (situacao) -> veredito
com MOTIVO. Isto age sozinho na ausencia do dono; sem o motivo registrado, "por
que o watt sumiu ontem a noite?" so se responde reencenando a noite.

⚠ STDLIB PURA, sem UM import do projeto. Quem importa isto roda como
ADMINISTRADOR (ver o cabecalho de `potencia_cpu.py`): um import distraido de
`config` traria o pydantic e o `.env` para dentro do processo elevado, e quem
edita um arquivo de texto comum nao pode dizer a um processo elevado o que fazer.
Ha teste em subprocesso que falha se isso acontecer.

⚠ PARAR O SERVICO NAO DESCARREGA O DRIVER. Medido em 2026-08-03: `sc stop PawnIO`
retorna sucesso, `sc query` ate anuncia `STOPPABLE`, e o estado continua RUNNING.
O PawnIO e enumerado como dispositivo PnP raiz (`ROOT\\PAWNIO\\0000`), entao quem
manda nele e o gerenciador de PnP, nao o SCM. O que descarrega de verdade e
DESABILITAR O DISPOSITIVO (`pnputil /disable-device`), e ai o servico cai para
Stopped. Nao troque por `sc stop` achando que e equivalente — parece funcionar e
deixa o driver carregado.
"""

from __future__ import annotations

import ctypes
import enum
import functools
from dataclasses import dataclass
from typing import Iterable

#: Instancia PnP do PawnIO. Constante e nao configuravel de proposito: e um
#: processo elevado, e caminho/identificador vindo de fora e superficie de ataque.
PAWNIO_INSTANCE_ID = r"ROOT\PAWNIO\0000"

#: Executaveis que disparam a pausa. Nomes, nao caminhos: o dono pode reinstalar
#: o jogo em outro disco e a regra continua valendo.
JOGOS_PADRAO = frozenset(
    {
        "escapefromtarkov.exe",
        "escapefromtarkovarena.exe",
        "battleye.exe",
        "beservice.exe",
    }
)


class Acao(enum.Enum):
    """O que o plantao deve fazer AGORA."""

    NADA = "nada"
    PAUSAR = "pausar"      # jogo entrou: fechar a lib e desabilitar o driver
    RETOMAR = "retomar"    # jogo saiu: reabilitar o driver e voltar a medir


@dataclass(frozen=True)
class Veredito:
    acao: Acao
    motivo: str
    jogo: str | None = None


def normalizar(nome: str) -> str:
    """'EscapeFromTarkov.EXE' -> 'escapefromtarkov.exe'."""
    return nome.strip().lower()


def detectar(
    em_execucao: Iterable[str], alvos: Iterable[str] = JOGOS_PADRAO
) -> str | None:
    """Primeiro alvo encontrado, ou None. Puro.

    Devolve o NOME em vez de um booleano porque o motivo vai para o log e
    "pausei por causa de EscapeFromTarkovArena.exe" e uma frase que se audita;
    "pausei porque detectei um jogo" nao.
    """
    procurados = {normalizar(a) for a in alvos}
    for bruto in em_execucao:
        nome = normalizar(bruto)
        if nome in procurados:
            return nome
    return None


def decidir(jogo: str | None, pausado: bool) -> Veredito:
    """Maquina de estados de duas casas. Pura — o relogio e o mundo ficam fora.

    A borda importa mais que o estado: so agimos na TRANSICAO, senao cada tique
    tentaria desabilitar um dispositivo ja desabilitado.
    """
    if jogo and not pausado:
        return Veredito(Acao.PAUSAR, f"{jogo} abriu — soltando o kernel", jogo)
    if jogo and pausado:
        return Veredito(Acao.NADA, f"{jogo} ainda rodando", jogo)
    if not jogo and pausado:
        return Veredito(Acao.RETOMAR, "nenhum jogo rodando — retomando a medicao")
    return Veredito(Acao.NADA, "sem jogo, medindo normalmente")


def comando_pnputil(habilitar: bool, instance_id: str = PAWNIO_INSTANCE_ID) -> list[str]:
    """argv do pnputil. Puro, para o teste conferir sem tocar no dispositivo."""
    verbo = "/enable-device" if habilitar else "/disable-device"
    return ["pnputil", verbo, instance_id]


# --------------------------------------------------------------------------
# A unica parte suja: perguntar ao Windows quem esta rodando.
# ctypes/Toolhelp em vez de psutil de proposito -- psutil nao e stdlib, e este
# modulo e importado pelo processo elevado.
#
# ⚠ TUDO QUE TOCA `ctypes.wintypes` E PREGUICOSO, e isso nao e estilo: o modulo
# `ctypes.wintypes` NAO EXISTE fora do Windows -- importa-lo no Linux levanta
# ValueError na hora. Com o import no topo, este arquivo quebraria no IMPORT em
# qualquer runner Linux; como o modulo de teste o importa, a suite inteira
# morreria no CI enquanto passa verde no Windows do dono. Ja aconteceu neste
# repo com `os.name`/pathlib, e o teste `test_import_nao_toca_wintypes` existe
# para que nao aconteca uma terceira vez.
# --------------------------------------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260


@functools.lru_cache(maxsize=1)
def _estrutura_processentry32w():
    """Monta PROCESSENTRY32W na PRIMEIRA chamada, nunca no import."""
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH),
        ]

    return PROCESSENTRY32W


def processos_em_execucao() -> set[str]:
    """Nomes dos executaveis vivos, em minusculas.

    Snapshot do Toolhelp: nao precisa abrir cada processo, entao nao pede
    privilegio nenhum e nao falha em processo protegido -- que e exatamente o
    caso de um jogo com anti-cheat.
    """
    from ctypes import wintypes

    entry = _estrutura_processentry32w()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(entry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(entry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        raise ctypes.WinError(ctypes.get_last_error())

    nomes: set[str] = set()
    try:
        entrada = entry()
        entrada.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entrada)):
            return nomes
        while True:
            nomes.add(normalizar(entrada.szExeFile))
            if not kernel32.Process32NextW(snap, ctypes.byref(entrada)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return nomes
