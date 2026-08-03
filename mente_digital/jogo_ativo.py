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

#: Jogos que vao para o CCD com V-Cache. DIFERENTE de JOGOS_PADRAO de proposito:
#: aquele inclui BEService/BattlEye porque o anti-cheat sobe ANTES do jogo e e o
#: gatilho certo para soltar o kernel A TEMPO. Mas o servico do anti-cheat nao
#: renderiza nada -- fixa-lo no V-Cache so tiraria nucleo de quem precisa.
JOGOS_AFINIDADE = frozenset(
    {
        "escapefromtarkov.exe",
        "escapefromtarkovarena.exe",
    }
)

#: Mascara do CCD com V-Cache nesta maquina (7950X3D): CPUs logicas 0-15.
#:
#: MEDIDO em 2026-08-03, nao herdado do folclore "CCD0 = cores 0-7". Aqui o
#: Windows reporta 96 MB de L3 nos DOIS CCDs (soma 192 MB contra os 128 MB reais
#: do processador), entao a topologia do sistema NAO distingue os dois -- ele nao
#: enxerga a assimetria. Quem separou foi um benchmark cruzado, com dois testes
#: porque um so seria ambiguo: working set de 512 KB (mora no L2, mede clock)
#: contra 64 MB (cabe nos 96 MB do V-Cache, nao cabe nos 32 MB do outro):
#:
#:     CPUs 0-15   perdeu clock por 11,2%, GANHOU cache por 44,1%  -> V-Cache
#:     CPUs 16-31  ganhou clock, perdeu cache                      -> clock alto
#:
#: Confirmado no jogo (Tarkov Arena, parado no mesmo ponto, 4 trocas de ida e
#: volta): livre 195-200 FPS / GPU 67% -> fixado aqui 251-258 FPS / GPU 84%.
#:
#: ⚠ O CONTROLE E O QUE DA VALOR AO NUMERO: fixar no CCD-B (16-31) deu 186-194
#: FPS, PIOR que deixar livre. Ou seja, o ganho NAO vem de "confinar a um unico
#: CCD e evitar latencia entre eles" -- vem do V-Cache especificamente. Sem esse
#: teste a conclusao seria plausivel e errada.
#:
#: ⚠ E ESPECIFICO DESTA CPU. Noutro 7950X3D a numeracao pode diferir, e noutro
#: modelo nao existe. Refaca o benchmark antes de reusar (--mascara sobrescreve).
MASCARA_VCACHE = 0xFFFF


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


def processos_com_pid() -> list[tuple[int, str]]:
    """(pid, nome em minusculas) de todo processo vivo.

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

    achados: list[tuple[int, str]] = []
    try:
        entrada = entry()
        entrada.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entrada)):
            return achados
        while True:
            achados.append((int(entrada.th32ProcessID), normalizar(entrada.szExeFile)))
            if not kernel32.Process32NextW(snap, ctypes.byref(entrada)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return achados


def processos_em_execucao() -> set[str]:
    """So os nomes. Deriva de `processos_com_pid` para nao duplicar o Toolhelp."""
    return {nome for _, nome in processos_com_pid()}


# --------------------------------------------------------------------------
# Afinidade: mandar o jogo para o CCD com V-Cache.
#
# Existe porque o driver AMD 3D V-Cache Performance Optimizer NAO fez isso nesta
# maquina -- medido em 2026-08-03, com o driver instalado e o servico rodando, o
# jogo continuou preferindo o CCD errado (CCD-B a 24,8% contra CCD-A a 20,0%).
# A causa provavel: o Arena e build de BETA e nao esta na Known Game List da
# Microsoft, e o Optimizer depende dessa classificacao. Uma regra `App` escrita
# a mao no registro do driver tambem nao mudou nada.
# --------------------------------------------------------------------------

PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def alvos_de_afinidade(
    processos: Iterable[tuple[int, str]], alvos: Iterable[str] = JOGOS_AFINIDADE
) -> list[tuple[int, str]]:
    """Quais (pid, nome) devem ir para o V-Cache. Puro.

    Devolve o nome NORMALIZADO, igual a `detectar` -- duas funcoes irmas com
    contratos diferentes viram bug de log e de comparacao mais tarde.
    """
    procurados = {normalizar(a) for a in alvos}
    achados = ((pid, normalizar(nome)) for pid, nome in processos)
    return [(pid, nome) for pid, nome in achados if nome in procurados]


def precisa_fixar(atual: int | None, desejada: int) -> bool:
    """Puro. `None` (nao consegui ler) conta como 'precisa' -- tentar e barato e
    o pior caso e um erro logado; NAO tentar deixaria o jogo no CCD errado em
    silencio, que e o defeito que este modulo existe para corrigir."""
    return atual != desejada


def afinidade_atual(pid: int) -> int | None:
    """Mascara de afinidade do processo, ou None se nao der para ler."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        proc = ctypes.c_uint64(0)
        sistema = ctypes.c_uint64(0)
        if not kernel32.GetProcessAffinityMask(
            h, ctypes.byref(proc), ctypes.byref(sistema)
        ):
            return None
        return int(proc.value)
    finally:
        kernel32.CloseHandle(h)


def fixar_afinidade(pid: int, mascara: int = MASCARA_VCACHE) -> tuple[bool, str]:
    """Prende o processo na mascara. (ok, mensagem) -- nunca levanta.

    ⚠ ESTREITAR FUNCIONA, TROCAR DE LADO NAO. `SetProcessAffinityMask` falha com
    ERROR_INVALID_PARAMETER se alguma THREAD ja tiver afinidade que nao cruze a
    nova mascara -- foi o que aconteceu ao tentar ir de 0x0000FFFF direto para
    0xFFFF0000 durante os testes. Como aqui so vamos de "todos" para um
    subconjunto, o caminho e seguro; quem precisar trocar de CCD tem de passar
    pela mascara cheia antes.
    """
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    h = kernel32.OpenProcess(
        PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not h:
        return False, f"nao consegui abrir o pid {pid}: {ctypes.WinError(ctypes.get_last_error())}"
    try:
        if not kernel32.SetProcessAffinityMask(h, wintypes.WPARAM(mascara)):
            return False, str(ctypes.WinError(ctypes.get_last_error()))
        return True, f"pid {pid} fixado em 0x{mascara:X}"
    finally:
        kernel32.CloseHandle(h)
