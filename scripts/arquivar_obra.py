"""Tira uma OBRA inteira do vault (ou a agrupa numa pasta própria).

Ordem do dono (2026-07-28): "manter apenas a Cannabis Encyclopedia nova" e
"separe os livros por pastas, estão todos juntos no Conhecimento_Novo". Hoje são
~26 mil notas de 4 livros no MESMO diretório, misturadas com o que veio de
conversa e web — não dá para aposentar um livro sem varrer o frontmatter de
todos, um por um.

O critério é a PROVENIÊNCIA (`origem` do frontmatter), nunca o nome do arquivo:
o nome é derivado do título do átomo e não diz de que obra ele veio.

Tirar do VAULT é o bastante para tirar da busca: o `VectorStore.sync` purga as
fontes órfãs (nota que sumiu ou saiu de baixo do vault) — ver a "PURGA DE
ÓRFÃOS" em rag.py. Por isso o destino padrão fica FORA de `Cerebro_Digital`, ao
lado de `dados/aposentados/`, e a operação é reversível: é um mover, não um
apagar.

    python scripts/arquivar_obra.py --obra "Amabis" --dry-run
    python scripts/arquivar_obra.py --obra "Amabis" --aplicar
    python scripts/arquivar_obra.py --obra "Cannabis Encyclopedia" --para dados/arquivo_obras/encyclopedia_pre8b --aplicar
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import List, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import livro as livro_mod, rag  # noqa: E402
from mente_digital.config import settings  # noqa: E402


def _obra_da_origem(origem: str) -> str:
    """`Livro 'X' — cap (p. 1-2)` -> `X`. "" quando não é nota de livro."""
    if not origem.startswith("Livro '"):
        return ""
    resto = origem[len("Livro '"):]
    fim = resto.find("'")
    return resto[:fim].strip() if fim > 0 else ""


def _levantar(filtro: str, manter_figuras: bool = False) -> Tuple[List[Tuple[Path, str]], Counter]:
    vault = Path(settings.caminho_obsidian)
    achados: List[Tuple[Path, str]] = []
    censo: Counter = Counter()
    for md in vault.rglob("*.md"):
        if ".obsidian" in md.parts:
            continue
        # As notas de FIGURA casam a mesma `origem` dos átomos (é ela que ancora a
        # figura à página). Numa RE-ATOMIZAÇÃO elas têm de FICAR: as imagens não
        # são regeradas, e a síntese nova aponta para elas.
        if manter_figuras and settings.subpasta_figuras in md.parts:
            continue
        try:
            origem = (rag.parse_frontmatter(md.read_text(encoding="utf-8"))
                      .get("origem") or "").strip()
        except (OSError, UnicodeDecodeError):
            continue
        obra = _obra_da_origem(origem)
        censo[obra or "(conversa/web/manual)"] += 1
        if obra and filtro.lower() in obra.lower():
            achados.append((md, obra))
    return achados, censo


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--obra", default="", help="substring do título da obra (vazio = só censo)")
    ap.add_argument("--para", default="", help="destino (padrão: dados/arquivo_obras/<slug>)")
    ap.add_argument("--manter-figuras", action="store_true",
                    help="não move as notas de figura (use ao RE-ATOMIZAR a obra)")
    ap.add_argument("--varias", action="store_true",
                    help="permite que o filtro pegue mais de uma variante de título")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--aplicar", action="store_true")
    args = ap.parse_args()

    achados, censo = _levantar(args.obra, args.manter_figuras)
    print(f"\ncenso do vault ({sum(censo.values())} notas):")
    for obra, n in censo.most_common():
        print(f"  {n:6d}  {obra[:80]}")
    if not args.obra:
        return 0
    if not achados:
        print(f"\nNenhuma nota com origem casando '{args.obra}'.")
        return 1
    obras = Counter(o for _p, o in achados)
    print(f"\n{len(achados)} nota(s) casam '{args.obra}':")
    for obra, n in obras.most_common():
        print(f"  {n:6d}  {obra[:80]}")
    if len(obras) > 1 and not args.varias:
        # Duas ingestões da mesma obra viram títulos DIFERENTES ("…(AMABIS E
        # MARTHO)" e "…(Amabis e Martho)"). Juntá-las é legítimo; fazê-lo por
        # acidente, não — daí o --varias explícito.
        print("\nATENÇÃO: o filtro pegou MAIS DE UMA obra. Refine, ou passe --varias.")
        if args.aplicar:
            return 2

    vault = Path(settings.caminho_obsidian)
    destino = Path(args.para) if args.para else Path("dados/arquivo_obras") / livro_mod.slug(
        args.obra if len(obras) > 1 else next(iter(obras)), max_len=60)
    print(f"\ndestino: {destino}")
    if not args.aplicar:
        for p, _o in achados[:5]:
            print(f"  {p.relative_to(vault).as_posix()}  ->  {destino.name}/…")
        print("\n(dry-run: NADA foi movido)")
        return 0

    movidas = 0
    for p, _o in achados:
        alvo = destino / p.relative_to(vault)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(alvo))
        movidas += 1
    print(f"{movidas} nota(s) movidas.")
    print("Rode `python scripts/reindexar.py` — a purga de órfãos tira do índice o que saiu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
