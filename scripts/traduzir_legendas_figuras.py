"""Reescreve em PT-BR a legenda das notas de figura de livro em inglês.

O defeito medido em 2026-07-26: a pergunta chega em português e a legenda do
Cervantes está em inglês, com 5 palavras. O e5 acha "deficiência em folha" mas não
segura a diferença entre `Mg`, `Mn` e `Fe` — uma pergunta sobre magnésio trouxe 2
figuras certas e 4 de manganês/ferro. As notas do Amabis (legenda em português)
não erram assim.

Dois consertos numa passada só:
1. TRADUZIR — o casamento cross-língua do embedding é o elo fraco;
2. EXPANDIR A SIGLA química — é ela que discrimina, e sozinha ela é ruído num
   texto curto. "Mg" vira "magnésio (Mg)", que casa por semântica E por léxico.

O TEXTO DA PÁGINA entra como contexto (observação do dono: nem sempre a legenda
basta para saber do que a figura trata). Ele é contexto, não fonte: o prompt
proíbe descrever o que a legenda não diz — inventar aqui seria pôr no vault uma
descrição que ninguém conferiu.

A legenda original fica preservada no frontmatter (`legenda_original`), então a
passada é reversível sem backup e idempotente (nota já traduzida é pulada).

    python scripts/traduzir_legendas_figuras.py --livro cervantes --limite 30 --dry-run
    python scripts/traduzir_legendas_figuras.py --livro cervantes --limite 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital.config import settings  # noqa: E402
from mente_digital.llm import LlamaManager  # noqa: E402
from mente_digital.telemetry import telemetry  # noqa: E402

SIGLAS = ("N = nitrogênio, P = fósforo, K = potássio, Mg = magnésio, "
          "Mn = manganês, Fe = ferro, Ca = cálcio, S = enxofre, Zn = zinco, "
          "B = boro, Cu = cobre, Mo = molibdênio, Cl = cloro, Ni = níquel")

SYS = (
    "Você traduz legendas de figuras de livros técnicos para o português do Brasil. "
    "Responde SOMENTE com a legenda traduzida, em uma frase, sem aspas e sem comentário."
)

_LINHAS_META = ("#", "![[", "---", "**Malha", "origem:", "colhido_em:", "legenda_original:")
_FM_RE = re.compile(r"^(---\r?\n)(.*?)(\r?\n---\r?\n)", re.DOTALL)


def prompt(legenda: str, texto_pagina: str) -> str:
    """O texto da página é CONTEXTO para desambiguar, nunca fonte de conteúdo novo."""
    return (
        f"Legenda original (inglês): {legenda}\n\n"
        f"Trecho da página onde a figura aparece (contexto, pode estar truncado):\n"
        f"{texto_pagina[:1200]}\n\n"
        "Escreva a legenda em português do Brasil seguindo estas regras:\n"
        f"1. Expanda sigla de elemento químico por extenso, mantendo a sigla entre "
        f"parênteses. Tabela: {SIGLAS}.\n"
        "2. Use o trecho da página APENAS para entender o contexto. NÃO descreva nada "
        "que a legenda original não diga — se ela é vaga, a tradução é igualmente vaga.\n"
        "3. Uma frase só. Sem aspas, sem 'Legenda:', sem explicação.\n"
    )


def corpo_e_legenda(texto: str):
    """Devolve (linhas, índice da linha da legenda) — a 1ª linha de conteúdo real."""
    linhas = texto.split("\n")
    for i, linha in enumerate(linhas):
        if linha.strip() and not linha.startswith(_LINHAS_META):
            return linhas, i
    return linhas, -1


def ja_traduzida(texto: str) -> bool:
    return "legenda_original:" in texto


def aplicar(texto: str, nova: str, original: str) -> str:
    """Troca a legenda e guarda a original no frontmatter (reversível/idempotente)."""
    linhas, i = corpo_e_legenda(texto)
    if i < 0:
        return texto
    linhas[i] = nova
    novo = "\n".join(linhas)
    m = _FM_RE.match(novo)
    if not m:
        return novo
    guardada = original.replace("\n", " ").replace('"', "'")
    return (m.group(1) + m.group(2) + f"\nlegenda_original: {guardada}"
            + m.group(3) + novo[m.end():])


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


async def _rodar(livro: str, limite: int, dry: bool, filtro: str = "") -> int:
    pasta = Path(settings.caminho_obsidian) / settings.subpasta_figuras
    alvos = [p for p in sorted(pasta.rglob("*.md")) if livro.lower() in p.name.lower()]
    if not alvos:
        print(f"nenhuma nota de figura casando '{livro}'.")
        return 1

    # texto da página, do relatório da passada de recorte
    rel_path = RAIZ / "eval" / "relatorios" / f"{livro}.json"
    por_pagina = {}
    if rel_path.is_file():
        dados = json.loads(rel_path.read_text(encoding="utf-8"))
        por_pagina = {p["pagina"]: (p.get("texto_ocr") or "") for p in dados["paginas"]}
        print(f"contexto de página: {len(por_pagina)} páginas do relatório")
    else:
        print(f"AVISO: {rel_path} não existe — traduzindo só pela legenda.")

    pendentes = []
    for p in alvos:
        texto = p.read_text(encoding="utf-8")
        if ja_traduzida(texto):
            continue
        linhas, i = corpo_e_legenda(texto)
        if i < 0:
            continue
        legenda = linhas[i].strip()
        # legenda genérica não tem o que traduzir: é rótulo nosso, já em português
        if re.match(r"^Figura da p[áa]gina \d+", legenda, re.IGNORECASE):
            continue
        # `--filtro` existe para mirar um CLUSTER (ex.: as legendas de deficiência,
        # onde o erro foi medido) em vez das primeiras N em ordem de nome.
        if filtro and not re.search(filtro, legenda, re.IGNORECASE):
            continue
        m = re.search(r"_p(\d{4})_f\d+\.md$", p.name)
        pagina = int(m.group(1)) if m else 0
        pendentes.append((p, legenda, por_pagina.get(pagina, "")))
        if limite and len(pendentes) >= limite:
            break

    print(f"{len(pendentes)} nota(s) a traduzir (limite={limite or 'todas'})\n")
    if not pendentes:
        return 0

    if _servidor_no_ar():
        print("ATENÇÃO: o servidor está no ar — os dois vão disputar a mesma VRAM.")

    llama = LlamaManager()
    telemetry.track("LEGENDAS", "Carregando o LLM…")
    await llama.load()
    t0 = time.perf_counter()
    feitas = 0
    try:
        for p, legenda, contexto in pendentes:
            saida = (await llama.collect(
                prompt(legenda, contexto), system_prompt=SYS, max_tokens=120
            )).strip().strip('"').split("\n")[0].strip()
            if not saida:
                print(f"  [vazio] {p.name[-22:]}  {legenda[:50]}")
                continue
            print(f"  {p.name[-22:]}\n     EN: {legenda[:70]}\n     PT: {saida[:70]}")
            if not dry:
                p.write_text(aplicar(p.read_text(encoding="utf-8"), saida, legenda),
                             encoding="utf-8")
            feitas += 1
    finally:
        await llama.unload()

    dt = time.perf_counter() - t0
    verbo = "traduziria" if dry else "traduziu"
    print(f"\n{verbo} {feitas} legenda(s) em {dt:.0f}s ({dt / max(1, feitas):.1f}s cada)")
    if not dry and feitas:
        print("Rode `python scripts/reindexar.py` para a busca ver o texto novo.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--livro", default="cervantes", help="filtro pelo nome do arquivo")
    ap.add_argument("--limite", type=int, default=30, help="0 = todas")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--filtro", default="", help="regex na legenda (mira um cluster)")
    args = ap.parse_args()
    return asyncio.run(_rodar(args.livro, args.limite, args.dry_run, args.filtro))


if __name__ == "__main__":
    raise SystemExit(main())
