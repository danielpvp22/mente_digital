"""Aplica `idioma.CORRECOES` aos átomos JÁ GRAVADOS, com o job-fonte como prova.

O defeito (visto ao vivo em 2026-07-30): a resposta sobre fimming saiu com "não é
possível remover completamente o COGUMELO desejado" e a de secagem com "densidade
dos cogumelos". Cogumelo é fungo; o original diz *bud*.

`idioma.CORRECOES` já conhece a troca e `aplicar_correcoes` já a aplica — mas só
onde há PROVA, isto é, onde o inglês daquela nota de fato dizia "bud". Essa prova
existe nas passadas que leem o job (reparo, fusão) e NÃO existia para o átomo que
já estava em disco: ninguém nunca varreu a base com o original em mãos.

É o que este script faz. Nada de tabela nova, nada de heurística nova: o mesmo
`aplicar_correcoes`, alimentado com o texto INGLÊS da página de onde o átomo veio
(`livro.origem_do_job` casa os dois lados por igualdade, como em
`reparar_atomos_quebrados.py`).

Por que a prova importa, e não é zelo teórico: o ensaio a seco de 2026-07-28 pegou
"borboleta" sendo trocada por "bud" em notas onde a palavra estava CERTA
("coevolução borboleta-monarca", "refletor borboleta"). Sem o 3º campo da tabela,
esta passada corromperia notas boas.

Só o CORPO é reescrito (`reparo.separar`/`montar`): frontmatter, Malha e tags
carregam a identidade do átomo no grafo e no índice.

    python scripts/corrigir_traducoes.py --so-triagem
    python scripts/corrigir_traducoes.py --dry-run --amostra 20
    python scripts/corrigir_traducoes.py --aplicar
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import idioma, livro, rag, reparo  # noqa: E402
from mente_digital.config import settings  # noqa: E402


def _fontes_por_origem() -> Dict[str, str]:
    """`origem` do frontmatter -> texto INGLÊS da página. A prova."""
    fora: Dict[str, str] = {}
    for j in (Path(settings.dir_ingestao) / "processados").glob("*.json"):
        try:
            job = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        texto = job.get("texto") or ""
        if texto:
            fora[livro.origem_do_job(job)] = texto
    return fora


def _varrer(fontes: Dict[str, str]) -> Tuple[List[tuple], Counter, Counter]:
    """[(caminho, texto, atomo, corpo_novo)] + contagens. Puro de efeito."""
    vault = Path(settings.caminho_obsidian)
    alvos: List[tuple] = []
    por_troca: Counter = Counter()
    motivos: Counter = Counter()
    for md in vault.rglob("*.md"):
        if ".obsidian" in md.parts:
            continue
        try:
            texto = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        origem = (rag.parse_frontmatter(texto).get("origem") or "").strip()
        ingles = fontes.get(origem)
        if not ingles:
            # Sem job-fonte não há prova. O átomo auto-colhido (web/conversa) fica
            # de fora por construção — é o mesmo critério do reparo.
            motivos["sem job-fonte (sem prova)"] += 1
            continue
        atomo = reparo.separar(texto)
        if not atomo.titulo or not atomo.corpo.strip():
            motivos["sem corpo"] += 1
            continue
        novo, n = idioma.aplicar_correcoes(atomo.corpo, ingles)
        if not n or novo == atomo.corpo:
            motivos["nada a trocar"] += 1
            continue
        # QUAIS trocas aconteceram — o relatório que deixa o dono julgar por par,
        # em vez de um total que esconde a troca perigosa no meio das seguras.
        for errado, certo, _exige in idioma.CORRECOES:
            if errado.lower() in atomo.corpo.lower() and errado.lower() not in novo.lower():
                por_troca[f"{errado} -> {certo}"] += 1
        alvos.append((md, texto, atomo, novo))
    return alvos, por_troca, motivos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--so-triagem", action="store_true", help="só conta")
    ap.add_argument("--dry-run", action="store_true", help="mostra o antes/depois, não grava")
    ap.add_argument("--aplicar", action="store_true", help="grava no vault (com backup)")
    ap.add_argument("--amostra", type=int, default=15)
    args = ap.parse_args()
    if not (args.so_triagem or args.dry_run or args.aplicar):
        ap.error("escolha --so-triagem, --dry-run ou --aplicar")

    fontes = _fontes_por_origem()
    print(f"{len(fontes)} página(s) de livro com texto-fonte (a prova)")
    alvos, por_troca, motivos = _varrer(fontes)

    print(f"\n{len(alvos)} átomo(s) a corrigir")
    print("\npor troca:")
    for t, n in por_troca.most_common():
        print(f"  {n:6d}  {t}")
    print("\nnão tocados:")
    for m, n in motivos.most_common():
        print(f"  {n:6d}  {m}")

    for md, _texto, atomo, novo in alvos[: args.amostra]:
        print(f"\n  ## {atomo.titulo[:80]}   [{md.name[:40]}]")
        print(f"     ANTES : {atomo.corpo[:200]}")
        print(f"     DEPOIS: {novo[:200]}")

    if args.so_triagem or args.dry_run:
        print("\n(nada foi escrito)")
        return 0

    backup = Path("dados/backups") / f"traducoes_{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    print(f"\nbackup dos originais em: {backup}")
    for md, texto, atomo, novo in alvos:
        (backup / md.name).write_text(texto, encoding="utf-8")
        md.write_text(reparo.montar(atomo, novo), encoding="utf-8")
    print(f"corrigiu {len(alvos)} átomo(s)")
    print("\nRode `python scripts/reindexar.py` — a busca só vê o texto novo depois.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
