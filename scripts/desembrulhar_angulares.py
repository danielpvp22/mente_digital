"""Tira o '<...>' que envolve o CORPO dos atomos ja salvos no vault.

O defeito (medido em 2026-07-28 sobre a base inteira, 39.534 .md): o decoder da
ATOMIZACAO devolveu o corpo entre angulares — '<O cultivo exige cinco horas de
sol.>' (2.818 linhas), '<...>.' com a pontuacao fora (101) e, nos atomos que o
lote truncou, so o '<' de abertura (78). Nao e conteudo: e marca de citacao que
o modelo inventa ao copiar o trecho da fonte, e ela entra no embedding, no
aterramento lexico e na FALA do TTS.

A correcao usa A MESMA funcao pura que o `atomos.normalizar_atomo` passou a
aplicar na criacao (`atomos.desembrulhar_angular`) — retroativo e preventivo nao
podem divergir. So mexe em LINHA DE CORPO: frontmatter, cabecalho, bullet de
malha/tag e marcacao de uma palavra ('<think>') ficam intactos.

Os atomos TRUNCADOS sao listados a parte: para eles, tirar o '<' e paliativo — o
conserto de verdade e reescrever a partir do job-fonte.

Uso:
    python scripts/desembrulhar_angulares.py --dry-run
    python scripts/desembrulhar_angulares.py --aplicar
"""
import argparse
import os
import sys
from pathlib import Path
from typing import List, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital.atomos import desembrulhar_angular  # noqa: E402
from mente_digital.config import settings  # noqa: E402


def _linhas_de_corpo(linhas: List[str]) -> List[int]:
    """Indices das linhas que sao CORPO (fora do frontmatter, do cabecalho e dos bullets)."""
    ini = 0
    if linhas and linhas[0].strip() == "---":
        for j in range(1, len(linhas)):
            if linhas[j].strip() == "---":
                ini = j + 1
                break
    idx = []
    for k in range(ini, len(linhas)):
        s = linhas[k].strip()
        if s and not s.startswith(("#", "-", "*", ">")):
            idx.append(k)
    return idx


def corrigir_texto(texto: str) -> Tuple[str, int, int]:
    """-> (texto novo, linhas desembrulhadas, linhas truncadas encontradas)."""
    quebra = "\r\n" if "\r\n" in texto else "\n"
    linhas = texto.splitlines()
    trocadas = truncadas = 0
    for k in _linhas_de_corpo(linhas):
        original = linhas[k]
        novo = desembrulhar_angular(original)
        if novo == original:
            continue
        # Truncado = abriu e nunca fechou (o lote estourou o orcamento de saida).
        if ">" not in original:
            truncadas += 1
        linhas[k] = novo
        trocadas += 1
    if not trocadas:
        return texto, 0, 0
    fim = quebra if texto.endswith(("\n", "\r")) else ""
    return quebra.join(linhas) + fim, trocadas, truncadas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="so relata, nao escreve")
    ap.add_argument("--aplicar", action="store_true", help="escreve as correcoes")
    ap.add_argument("--lista-truncados", default="dados/atomos_truncados.md",
                    help="onde gravar a lista dos atomos truncados (para reescrita)")
    ap.add_argument("--sem-backup", action="store_true",
                    help="nao copia os originais antes de escrever (NAO recomendado)")
    args = ap.parse_args()
    if not (args.dry_run or args.aplicar):
        ap.error("escolha --dry-run ou --aplicar")

    vault = Path(settings.caminho_obsidian)
    # O vault NAO esta no git: sem copia dos originais, 2.991 reescritas nao tem
    # desfazer. Copia so o que muda, preservando o caminho relativo.
    backup = None
    if args.aplicar and not args.sem_backup:
        from datetime import datetime
        backup = Path("dados/backups") / f"desembrulhar_{datetime.now():%Y%m%d_%H%M%S}"
        backup.mkdir(parents=True, exist_ok=True)
        print(f"backup dos originais em: {backup}")
    arquivos = linhas_trocadas = arq_truncados = 0
    truncados: List[Path] = []
    amostra: List[Tuple[str, str, str]] = []
    for md in vault.rglob("*.md"):
        try:
            texto = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "<" not in texto:
            continue
        novo, trocadas, truncadas = corrigir_texto(texto)
        if not trocadas:
            continue
        arquivos += 1
        linhas_trocadas += trocadas
        if truncadas:
            arq_truncados += 1
            truncados.append(md)
        if len(amostra) < 8:
            antes = next((ln.strip() for ln in texto.splitlines() if ln.strip().startswith("<")), "")
            depois = desembrulhar_angular(antes)
            amostra.append((md.name, antes[:120], depois[:120]))
        if args.aplicar:
            if backup is not None:
                destino_bkp = backup / md.relative_to(vault)
                destino_bkp.parent.mkdir(parents=True, exist_ok=True)
                destino_bkp.write_text(texto, encoding="utf-8")
            md.write_text(novo, encoding="utf-8")

    print(f"arquivos com corpo envolto: {arquivos}")
    print(f"linhas desembrulhadas:      {linhas_trocadas}")
    print(f"arquivos TRUNCADOS (sem '>'): {arq_truncados}  <- pedem reescrita pelo job-fonte")
    print("\namostra:")
    for nome, antes, depois in amostra:
        print(f"  {nome}\n    antes : {antes}\n    depois: {depois}")

    if truncados:
        destino = Path(args.lista_truncados)
        destino.parent.mkdir(parents=True, exist_ok=True)
        linhas_md = [
            "# Atomos truncados no meio da frase",
            "",
            f"- **{len(truncados)}** atomos abriram '<' e o '>' nunca veio: o lote",
            "  estourou o orcamento de saida do modelo e a frase ficou pela metade.",
            "- Tirar o '<' e paliativo. O conserto e reescrever a partir do job-fonte",
            "  (`livro.origem_do_job` casa 100% dos atomos com seu job).",
            "",
        ]
        for p in sorted(truncados):
            linhas_md.append(f"- `{p.relative_to(vault).as_posix()}`")
        destino.write_text("\n".join(linhas_md) + "\n", encoding="utf-8")
        print(f"\nlista dos truncados: {destino}")

    if args.dry_run:
        print("\n(dry-run: NADA foi escrito)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
