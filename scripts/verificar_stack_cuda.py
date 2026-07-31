"""
Verifica se a stack CUDA da máquina cobre uma arquitetura de GPU — o GATE para
trocar de placa (ex.: RTX 3080 sm_86 -> RTX 5080 Blackwell sm_120).

POR QUE ISTO EXISTE
    Trocar a GPU não é "instalar driver". Cada componente que toca CUDA carrega
    kernels COMPILADOS para arquiteturas específicas, e são QUATRO binários
    independentes nesta máquina (llama-cpp-python, torch, ctranslate2 e o
    llama-server do OCR). Levantamento de 2026-07-29 mediu que NENHUM deles tinha
    código sm_120:

      llama-cpp-python 0.3.34  cubins sm_60..sm_90, PTX só sm_90  -> JIT possível
      torch 2.5.1+cu121        arch_list até sm_90, ZERO PTX      -> sem JIT
      ctranslate2 4.8.1        cubins até sm_86, PTX compute_86   -> JIT possível
      llama-server (OCR)       cubins sm_86/89, CUDA 12.4         -> sem sm_120

    O sintoma de falha é SILENCIOSO e enganoso: `audio.py` só arma o pára-quedas de
    CPU dentro do `load`; o `transcribe` faz telemetry.error + `return ""`, e
    `ws.py:371` descarta texto <3 chars sem avisar ninguém. O app sobe "saudável",
    o microfone fica mudo para sempre e só uma linha de log denuncia.

    Por isso o veredito precisa ser MEDIDO no binário, antes e depois do upgrade.
    Rode este script, guarde a saída, faça o upgrade, rode de novo: o diff é a prova.

MÉTODO (e seus limites)
    Caminha os containers fatbinary de cada binário e lê a arquitetura no header de
    cada entrada (ver _archs_de_fatbins), separando cubin nativo de PTV/PTX:
      - cubin nativo da arch alvo  -> COBERTO (caminho rápido)
      - só PTX de arch <= alvo     -> JIT (o driver compila no 1º load; funciona,
                                      mas custa tempo e perde performance)
      - nada                       -> NAO COBERTO
    Onde a lib declara suas próprias archs (llama.cpp em llama_print_system_info,
    torch em get_arch_list) isso entra como segunda fonte — nos testes as duas
    concordaram, o que é a validação do parser.

    Uma arch coberta só por PTX NÃO garante que cada kernel roda correto — garante
    que existe caminho. Existem bugs sm_120 conhecidos no ggml recente. E o cuBLAS
    é julgado pela VERSÃO, não por cubin: libs math da NVIDIA não são
    forward-compatible na prática (GEMMs sm_90a com wgmma não JITam para sm_120).

USO
    python scripts/verificar_stack_cuda.py                 # alvo default: sm_120
    python scripts/verificar_stack_cuda.py --arch 86       # sanidade: a 3080 de hoje
    python scripts/verificar_stack_cuda.py --json saida.json

    Sai com código != 0 se algum componente não cobrir o alvo — serve de gate em CI
    ou como passo verificável do runbook (docs/UPGRADE_BLACKWELL.md).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- parse de cubin

_FATBIN_MAGIC = struct.pack("<I", 0xBA55ED50)
_PTX_TARGET = re.compile(rb"\.target\s+sm_(\d+)")
# CUDA 12.8 é a primeira release que conhece sm_120; 13.x tem regressão de MMQ em
# Blackwell (segfault), por isso o runbook fixa 12.8/12.9.
CUDA_MINIMA_PARA_SM120 = (12, 8)


def _archs_de_fatbins(blob: bytes) -> tuple[set[int], set[int]]:
    """(cubins, ptx) — caminha os containers fatbinary do binário.

    Método MEDIDO nos binários reais desta máquina, não deduzido de documentação:
      container: magic 0xBA55ED50 (4) | version (2) | headerSize (2) | fatSize (8)
      entrada:   kind (2) | version (2) | headerSize (4) | paddedPayloadSize (8)
                 ... e a ARQUITETURA em +0x1c (u32)
      kind 1 = PTX (JIT possível), kind 2 = ELF/cubin (nativo)

    Por que não varrer o ELF magic direto: no ctranslate2.dll os 175 cubins não
    saem por esse caminho (encoding de e_flags diferente), enquanto o header do
    fatbin é consistente nos DOIS binários — 139 containers no ggml-cuda.dll do
    llama.cpp e 25 no ctranslate2.dll, ambos com as archs corretas.
    """
    cubins: set[int] = set()
    ptx: set[int] = set()
    off = blob.find(_FATBIN_MAGIC)
    while off != -1:
        try:
            (fatsize,) = struct.unpack_from("<Q", blob, off + 8)
        except struct.error:
            break
        entrada, limite = off + 16, min(off + 16 + fatsize, len(blob))
        while entrada + 0x20 <= limite:
            try:
                kind, _ver, hsize = struct.unpack_from("<HHI", blob, entrada)
                (padded,) = struct.unpack_from("<Q", blob, entrada + 8)
                (arch,) = struct.unpack_from("<I", blob, entrada + 0x1C)
            except struct.error:
                break
            if not 8 <= hsize <= 4096:
                break  # header implausível: falso-positivo do magic
            if 30 <= arch <= 200:
                (cubins if kind == 2 else ptx).add(arch)
            entrada += hsize + padded
        off = blob.find(_FATBIN_MAGIC, off + 4)
    # PTX solto (fora de fatbin) — complementa, não substitui
    ptx |= {int(m.group(1)) for m in _PTX_TARGET.finditer(blob)}
    return cubins, ptx


def _dll_carregada(nome: str) -> str | None:
    """Caminho REAL da DLL que o processo carregou — pega a armadilha de PATH.

    No Windows a resolução é por NOME e o módulo é global ao processo: quem
    carregar cublas64_12.dll primeiro define para todos. Uma pasta antiga no PATH
    (ex.: D:\\projetos\\llama-omni\\llamacpp com CUDA 12.4) SOMBREIA o conda e faz
    o rebuild "não pegar".
    """
    if os.name != "nt":
        return None
    h = ctypes.windll.kernel32.GetModuleHandleW(nome)
    if not h:
        return None
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetModuleFileNameW(h, buf, 1024):
        return buf.value
    return None


# ------------------------------------------------------------------- resultados

@dataclass
class Componente:
    nome: str
    versao: str = "?"
    caminho: str = "?"
    cubins: set[int] = field(default_factory=set)
    ptx: set[int] = field(default_factory=set)
    arch_list: list[str] = field(default_factory=list)   # quando a lib declara
    notas: list[str] = field(default_factory=list)
    erro: str | None = None
    # o cuBLAS não embute cubins auditáveis: seu veredito vem da VERSÃO, não do ELF
    veredito_manual: str | None = None

    def veredito(self, alvo: int) -> str:
        if self.erro:
            return "ERRO"
        if self.veredito_manual:
            return self.veredito_manual
        if alvo in self.cubins:
            return "COBERTO"
        if any(p <= alvo for p in self.ptx):
            return "JIT"
        return "NAO COBERTO"

    def para_dict(self, alvo: int) -> dict:
        return {
            "nome": self.nome, "versao": self.versao, "caminho": self.caminho,
            "cubins": sorted(self.cubins), "ptx": sorted(self.ptx),
            "arch_list": self.arch_list, "veredito": self.veredito(alvo),
            "notas": self.notas, "erro": self.erro,
        }


# --------------------------------------------------------------- coletores

def ver_llama_cpp() -> Componente:
    c = Componente("llama-cpp-python (LLM)")
    try:
        import llama_cpp
    except Exception as exc:  # noqa: BLE001 - queremos reportar, não engolir
        c.erro = f"import falhou: {exc}"
        return c
    c.versao = getattr(llama_cpp, "__version__", "?")
    lib = Path(llama_cpp.__file__).parent / "lib" / "ggml-cuda.dll"
    if not lib.exists():
        # build unificado (versões antigas) ou não-Windows
        alt = list((Path(llama_cpp.__file__).parent / "lib").glob("*cuda*"))
        lib = alt[0] if alt else lib
    if lib.exists():
        c.caminho = str(lib)
        c.cubins, c.ptx = _archs_de_fatbins(lib.read_bytes())
    else:
        c.erro = "ggml-cuda.dll não encontrado (build sem CUDA?)"
        return c
    # a própria lib declara as ARCHS — segunda fonte, de graça
    try:
        info = llama_cpp.llama_cpp.llama_print_system_info()
        txt = info.decode(errors="replace") if isinstance(info, bytes) else str(info)
        m = re.search(r"ARCHS\s*=\s*([\d,]+)", txt)
        if m:
            # a lib declara no formato CMake de 3 dígitos (860 = sm_86)
            declaradas = {int(x) // 10 for x in m.group(1).split(",") if x}
            c.arch_list = [f"sm_{a}" for a in sorted(declaradas)]
            c.cubins |= declaradas   # fonte de 1a classe: a própria lib se declarando
        if "FORCE_MMQ = 1" in txt:
            # MMQ + CUDA 13.x tem segfault documentado em Blackwell
            c.notas.append("FORCE_MMQ=1 — evite driver/toolkit CUDA 13.x")
    except Exception as exc:  # noqa: BLE001
        c.notas.append(f"llama_print_system_info indisponível: {exc}")
    return c


def ver_torch() -> Componente:
    c = Componente("torch (XTTS + embeddings)")
    try:
        import torch
    except Exception as exc:  # noqa: BLE001
        c.erro = f"import falhou: {exc}"
        return c
    c.versao = f"{torch.__version__} (CUDA {torch.version.cuda})"
    try:
        c.arch_list = list(torch.cuda.get_arch_list())
        c.cubins = {int(a.removeprefix("sm_")) for a in c.arch_list
                    if a.startswith("sm_")}
    except Exception as exc:  # noqa: BLE001
        c.notas.append(f"get_arch_list falhou: {exc}")
    lib = Path(torch.__file__).parent / "lib" / "torch_cuda.dll"
    if lib.exists():
        c.caminho = str(lib)
        # torch de Windows é compilado SEM +PTX: sem PTX não existe JIT para frente.
        _, c.ptx = _archs_de_fatbins(lib.read_bytes())
        if not c.ptx:
            c.notas.append("zero PTX no binário — sem caminho de JIT, só reinstalar")
    return c


def ver_ctranslate2() -> Componente:
    c = Componente("ctranslate2 (STT/Whisper)")
    try:
        import ctranslate2
    except Exception as exc:  # noqa: BLE001
        c.erro = f"import falhou: {exc}"
        return c
    c.versao = ctranslate2.__version__
    base = Path(ctranslate2.__file__).parent
    lib = next(iter(base.glob("ctranslate2.dll")), None) or next(
        iter(base.glob("*ctranslate2*")), None)
    if lib and lib.is_file():
        c.caminho = str(lib)
        c.cubins, c.ptx = _archs_de_fatbins(lib.read_bytes())
    else:
        c.notas.append("binário do ctranslate2 não localizado")
    # o CT2 carrega cuBLAS por nome em runtime: é o elo fraco, não o próprio CT2
    c.notas.append(
        "depende de cuBLAS externo — ver a seção cuBLAS abaixo (libs math da "
        "NVIDIA não são forward-compatible na prática)")
    return c


def ver_ocr(caminho_bin: str | None) -> Componente:
    """llama-server do OCR (Fase 3). Binário AVULSO, fora do pip."""
    c = Componente("llama-server (OCR Fase 3)")
    if not caminho_bin:
        c.erro = "MENTE_OCR_BIN não definido — pule se não usa OCR"
        return c
    exe = Path(caminho_bin)
    if not exe.exists():
        c.erro = f"não existe: {exe}"
        return c
    lib = exe.parent / "ggml-cuda.dll"
    if not lib.exists():
        c.erro = f"ggml-cuda.dll não achado em {exe.parent}"
        return c
    c.caminho = str(lib)
    c.versao = exe.parent.name  # a release está no nome da pasta (ex.: llama-b10107)
    c.cubins, c.ptx = _archs_de_fatbins(lib.read_bytes())
    # ocr.py faz Popen(cwd=pasta do exe) sem env=: no Windows o diretório do EXE
    # vence o PATH, então as DLLs CUDA daqui mandam neste subprocesso.
    locais = sorted(p.name for p in exe.parent.glob("cu*64_1*.dll"))
    if locais:
        c.notas.append(
            f"DLLs CUDA na pasta do exe vencem o PATH neste subprocesso: {locais}")
    return c


def ver_cublas(alvo: int) -> Componente:
    """cuBLAS/cudart REALMENTE carregados — o detector da armadilha de PATH."""
    c = Componente("cuBLAS / cudart carregados no processo")
    achou = False
    for nome in ("cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll",
                 "cublas64_13.dll", "cudart64_13.dll"):
        caminho = _dll_carregada(nome)
        if caminho:
            achou = True
            c.notas.append(f"{nome} <- {caminho}")
    if not achou:
        c.notas.append("nenhuma DLL CUDA carregada ainda (importe torch/llama_cpp antes)")
    try:
        import ctypes.util  # noqa: F401
        h = ctypes.CDLL("cublas64_12.dll")
        ver = ctypes.c_int(0)
        # cublasGetProperty(0=MAJOR,1=MINOR,2=PATCH)
        partes = []
        for p in (0, 1, 2):
            if h.cublasGetProperty(p, ctypes.byref(ver)) == 0:
                partes.append(str(ver.value))
        if partes:
            c.versao = ".".join(partes)
            maj = int(partes[0])
            mn = int(partes[1]) if len(partes) > 1 else 0
            if alvo >= 120 and (maj, mn) < CUDA_MINIMA_PARA_SM120:
                c.veredito_manual = "NAO COBERTO"
                c.notas.append(
                    f"cuBLAS {c.versao} < 12.8 — NÃO tem kernel Blackwell e o PTX "
                    "dele não recupera (GEMMs sm_90a com wgmma não JITam)")
            else:
                c.veredito_manual = "COBERTO"
    except Exception as exc:  # noqa: BLE001
        c.notas.append(f"cublasGetProperty indisponível: {exc}")
        c.veredito_manual = "INDETERMINADO"
    return c


def ver_driver() -> dict:
    """Driver e CUDA runtime version, mais as GPUs presentes."""
    out: dict = {"gpus": [], "driver": "?", "cuda_driver": "?"}
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.used,compute_cap,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30, check=False)
        if r.returncode == 0:
            for linha in r.stdout.strip().splitlines():
                campos = [x.strip() for x in linha.split(",")]
                if len(campos) >= 5:
                    out["gpus"].append({
                        "nome": campos[0], "vram_total_mib": campos[1],
                        "vram_usada_mib": campos[2], "compute_cap": campos[3]})
                    out["driver"] = campos[4]
    except Exception as exc:  # noqa: BLE001
        out["erro"] = str(exc)
    return out


# ------------------------------------------------------------------------ saída

def _fmt(archs: set[int]) -> str:
    return ",".join(f"sm_{a}" for a in sorted(archs)) or "—"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arch", type=int, default=120,
                    help="arquitetura alvo (120=Blackwell/5080, 86=Ampere/3080)")
    ap.add_argument("--json", type=str, default=None,
                    help="grava o relatório completo em JSON (para diffar antes/depois)")
    ap.add_argument("--ocr-bin", type=str, default=None,
                    help="caminho do llama-server do OCR (default: lê do settings)")
    args = ap.parse_args()
    alvo = args.arch

    ocr_bin = args.ocr_bin
    if ocr_bin is None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from mente_digital.config import settings
            ocr_bin = getattr(settings, "ocr_bin", None)
        except Exception:  # noqa: BLE001 - o script tem de rodar sem o projeto
            ocr_bin = os.environ.get("MENTE_OCR_BIN")

    print(f"\n=== ALVO: sm_{alvo} " + "=" * 52)
    drv = ver_driver()
    print(f"driver {drv['driver']}")
    for g in drv["gpus"]:
        print(f"  GPU: {g['nome']} | {g['vram_usada_mib']}/{g['vram_total_mib']} MiB "
              f"| compute {g['compute_cap']}")
    if not drv["gpus"]:
        print("  (nenhuma GPU enumerada — análise segue estática nos binários)")

    comps = [ver_llama_cpp(), ver_torch(), ver_ctranslate2(),
             ver_ocr(ocr_bin), ver_cublas(alvo)]

    print("\n" + "-" * 78)
    print(f"{'componente':<34} {'veredito':<12} versão / archs")
    print("-" * 78)
    falhou = []
    for c in comps:
        v = c.veredito(alvo)
        # OCR sem MENTE_OCR_BIN é opcional; qualquer outro NAO COBERTO/ERRO é gate
        opcional = c.nome.startswith("llama-server") and c.erro and "não definido" in c.erro
        if v in ("NAO COBERTO", "ERRO") and not opcional:
            falhou.append(c.nome)
        print(f"{c.nome:<34} {v:<12} {c.versao}")
        if c.arch_list:
            print(f"{'':<34} {'':<12} declara: {','.join(c.arch_list)}")
        if c.cubins:
            print(f"{'':<34} {'':<12} cubins: {_fmt(c.cubins)}")
        if c.ptx:
            print(f"{'':<34} {'':<12} PTX:    {_fmt(c.ptx)}")
        if c.caminho not in ("?",):
            print(f"{'':<34} {'':<12} {c.caminho}")
        for n in c.notas:
            print(f"{'':<34} {'':<12} ! {n}")
        if c.erro:
            print(f"{'':<34} {'':<12} X {c.erro}")
    print("-" * 78)

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"alvo": alvo, "driver": drv,
             "componentes": [c.para_dict(alvo) for c in comps]},
            indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"relatório JSON: {args.json}")

    if falhou:
        print(f"\nFAIL — sem cobertura para sm_{alvo}: {', '.join(falhou)}")
        print("Roteiro de correção: docs/UPGRADE_BLACKWELL.md")
        return 1
    print(f"\nPASS — todos os componentes cobrem sm_{alvo} (nativo ou via JIT).")
    print("Atenção: JIT prova que existe caminho, não que cada kernel roda correto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
