"""Correção retroativa da Malha Neural nos átomos já salvos.

Por que existe: o `eval/qualidade_atomos.py` mediu 67% de rótulo canônico nos 2.038
átomos do Amabis contra 99,1% no import antigo — em ~670 deles o modelo improvisou
o rótulo ('**Divisão binária:** [[Reprodução asexuada]]'), a linha não casava o
regex da Malha e o átomo ficava fora do grafo de conceitos. O `normalizar_atomo`
foi consertado para impor a forma; este script aplica o mesmo conserto ao que já
está em disco.

Só REFORMATA — nunca reescreve a ideia. É o `normalizar_atomo` rodando de novo, e
ele é idempotente por contrato (átomo já normalizado volta igual). Atomo sem
NENHUM colchete não é inventado: fabricar um wikilink a partir do título criaria um
auto-link inútil e poluiria a malha com ruído. Esses ficam como estão, contados no
relatório.

Uso:
    python scripts/normalizar_malha_vault.py            # DRY-RUN (não escreve)
    python scripts/normalizar_malha_vault.py --aplicar  # grava
"""
import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital.atomos import normalizar_atomo  # noqa: E402
from mente_digital.config import settings  # noqa: E402

_ORIGEM = re.compile(r"^origem:\s*(.+)$", re.M)
_COLHIDO = re.compile(r"^colhido_em:\s*(.+)$", re.M)
_MALHA_OK = re.compile(r"^\*\*Malha Neural:\*\*", re.M)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aplicar", action="store_true", help="grava (sem isto é dry-run)")
    ap.add_argument("--filtro", default="", help="só átomos cuja origem contenha isto")
    args = ap.parse_args()

    vault = Path(settings.caminho_obsidian)
    total = fora = corrigidos = sem_link = inalterados = 0
    exemplos = []
    for f in sorted(vault.rglob("*.md")):
        try:
            texto = f.read_text(encoding="utf-8")
        except OSError:
            continue
        og = _ORIGEM.search(texto)
        origem = og.group(1).strip() if og else ""
        if args.filtro and args.filtro.lower() not in origem.lower():
            continue
        total += 1
        if _MALHA_OK.search(texto):
            continue                      # já canônico
        fora += 1
        if "[" not in texto:
            sem_link += 1                 # nada a canonizar: não se inventa link
            continue
        # Preserva a data original (o átomo não foi colhido hoje de novo).
        cd = _COLHIDO.search(texto)
        try:
            quando = datetime.fromisoformat(cd.group(1).strip()) if cd else datetime.now()
        except ValueError:
            quando = datetime.now()
        novo = normalizar_atomo(texto, origem or "Desconhecida", quando)
        if not novo or not _MALHA_OK.search(novo):
            inalterados += 1
            continue
        if len(exemplos) < 3:
            antes = next((ln for ln in texto.splitlines()
                          if ln.startswith("**") and "[" in ln), "(sem linha de rótulo)")
            exemplos.append((f.name, antes.strip()[:70]))
        corrigidos += 1
        if args.aplicar:
            f.write_text(novo, encoding="utf-8")

    modo = "APLICADO" if args.aplicar else "DRY-RUN (nada escrito)"
    print(f"{modo}\n  analisados        {total}")
    print(f"  fora do canônico  {fora}")
    print(f"  CORRIGIDOS        {corrigidos}")
    print(f"  sem link nenhum   {sem_link}   (não se inventa wikilink)")
    print(f"  sem conserto      {inalterados}")
    for nome, antes in exemplos:
        print(f"    ex: {nome[:46]:48} {antes}")
    if not args.aplicar and corrigidos:
        print("\nRode de novo com --aplicar para gravar.")
    if args.aplicar and corrigidos:
        print("\nO vault mudou: o próximo sync do idle reindexa (mtime novo).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
