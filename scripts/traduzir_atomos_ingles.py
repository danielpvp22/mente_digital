"""Reescreve em PT-BR os átomos que saíram em inglês.

O defeito: livro em inglês atomizado com prompt que exige PT-BR funciona em ~97%
dos lotes, mas o modelo ESPELHA o idioma da fonte quando o trecho é muito
técnico. Medido na Cannabis Encyclopedia: 185 de 5.907 átomos (3,1%) com título
em inglês, 141 deles com o corpo junto. A pergunta chega em português e o
aterramento LÉXICO do gate é casamento de palavra — esses átomos não casam nada.

Mesmo molde do `traduzir_legendas_figuras.py`, que já rodou 635 legendas:
- o ORIGINAL fica no frontmatter (`titulo_original`), então a passada é
  reversível sem backup e idempotente (átomo já traduzido é pulado);
- a MALHA e as TAGS não são tocadas: mexer nos `[[conceitos]]` reescreveria as
  arestas do grafo, que é outro problema e outra passada;
- `--dry-run` mostra o antes/depois sem gravar.

    python scripts/traduzir_atomos_ingles.py --limite 5 --dry-run
    python scripts/traduzir_atomos_ingles.py --limite 0
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import socket
import sys
import textwrap
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import idioma, rag, reparo  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.llm import LlamaManager, preparar_offline  # noqa: E402
from mente_digital.telemetry import telemetry  # noqa: E402

SYS = (
    "Você traduz notas técnicas de cultivo e botânica para o português do Brasil. "
    "Traduz, não resume nem comenta: toda informação da nota original tem de "
    "aparecer na tradução, e nada além dela."
)

_FM_RE = re.compile(r"^(---\r?\n)(.*?)(\r?\n---\r?\n)", re.DOTALL)
_TITULO_RE = re.compile(r"^##\s+(.+)$", re.M)
_MALHA = "**Malha Neural:**"


def prompt(titulo: str, corpo: str, glossario: str = "") -> str:
    regra_glossario = (
        # Sem isto o modelo TRADUZ o jargão do cultivo e troca o FATO: o 4B fez
        # "buds" virar "borboletas", o 8B fez virar "cogumelos". Só os termos
        # presentes na nota são enviados (ver idioma.linha_glossario).
        f"5. Use EXATAMENTE estas formas para o jargão: {glossario}.\n" if glossario else "")
    return (
        f"Nota em inglês:\n## {titulo}\n{corpo}\n\n"
        "Reescreva em português do Brasil seguindo estas regras:\n"
        "1. Primeira linha: `## ` seguido do título traduzido.\n"
        "2. Linhas seguintes: o corpo traduzido, na mesma ordem de ideias.\n"
        "3. Preserve sigla, nome de composto, cultivar e unidade como estão "
        "(CB1, THC, CBD, pH, EC, ppm, N-P-K). Se traduzir um termo técnico, "
        "deixe o original entre parênteses na primeira ocorrência.\n"
        "4. NÃO acrescente informação que a nota não tem, não resuma e não "
        "comente. Sem tags, sem 'Malha Neural', sem aspas em volta.\n"
        + regra_glossario
    )


def partes(texto: str):
    """`(frontmatter, titulo, corpo, rodape)` — rodapé = malha + tags, intocados."""
    m = _FM_RE.match(texto)
    fm = m.group(0) if m else ""
    resto = texto[len(fm):]
    mt = _TITULO_RE.search(resto)
    if not mt:
        return fm, "", "", resto
    titulo = mt.group(1).strip()
    linhas = resto[mt.end():].strip("\n").split("\n")
    corpo, rodape = [], []
    for ln in linhas:
        # A MALHA e as TAGS ficam como estão: reescrever `[[conceito]]` mudaria as
        # arestas do grafo, e uma aresta renomeada não tem como ser reapontada.
        if ln.startswith(_MALHA) or re.match(r"^\s*#\w", ln):
            rodape.append(ln)
        elif rodape:
            rodape.append(ln)
        else:
            corpo.append(ln)
    return fm, titulo, "\n".join(corpo).strip(), "\n".join(rodape).strip()


def ja_traduzido(texto: str) -> bool:
    return "titulo_original:" in texto


def aplicar(texto: str, saida: str) -> str:
    """Monta a nota nova preservando frontmatter, malha e tags. "" se a saída
    do LLM não tiver o formato esperado (título com `## `)."""
    fm, titulo, _corpo, rodape = partes(texto)
    linhas = [ln for ln in saida.strip().split("\n")]
    while linhas and not linhas[0].strip():
        linhas.pop(0)
    if not linhas or not linhas[0].lstrip().startswith("##"):
        return ""
    novo_titulo = linhas[0].lstrip("#").strip()
    novo_corpo = "\n".join(
        ln for ln in linhas[1:]
        if not ln.startswith(_MALHA) and not re.match(r"^\s*#\w", ln)
    ).strip()
    if not novo_titulo:
        return ""
    guardado = titulo.replace("\n", " ").replace('"', "'")
    m = _FM_RE.match(fm)
    novo_fm = (m.group(1) + m.group(2) + f"\ntitulo_original: {guardado}"
               + m.group(3)) if m else fm
    partes_saida = [novo_fm + f"## {novo_titulo}"]
    if novo_corpo:
        partes_saida.append(novo_corpo)
    if rodape:
        partes_saida.append(rodape)
    return "\n".join(partes_saida) + "\n"


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


def _candidatos(origem_filtro: str, limite: int):
    pasta = Path(settings.dir_conhecimento_novo)
    achados = []
    # rglob: desde 2026-07-28 o átomo de livro nasce em Conhecimento_Novo/<obra>/,
    # e um glob raso não enxerga nenhum deles.
    for p in sorted(pasta.rglob("*.md")):
        texto = p.read_text(encoding="utf-8", errors="ignore")
        if ja_traduzido(texto):
            continue
        fm = rag.parse_frontmatter(texto)
        if origem_filtro and origem_filtro.lower() not in (fm.get("origem") or "").lower():
            continue
        _f, titulo, corpo, _r = partes(texto)
        # TÍTULO em inglês OU frase inglesa no CORPO (2026-07-28). Selecionar só
        # pelo título deixava um buraco: a passada do 8B produziu 356 átomos de
        # título português com frase inglesa no meio do corpo, e eles não tinham
        # dono — nem aqui, nem no reparo, que é para átomo QUEBRADO.
        #
        # E o reparo é a ferramenta ERRADA para eles: reescrever a partir da
        # fonte PERDE fato ("coloque a rede quando os galhos estiverem ~2 pés
        # mais longos" virou prosa genérica sobre trelissas). Traduzir preserva.
        if not titulo:
            continue
        if not idioma.em_ingles(titulo) and not reparo.frases_em_ingles(corpo):
            continue
        achados.append((p, titulo, corpo))
        if limite and len(achados) >= limite:
            break
    return achados


async def _rodar(origem_filtro: str, limite: int, dry: bool) -> int:
    pendentes = _candidatos(origem_filtro, limite)
    print(f"{len(pendentes)} átomo(s) com título em inglês (limite={limite or 'todos'})\n")
    if not pendentes:
        return 0
    if _servidor_no_ar():
        print("ATENÇÃO: o servidor está no ar — os dois vão disputar a mesma VRAM.")
        return 3

    # Modelo OFFLINE (8B): o servidor está fechado aqui, a VRAM está livre, e o
    # 4B é justamente quem inventou "borboletas" para "buds".
    backup = None
    if not dry:
        from datetime import datetime
        backup = Path("dados/backups") / f"traducao_{datetime.now():%Y%m%d_%H%M%S}"
        backup.mkdir(parents=True, exist_ok=True)
        print(f"backup dos originais em: {backup}\n")
    llama = LlamaManager(preparar_offline(settings.caminho_modelo_atomizacao))
    telemetry.track("IDIOMA", "Carregando o LLM…")
    await llama.load()
    t0 = time.perf_counter()
    feitos = falhos = 0
    try:
        for p, titulo, corpo in pendentes:
            original = f"{titulo}\n{corpo}"
            saida = await llama.collect(
                prompt(titulo, corpo,
                       idioma.linha_glossario(idioma.termos_do_glossario(original))),
                system_prompt=SYS,
                max_tokens=settings.max_tokens_sintese,
            )
            # Pós-checagem com PROVA: só troca a tradução errada conhecida onde o
            # inglês DAQUELA nota dizia aquilo. O prompt previne; isto pega o resto.
            saida, _n = idioma.aplicar_correcoes(saida, original)
            novo = aplicar(p.read_text(encoding="utf-8"), saida)
            if not novo:
                falhos += 1
                print(f"  [formato inesperado, pulado] {titulo[:60]}")
                continue
            novo_titulo = _TITULO_RE.search(novo).group(1)
            if idioma.em_ingles(novo_titulo):
                falhos += 1
                print(f"  [continuou em inglês, pulado] {titulo[:60]}")
                continue
            # O CORPO também: desde que a seleção passou a pegar nota de título
            # português com frase inglesa no meio, é ele que muda — mostrar só o
            # título esconde exatamente o que esta passada faz.
            _f, _t, novo_corpo, _r = partes(novo)
            if not dry and backup is not None:
                # O corpo INGLÊS é sobrescrito e o vault não está no git: sem
                # cópia, uma tradução ruim não tem desfazer (o `titulo_original`
                # do frontmatter só preserva o título).
                (backup / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  EN: {titulo[:74]}\n  PT: {novo_titulo[:74]}")
            for rotulo, txt in (("     antes ", corpo), ("     depois", novo_corpo)):
                linhas = textwrap.wrap(txt.replace("\n", " "), 92) or [""]
                print(f"{rotulo}: {linhas[0]}")
                for extra in linhas[1:]:
                    print(f"              {extra}")
            if not dry:
                p.write_text(novo, encoding="utf-8")
            feitos += 1
    finally:
        await llama.unload()

    dt = time.perf_counter() - t0
    verbo = "traduziria" if dry else "traduziu"
    print(f"\n{verbo} {feitos} átomo(s) em {dt:.0f}s ({dt / max(1, feitos):.1f}s cada)"
          f" | {falhos} pulado(s)")
    if not dry and feitos:
        print("A busca só vê o texto novo depois do próximo sync do vault.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--origem", default="The Cannabis Encyclopedia",
                    help="filtro por substring da proveniência ('' = vault inteiro)")
    ap.add_argument("--limite", type=int, default=20, help="0 = todos")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return asyncio.run(_rodar(args.origem, args.limite, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
