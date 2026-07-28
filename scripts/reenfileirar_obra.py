"""Devolve os capítulos de uma OBRA à fila de ingestão, para re-atomizar.

Por que existe (ordem do dono, 2026-07-28): a Cannabis Encyclopedia foi
atomizada com o 4B e lote de 6000 — a configuração que produziu 461 átomos
quebrados. Com o 8B (0 truncados em 24 medições) e lote 3500, refazer o livro
inteiro rende mais que remendar átomo a átomo. Os jobs guardam o TEXTO integral
em `dados/ingestao/processados/`, então re-atomizar não pede o PDF nem a web de
novo — é só devolvê-los a `pendentes/`.

ANTES de rodar isto, TIRE os átomos velhos da obra do vault E do índice
(`scripts/arquivar_obra.py --manter-figuras` + `scripts/reindexar.py`): se eles
continuarem indexados, o dedup de `_salvar_atomos` descarta cada átomo NOVO como
duplicata do pobre que já está lá, e a passada inteira vira trabalho perdido.

    python scripts/reenfileirar_obra.py --obra "Cannabis Encyclopedia" --dry-run
    python scripts/reenfileirar_obra.py --obra "Cannabis Encyclopedia" --aplicar
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital.config import settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--obra", required=True, help="substring do título do livro")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--aplicar", action="store_true")
    args = ap.parse_args()
    if not (args.dry_run or args.aplicar):
        ap.error("escolha --dry-run ou --aplicar")

    base = Path(settings.dir_ingestao)
    proc, pend = base / "processados", base / "pendentes"
    censo: Counter = Counter()
    alvos = []
    chars = 0
    for j in sorted(proc.glob("*.json")):
        try:
            job = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        obra = job.get("livro", "?")
        censo[obra] += 1
        if args.obra.lower() in obra.lower():
            alvos.append(j)
            chars += len(job.get("texto", ""))

    print(f"jobs processados por obra ({sum(censo.values())}):")
    for obra, n in censo.most_common():
        print(f"  {n:5d}  {obra[:78]}")
    if not alvos:
        print(f"\nNenhum job casa '{args.obra}'.")
        return 1
    lotes = chars // max(1, settings.ingestao_lote_chars)
    print(f"\n{len(alvos)} job(s) casam '{args.obra}' — {chars/1e6:.2f} Mchars, "
          f"~{lotes} lotes de {settings.ingestao_lote_chars} chars")
    print(f"modelo da atomização: {Path(settings.caminho_modelo_atomizacao).name}")
    if not args.aplicar:
        print("\n(dry-run: NADA foi movido)")
        return 0

    pend.mkdir(parents=True, exist_ok=True)
    for j in alvos:
        shutil.move(str(j), str(pend / j.name))
    print(f"{len(alvos)} job(s) de volta em {pend}.")
    print("Feche o servidor e rode: python scripts/atomizar_agora.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
