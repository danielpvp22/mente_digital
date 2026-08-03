"""Mede o que esta acontecendo ENQUANTO o jogo roda: GPU, CPU por CCD e afinidade.

Existe para responder uma pergunta com numero em vez de sensacao: "a GPU esta
subutilizada jogando?". Sem isso nao da para saber se o gargalo e CPU, se o jogo
caiu no CCD errado, ou se nao ha problema nenhum.

Rode ANTES de instalar/configurar qualquer coisa (linha de base) e DEPOIS, com o
mesmo jogo e o mesmo mapa. Comparar as duas medidas e o unico jeito de dizer se
a mudanca fez efeito.

    python scripts/medir_jogo.py                      # espera o jogo aparecer
    python scripts/medir_jogo.py --proc EscapeFromTarkov.exe
    python scripts/medir_jogo.py --segundos 120

Topologia MEDIDA neste 7950X3D (scripts de apoio no scratchpad da sessao):
    CCD-A = CPUs logicas 0-15   -> tem o V-Cache (ganhou o teste de cache 64 MB
                                   por 44%, perdeu o de clock por 11%)
    CCD-B = CPUs logicas 16-31  -> clock maior
Jogo deve concentrar trabalho no CCD-A.

⚠ A leitura de GPU vem do nvidia-smi. "utilization.gpu" e a fracao de tempo com
QUALQUER kernel ativo -- nao e "quanto da placa esta sendo usada". Uma GPU em
99% de ocupacao com clock baixo nao esta saturada. Por isso amostramos tambem
clock, potencia e o motivo do throttle: e o conjunto que diz se a placa e o
gargalo, nunca o percentual sozinho.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psutil
except ImportError:
    print("psutil ausente. Instale com:\n"
          r"  C:\ProgramData\miniconda3\envs\llama-omni\python.exe -m pip install psutil")
    sys.exit(1)

CCD_A = list(range(0, 16))
CCD_B = list(range(16, 32))

ALVOS_PADRAO = [
    "EscapeFromTarkov.exe",
    "EscapeFromTarkovArena.exe",
]

CAMPOS_SMI = [
    "utilization.gpu",
    "clocks.current.graphics",
    "power.draw",
    "temperature.gpu",
    "memory.used",
]


@dataclass
class Amostra:
    t: float
    gpu_util: float | None
    gpu_clock: float | None
    gpu_watt: float | None
    gpu_temp: float | None
    vram_mb: float | None
    ccd_a: float
    ccd_b: float
    cpu_total: float
    proc_cpu: float | None


@dataclass
class Coleta:
    amostras: list[Amostra] = field(default_factory=list)
    afinidade_inicial: list[int] | None = None
    nome_proc: str | None = None
    pid: int | None = None


def achar_nvidia_smi() -> str | None:
    p = shutil.which("nvidia-smi")
    if p:
        return p
    padrao = Path(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe")
    return str(padrao) if padrao.exists() else None


def ler_gpu(smi: str | None):
    if not smi:
        return (None,) * 5
    try:
        saida = subprocess.run(
            [smi, f"--query-gpu={','.join(CAMPOS_SMI)}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()[0]
    except Exception:
        return (None,) * 5

    vals = []
    for bruto in saida.split(","):
        bruto = bruto.strip()
        try:
            vals.append(float(bruto))
        except ValueError:
            vals.append(None)  # "[N/A]" acontece; nao vira zero
    while len(vals) < 5:
        vals.append(None)
    return tuple(vals[:5])


def achar_processo(alvos: list[str]) -> psutil.Process | None:
    alvos_low = {a.lower() for a in alvos}
    for p in psutil.process_iter(["name", "pid"]):
        nome = (p.info["name"] or "").lower()
        if nome in alvos_low:
            return p
    return None


def media(cpus: list[float], indices: list[int]) -> float:
    vivos = [cpus[i] for i in indices if i < len(cpus)]
    return sum(vivos) / len(vivos) if vivos else 0.0


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return float("nan")
    ordenados = sorted(vals)
    k = min(int(round((len(ordenados) - 1) * q)), len(ordenados) - 1)
    return ordenados[k]


def coletar(alvos: list[str], segundos: int, intervalo: float,
            esperar: bool) -> Coleta:
    smi = achar_nvidia_smi()
    if not smi:
        print("[!] nvidia-smi nao encontrado -- as colunas de GPU virao vazias.")

    proc = achar_processo(alvos)
    if proc is None and esperar:
        print(f"aguardando um destes processos: {', '.join(alvos)}"
              "   (Ctrl+C cancela)")
        while proc is None:
            time.sleep(2)
            proc = achar_processo(alvos)

    c = Coleta()
    if proc is not None:
        c.pid, c.nome_proc = proc.pid, proc.name()
        try:
            c.afinidade_inicial = proc.cpu_affinity()
        except Exception:
            c.afinidade_inicial = None
        print(f"processo: {c.nome_proc} (pid {c.pid})")
        print(f"afinidade atual: {resumir_cpus(c.afinidade_inicial)}")
        proc.cpu_percent(None)
    else:
        print("[!] processo nao encontrado -- medindo so a maquina.")

    psutil.cpu_percent(percpu=True)
    time.sleep(intervalo)

    fim = time.time() + segundos
    print(f"\ncoletando por {segundos}s a cada {intervalo}s...\n")
    print(f"{'t':>5} {'GPU%':>6} {'MHz':>6} {'W':>6} {'C':>4} "
          f"{'CCD-A%':>7} {'CCD-B%':>7} {'proc%':>7}")

    t0 = time.time()
    while time.time() < fim:
        cpus = psutil.cpu_percent(percpu=True)
        util, clock, watt, temp, vram = ler_gpu(smi)
        a, b = media(cpus, CCD_A), media(cpus, CCD_B)

        pcpu = None
        if proc is not None:
            try:
                pcpu = proc.cpu_percent(None)
            except psutil.NoSuchProcess:
                print("\n[!] o processo terminou -- encerrando a coleta.")
                break

        dt = time.time() - t0
        c.amostras.append(Amostra(dt, util, clock, watt, temp, vram, a, b,
                                  sum(cpus) / len(cpus), pcpu))
        print(f"{dt:5.0f} {fmt(util,1):>6} {fmt(clock,0):>6} {fmt(watt,0):>6} "
              f"{fmt(temp,0):>4} {a:7.1f} {b:7.1f} "
              f"{fmt(pcpu,1):>7}")
        time.sleep(intervalo)

    return c


def fmt(v, casas):
    return "-" if v is None else f"{v:.{casas}f}"


def resumir_cpus(cpus: list[int] | None) -> str:
    if not cpus:
        return "(desconhecida)"
    s = set(cpus)
    if s == set(CCD_A):
        return "CPUs 0-15 -> so o CCD-A (V-Cache) ✔"
    if s == set(CCD_B):
        return "CPUs 16-31 -> so o CCD-B (clock alto)"
    if s == set(CCD_A) | set(CCD_B):
        return "CPUs 0-31 -> os DOIS CCDs (sem fixacao)"
    return f"{sorted(s)}"


def relatorio(c: Coleta, saida: Path):
    if not c.amostras:
        print("nenhuma amostra coletada.")
        return

    with saida.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t_s", "gpu_util", "gpu_mhz", "gpu_w", "gpu_c", "vram_mb",
                    "ccd_a_pct", "ccd_b_pct", "cpu_total_pct", "proc_pct"])
        for a in c.amostras:
            w.writerow([f"{a.t:.1f}", a.gpu_util, a.gpu_clock, a.gpu_watt,
                        a.gpu_temp, a.vram_mb, f"{a.ccd_a:.1f}",
                        f"{a.ccd_b:.1f}", f"{a.cpu_total:.1f}", a.proc_cpu])

    gpu = [a.gpu_util for a in c.amostras if a.gpu_util is not None]
    aa = [a.ccd_a for a in c.amostras]
    bb = [a.ccd_b for a in c.amostras]
    watts = [a.gpu_watt for a in c.amostras if a.gpu_watt is not None]

    print("\n" + "=" * 64)
    print(f"amostras: {len(c.amostras)}   csv: {saida}")
    if c.nome_proc:
        print(f"processo: {c.nome_proc} (pid {c.pid})")
        print(f"afinidade: {resumir_cpus(c.afinidade_inicial)}")
    if gpu:
        print(f"\nGPU util   p50 {pct(gpu,.50):5.1f}%   p95 {pct(gpu,.95):5.1f}%"
              f"   min {min(gpu):5.1f}%   max {max(gpu):5.1f}%")
    if watts:
        print(f"GPU watts  p50 {pct(watts,.50):5.0f}W   max {max(watts):5.0f}W")
    print(f"CCD-A      p50 {pct(aa,.50):5.1f}%   max {max(aa):5.1f}%   "
          "(V-Cache, e onde o jogo DEVE estar)")
    print(f"CCD-B      p50 {pct(bb,.50):5.1f}%   max {max(bb):5.1f}%")

    print("\n--- leitura ---")
    if gpu:
        g50 = pct(gpu, .50)
        if g50 >= 95:
            print("GPU perto do teto: a placa e o gargalo. Mexer em CCD/afinidade")
            print("NAO vai aumentar FPS -- o caminho seria resolucao/qualidade.")
        elif g50 >= 80:
            print("GPU alta mas nao saturada; ganho de CPU tende a ser pequeno.")
        else:
            print(f"GPU em {g50:.0f}%: ha folga na placa. Vale investigar a CPU.")
    if aa and bb:
        if pct(bb, .50) > pct(aa, .50) * 1.3:
            print("O trabalho esta MAIS no CCD-B (clock alto) que no CCD-A")
            print("(V-Cache) -- e o padrao que o 3D V-Cache Optimizer corrige.")
        elif pct(aa, .50) > pct(bb, .50) * 1.3:
            print("O trabalho ja esta concentrado no CCD-A (V-Cache).")
        else:
            print("Trabalho ESPALHADO pelos dois CCDs -- e o caso que paga")
            print("latencia de comunicacao entre eles.")
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proc", action="append", default=None,
                    help="nome do executavel do jogo (pode repetir)")
    ap.add_argument("--segundos", type=int, default=90)
    ap.add_argument("--intervalo", type=float, default=1.0)
    ap.add_argument("--saida", default=None, help="caminho do CSV")
    ap.add_argument("--nao-esperar", action="store_true",
                    help="nao fica aguardando o jogo abrir")
    args = ap.parse_args()

    alvos = args.proc or ALVOS_PADRAO
    saida = Path(args.saida) if args.saida else Path(
        f"medicao_jogo_{time.strftime('%Y%m%d_%H%M%S')}.csv")

    try:
        c = coletar(alvos, args.segundos, args.intervalo, not args.nao_esperar)
    except KeyboardInterrupt:
        print("\ninterrompido.")
        return
    relatorio(c, saida)


if __name__ == "__main__":
    main()
