"""A/B: o modelo offline deve PENSAR antes de atomizar? (pergunta do dono, 2026-07-28)

A tese a favor: raciocinar condensaria melhor a pagina — menos atomos por trecho
e cada um mais rico. A tese contra e mecanica: o bloco `<think>` consome o MESMO
teto de saida que os atomos, e esgotar esse teto e a causa MEDIDA dos 2.087
atomos truncados no meio da frase.

Isto decide entre as duas com paginas REAIS do vault, medindo o que importa:
atomos por chamada, riqueza (palavras de conteudo distintas por atomo), quantos
sairam CORTADOS, e o tempo. NADA e salvo no vault — so o texto e medido.

Uso (servidor FECHADO — a GPU e uma so):
    python eval/ab_atomizacao_think.py --paginas 3
"""
import argparse
import asyncio
import json
import os
import socket
import statistics
import sys
import time
from pathlib import Path
from typing import List, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import livro as livro_mod  # noqa: E402
from mente_digital import prompts, textutils  # noqa: E402
from mente_digital.atomos import dividir_atomos, normalizar_atomo  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.llm import LlamaManager, preparar_offline  # noqa: E402


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


def _amostrar_lotes(n: int) -> List[Tuple[str, str, str]]:
    """(livro, capitulo, lote) de paginas reais ja processadas."""
    proc = Path(settings.dir_ingestao) / "processados"
    fora = []
    for j in sorted(proc.glob("*.json")):
        try:
            job = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        lotes = livro_mod.fatiar_lotes(job.get("texto", ""), settings.ingestao_lote_chars)
        if not lotes:
            continue
        fora.append((job.get("livro", "?"),
                     job.get("titulo_cap") or f"cap. {job.get('capitulo', '?')}",
                     lotes[0]))
        if len(fora) >= n:
            break
    return fora


def _medir(saida: str) -> dict:
    """Metricas do texto CRU devolvido pelo modelo (nada e salvo)."""
    blocos = dividir_atomos(saida)
    riquezas, truncados = [], 0
    for b in blocos:
        norm = normalizar_atomo(b, "A/B", __import__("datetime").datetime(2026, 7, 28))
        if not norm:
            continue
        corpo = [ln.strip() for ln in norm.splitlines()
                 if ln.strip() and not ln.startswith(("---", "## ", "origem:", "colhido_em:",
                                                      "**Malha", "#"))]
        texto = " ".join(corpo)
        riquezas.append(len({p for p in textutils.normaliza(texto).split() if len(p) > 2}))
        if texto and not texto.rstrip().endswith((".", "!", "?", ":", ";", ")", "%")):
            truncados += 1
    return {
        "atomos": len(blocos),
        "riqueza_mediana": statistics.median(riquezas) if riquezas else 0,
        "riqueza_min": min(riquezas) if riquezas else 0,
        "truncados": truncados,
        "chars": len(saida),
    }


async def _rodar(paginas: int) -> int:
    lotes = _amostrar_lotes(paginas)
    if not lotes:
        print("Nenhum job processado para amostrar.")
        return 2
    llama = LlamaManager(preparar_offline(settings.caminho_modelo_atomizacao))
    print(f"modelo: {settings.caminho_modelo_atomizacao}")
    print(f"lote={settings.ingestao_lote_chars} chars | teto={settings.max_tokens_atomizacao} tokens")
    await llama.load()
    linhas = []
    try:
        for pensar in (False, True):
            settings.llm_no_think = not pensar     # a diretiva '/no_think' no system
            settings.llm_strip_think = True        # higiene: o bloco nunca vira nota
            for i, (liv, cap, lote) in enumerate(lotes, 1):
                t0 = time.perf_counter()
                saida = await llama.collect(
                    prompts.prompt_atomizar_livro(liv, cap, lote),
                    max_tokens=settings.max_tokens_atomizacao,
                    system_prompt=prompts.SYS_SINTESE,
                )
                dt = time.perf_counter() - t0
                m = _medir(saida)
                m.update(pagina=i, pensar=pensar, segundos=dt)
                linhas.append(m)
                print(f"  [{'PENSA ' if pensar else 'direto'}] pág {i}: "
                      f"{m['atomos']:2d} átomos | riqueza mediana {m['riqueza_mediana']:5.1f} "
                      f"| mínima {m['riqueza_min']:3d} | truncados {m['truncados']} "
                      f"| {dt:5.1f}s", flush=True)
    finally:
        await llama.unload()

    print("\n=== RESUMO ===")
    for pensar in (False, True):
        g = [x for x in linhas if x["pensar"] is pensar]
        if not g:
            continue
        print(f"{'PENSANDO' if pensar else 'DIRETO  '}: "
              f"átomos/pág {statistics.mean(x['atomos'] for x in g):5.1f} | "
              f"riqueza mediana {statistics.mean(x['riqueza_mediana'] for x in g):5.1f} | "
              f"truncados {sum(x['truncados'] for x in g):3d} | "
              f"{statistics.mean(x['segundos'] for x in g):5.1f}s/pág")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--paginas", type=int, default=3, help="quantas páginas reais medir")
    ap.add_argument("--forcar", action="store_true", help="roda mesmo com o servidor no ar")
    args = ap.parse_args()
    if _servidor_no_ar() and not args.forcar:
        print("O servidor está NO AR e a GPU é uma só. Feche-o (ou use --forcar).")
        return 3
    return asyncio.run(_rodar(args.paginas))


if __name__ == "__main__":
    raise SystemExit(main())
