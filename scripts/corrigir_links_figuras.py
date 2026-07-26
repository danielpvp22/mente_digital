"""Conserta os wikilinks de figura que apontam para o lugar errado.

O defeito (medido em 2026-07-25): os jobs foram gerados quando as figuras iam
soltas em `Figuras/`, e o extrator passou a gravar UMA PASTA POR LIVRO
(`Figuras/<livro>/`). Ninguem reescreveu os atomos ja salvos — resultado: 800 de
800 wikilinks unicos apontando para arquivo inexistente.

A correcao e mecanica e reversivel (git): `Figuras/x.webp` -> `Figuras/<slug>/x.webp`,
com o slug tirado do PROPRIO nome do arquivo. So reescreve quando o destino
corrigido EXISTE em disco — link cujo arquivo nunca foi extraido (Cervantes, cujas
figuras a guarda do escaneado apagou) fica como esta, para nao trocar um link
quebrado por outro.

Uso:
    python scripts/corrigir_links_figuras.py --dry-run
    python scripts/corrigir_links_figuras.py
"""
import argparse
import os
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital.config import settings  # noqa: E402
from mente_digital.figuras_recorte import corrigir_alvo  # noqa: E402

_LINK_RE = re.compile(r"!\[\[([^\]|#]+?)\]\]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="so relata")
    args = ap.parse_args()

    vault = Path(settings.caminho_obsidian)
    sub = settings.subpasta_figuras
    arquivos = corrigidos = pulados = 0
    for md in vault.rglob("*.md"):
        try:
            texto = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f"[[{sub}/" not in texto:
            continue
        mudou = False

        def _troca(m: re.Match) -> str:
            nonlocal mudou, corrigidos, pulados
            novo = corrigir_alvo(m.group(1), sub)
            if novo is None:
                return m.group(0)
            if not (vault / novo).is_file():
                pulados += 1          # arquivo nunca extraido: nao piora o link
                return m.group(0)
            mudou = True
            corrigidos += 1
            return f"![[{novo}]]"

        novo_texto = _LINK_RE.sub(_troca, texto)
        if mudou:
            arquivos += 1
            if not args.dry_run:
                md.write_text(novo_texto, encoding="utf-8")

    verbo = "corrigiria" if args.dry_run else "corrigiu"
    print(f"{verbo} {corrigidos} link(s) em {arquivos} nota(s); "
          f"{pulados} pulado(s) por o arquivo nao existir em disco.")
    if not args.dry_run and corrigidos:
        print("Rode `python scripts/reindexar.py` para a busca ver o texto novo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
