"""
Limpeza pontual do vault atomizado: remove o lixo MEDIDO da base auto-gerada.

Não é o importador (que regeneraria tudo) — é um tratamento in-place da base atual,
para a decisão de "usar esta base e fazê-la funcionar bem". Três classes, todas
auditadas antes de escrever isto (12.722 átomos auto-gerados):

  NADA          — o sentinela "nada a extrair" vazou por bloco: o check do importador
                  só pega quando a saída INTEIRA é NADA, então uma síntese com átomos
                  bons + um bloco NADA salvava '## Assunto: NADA' com corpo 'NADA'.
  corpo vazio   — átomo só com título/Malha, sem fato (linha de Malha solta, stub).
  clone exato   — corpo byte-a-byte idêntico entre conversas; o Deduplicador roda por
                  lote e semeia do disco, mas clones cross-run escaparam. Mantém 1.

SEGURANÇA:
  - Só toca em `Importado_Gemini/` (auto-gerado). Nota escrita à mão (CPU-GPU.md) nunca.
  - NÃO deleta: MOVE para `Importado_Gemini/_lixo_purgado/` — reversível.
  - `--executar` para agir; sem ele, dry-run (só conta e mostra).

Uso:
    python scripts/limpar_vault.py            # dry-run
    python scripts/limpar_vault.py --executar
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mente_digital import textutils  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.rag import strip_frontmatter  # noqa: E402

AUTO = os.path.join(settings.caminho_obsidian, "Importado_Gemini")
# FORA do vault: o sync faz glob recursivo `**/*.md`, então um backup DENTRO do vault
# seria re-indexado e a limpeza se anularia (bug medido: os 38 voltaram ao Chroma).
LIXO = os.path.join(os.path.dirname(settings.caminho_obsidian), "_lixo_purgado")


def corpo_puro(txt: str) -> str:
    """O texto do átomo sem título, Malha nem tags — o fato em si."""
    t = strip_frontmatter(txt)
    linhas = [
        ln for ln in t.splitlines()
        if ln.strip()
        and not ln.lstrip().startswith("#")
        and not ln.startswith("**Malha")
        and not ln.strip().startswith("#zettel")
    ]
    return " ".join(linhas).strip()


def classificar() -> dict:
    """Varre a base auto-gerada e rotula cada arquivo. Puro de efeitos (só lê)."""
    arqs = glob.glob(os.path.join(AUTO, "**/*.md"), recursive=True)
    nada, vazio, chines, corpos = [], [], [], {}
    for p in arqs:
        try:
            c = corpo_puro(open(p, encoding="utf-8").read())
        except OSError:
            continue
        cn = textutils.normaliza(c)
        if cn == "nada":
            nada.append(p)
        elif cn == "":
            vazio.append(p)
        elif textutils.fracao_cjk(c) > 0.15:
            # Idioma errado: síntese em chinês. Nunca serve a pergunta em PT.
            chines.append(p)
        else:
            corpos.setdefault(hashlib.md5(cn.encode()).hexdigest(), []).append(p)
    # Clone exato: ordena por nome e mantém o 1º (determinístico, sem Date/random).
    dup = []
    for ps in corpos.values():
        if len(ps) > 1:
            dup.extend(sorted(ps)[1:])
    return {"NADA": nada, "corpo vazio": vazio, "idioma errado (CJK)": chines,
            "clone exato": dup, "total_base": len(arqs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--executar", action="store_true", help="move de fato (senão dry-run)")
    args = ap.parse_args()

    r = classificar()
    categorias = ("NADA", "corpo vazio", "idioma errado (CJK)", "clone exato")
    remover = [p for k in categorias for p in r[k]]
    print(f"base auto-gerada: {r['total_base']} átomos")
    for k in categorias:
        print(f"  {k:20}: {len(r[k])}")
    print(f"  {'TOTAL':20}: {len(remover)}")

    if not args.executar:
        print("\n[dry-run] nada movido. Rode com --executar para agir.")
        return

    os.makedirs(LIXO, exist_ok=True)
    movidos = 0
    for p in remover:
        destino = os.path.join(LIXO, os.path.basename(p))
        # Colisão de nome no lixo: sufixa para não sobrescrever evidência.
        i = 1
        while os.path.exists(destino):
            base, ext = os.path.splitext(os.path.basename(p))
            destino = os.path.join(LIXO, f"{base}__{i}{ext}")
            i += 1
        try:
            shutil.move(p, destino)
            movidos += 1
        except OSError as exc:
            print(f"  falha ao mover {os.path.basename(p)}: {exc}")
    print(f"\n{movidos} átomos movidos para {LIXO}")
    print("Reversível: os arquivos estão lá. Rode o sync depois (o índice muda).")


if __name__ == "__main__":
    main()
