"""Escreve os átomos que a atomização DESCARTOU, achando-os pela página-fonte.

A régua atravessa idiomas de propósito: o livro está em inglês e os átomos em
português, mas NÚMERO não se traduz. Comparando os dados duros do trecho com os
dados duros dos átomos daquela mesma página (`fusao.lacunas_da_pagina`),
sobra exatamente o fato que ninguém registrou — "5 a 14 dias", "±0,5 ponto",
"6 a 9 semanas". Foi essa classe de fato que o teste cego mostrou faltando.

Diferente da FUSÃO (`fundir_bases.py`), que só remaneja o que já existe numa das
duas edições: aqui a fonte é o LIVRO, então entra conhecimento que nenhuma das
duas tinha. E diferente de re-atomizar a página, que só produziria duplicata —
o alvo é fechado nos dados ausentes.

    python scripts/preencher_lacunas.py --so-triagem
    python scripts/preencher_lacunas.py --dry-run --limite 10
    python scripts/preencher_lacunas.py --aplicar
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import textwrap
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import fusao, idioma, livro as livro_mod, prompts, rag, reparo  # noqa: E402
from mente_digital.atomos import dividir_atomos, normalizar_atomo  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.llm import LlamaManager, preparar_offline  # noqa: E402

import re  # noqa: E402

# Nota SOBRE a extração, não sobre o livro — ver a guarda no laço.
_META_RE = re.compile(
    r"\((?:trecho|texto|frase)\s+(?:incompleto|cortado|truncado)|"
    r"\(n[ãa]o\s+(?:especificado|informado|disponível|consta)|"
    r"informa[çc][ãa]o\s+incompleta|dado\s+n[ãa]o\s+dispon[íi]vel", re.IGNORECASE)

MIN_LACUNAS = 3          # abaixo disto não paga uma chamada de GPU
MAX_TOKENS = 700
ORCAMENTO_TRECHO = 5000


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


def _atomos_por_origem(pasta: Path) -> Dict[str, List[str]]:
    fora: Dict[str, List[str]] = defaultdict(list)
    for md in pasta.rglob("*.md"):
        try:
            texto = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        origem = (rag.parse_frontmatter(texto).get("origem") or "").strip()
        if not origem:
            continue
        _cat, a = reparo.triar(texto)
        if a.titulo:
            fora[origem].append(f"{a.titulo} {a.corpo}")
    return fora


def _jobs(filtro: str) -> List[Tuple[str, dict]]:
    base = Path(settings.dir_ingestao) / "processados"
    fora = []
    for j in sorted(base.glob("*.json")):
        try:
            job = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if filtro.lower() in job.get("livro", "").lower():
            fora.append((livro_mod.origem_do_job(job), job))
    return fora


async def main_async(args) -> int:
    pasta = Path(settings.dir_conhecimento_novo) / args.pasta_obra
    por_origem = _atomos_por_origem(pasta)
    jobs = _jobs(args.obra)
    print(f"{len(jobs)} página(s)-fonte | {sum(len(v) for v in por_origem.values())} átomos no vault")

    alvos = []
    censo: Counter = Counter()
    for origem, job in jobs:
        corpos = por_origem.get(origem, [])
        if not corpos:
            censo["página sem átomo no vault"] += 1
            continue
        faltam = fusao.lacunas_da_pagina(job.get("texto", ""), corpos)
        if len(faltam) < MIN_LACUNAS:
            censo["coberta"] += 1
            continue
        censo["com lacuna"] += 1
        alvos.append((origem, job, corpos, sorted(faltam)))

    for k, v in censo.most_common():
        print(f"  {v:5d}  {k}")
    total_lac = sum(len(f) for _o, _j, _c, f in alvos)
    print(f"\npáginas a preencher: {len(alvos)} | dados ausentes: {total_lac} "
          f"(mediana {sorted(len(f) for *_x, f in alvos)[len(alvos)//2] if alvos else 0} por página)")
    if args.so_triagem:
        for origem, _job, _corpos, faltam in alvos[: args.amostra]:
            print(f"\n  {origem[:88]}\n    faltam: {', '.join(faltam[:12])}")
        return 0
    if _servidor_no_ar():
        print("\nO servidor está NO AR — feche-o (a GPU é uma só).")
        return 3

    if args.limite:
        alvos = alvos[: args.limite]
    destino = pasta
    backup = None
    if args.aplicar:
        backup = Path("dados/backups") / f"lacunas_{datetime.now():%Y%m%d_%H%M%S}"
        backup.mkdir(parents=True, exist_ok=True)
        print(f"registro dos novos em: {backup}")

    llama = LlamaManager(preparar_offline(settings.caminho_modelo_atomizacao))
    await llama.load()
    escritos = 0
    recusas: Counter = Counter()
    mostrados = 0
    t0 = time.perf_counter()
    agora = datetime.now()
    try:
        for origem, job, corpos, faltam in alvos:
            trecho = (job.get("texto") or "")[:ORCAMENTO_TRECHO]
            saida = await llama.collect(
                prompts.prompt_atomos_faltantes(
                    job.get("livro", "?"), job.get("titulo_cap", "?"), trecho,
                    ", ".join(faltam[:20]),
                    " | ".join(c[:90] for c in corpos[:14])),
                system_prompt=prompts.SYS_SINTESE,
                max_tokens=MAX_TOKENS,
            )
            if prompts.TAG_NADA in saida.strip().upper()[:40]:
                recusas["modelo disse NADA"] += 1
                continue
            for bloco in dividir_atomos(saida):
                nota = normalizar_atomo(bloco, origem, agora)
                if not nota.strip():
                    recusas["bloco vazio/NADA"] += 1
                    continue
                _cat, a = reparo.triar(nota)
                # GUARDA CENTRAL: a nota tem de conter um dado que FALTAVA. Sem
                # isto a passada vira reatomização disfarçada — o modelo reescreve
                # o que já existe e o vault incha com duplicata.
                if not (fusao.tokens_de_dado(a.corpo) & set(faltam)):
                    recusas["não traz o dado que faltava"] += 1
                    continue
                if not reparo.em_pt_dominante(a.corpo) or idioma.em_ingles(a.corpo):
                    recusas["saiu em inglês"] += 1
                    continue
                # META-COMENTÁRIO: o modelo às vezes ANOTA que o trecho acabou no
                # meio ("Em 1938, New York City Mayo (trecho incompleto)") em vez
                # de pular o fato. Isso é nota sobre a extração, não sobre o
                # livro — e entra no embedding como se fosse conhecimento.
                if _META_RE.search(a.corpo):
                    recusas["meta-comentário sobre o trecho"] += 1
                    continue
                if fusao.palavras_ausentes(a.corpo, corpos, min_len=5) == set():
                    recusas["não acrescenta palavra nova"] += 1
                    continue
                if mostrados < args.amostra:
                    mostrados += 1
                    print(f"\n  + ## {a.titulo}")
                    for ln in textwrap.wrap(a.corpo.replace("\n", " "), 92):
                        print(f"      {ln}")
                if args.aplicar:
                    nome = f"Lacuna_{reparo._SO_CERQUILHA_RE.sub('', a.titulo)[:40]}"
                    nome = "".join(ch for ch in nome if ch.isalnum() or ch in " _-").strip()
                    caminho = destino / f"{nome}_{int(time.time())}_{escritos}.md"
                    caminho.write_text(nota, encoding="utf-8")
                    (backup / caminho.name).write_text(nota, encoding="utf-8")
                escritos += 1
    finally:
        await llama.unload()

    dt = time.perf_counter() - t0
    print(f"\n{'escreveria' if args.dry_run else 'escreveu'} {escritos} átomo(s) novo(s) "
          f"em {dt:.0f}s")
    if recusas:
        print("\ndescartados:")
        for m, n in recusas.most_common():
            print(f"  {n:5d}  {m}")
    if args.dry_run:
        print("\n(dry-run: NADA foi escrito)")
    elif escritos:
        print("\nRode `python scripts/reindexar.py`.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--obra", default="Cannabis Encyclopedia")
    ap.add_argument("--pasta-obra", default="the-cannabis-encyclopedia-jorge-cervante")
    ap.add_argument("--so-triagem", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--amostra", type=int, default=6)
    args = ap.parse_args()
    if not (args.so_triagem or args.dry_run or args.aplicar):
        ap.error("escolha --so-triagem, --dry-run ou --aplicar")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
