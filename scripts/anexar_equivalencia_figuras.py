"""
Torna ENCONTRÁVEL a figura que o vault descreve com OUTRO nome (2026-07-31).

O caso que originou: o dono perguntou "o que são tricomas?" e nenhuma imagem
apareceu, embora o acervo tenha 41 figuras do assunto. Medido: **uma** delas diz
"tricoma" em português; as outras 40 dizem "glândulas resiníferas", que é como
Jorge Cervantes chama a estrutura no livro. Nenhuma busca por embedding parte da
palavra do dono e chega nessas 40 — e a legenda é todo o texto que a figura tem.

O que este script faz: acrescenta o termo do usuário como WIKILINK na Malha Neural
da nota, usando o texto ORIGINAL EM INGLÊS (`legenda_original`) como PROVA de que
a figura é mesmo daquele assunto. Nada é reescrito: o termo do autor continua lá.
É a mesma régua do `corrigir_traducoes.py` — a prova vem do inglês da página, não
de um palpite sobre a palavra portuguesa.

POR QUE ACRESCENTAR E NÃO TROCAR (o dono ofereceu as duas saídas): "glândula
resinífera" é português correto e é o termo do autor; trocar apagaria a palavra
que faz a nota casar com o resto do capítulo, e a régua desta base já foi paga
caro — só se substitui o que NÃO é palavra portuguesa em contexto nenhum.

Isto é o conserto dos DADOS para um assunto. O conserto do MECANISMO, que vale
para todo assunto sem tabela nenhuma, é a co-locação por página em
`rag._anexos_colocados`: se o átomo da página respondeu, a figura da página
aparece mesmo sem repetir a palavra da pergunta.

    # o que mudaria (não escreve nada)
    python scripts/anexar_equivalencia_figuras.py --alvo Tricomas \
        --prova-en "resin gland,capitate,trichome,cystolith" \
        --excluir-en "trichome technolog"

    # aplica
    python scripts/anexar_equivalencia_figuras.py ... --aplicar

Depois de aplicar, rode `python scripts/reindexar.py` (passada incremental por
mtime) para que a mudança chegue ao índice.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mente_digital import textutils  # noqa: E402
from mente_digital.config import settings  # noqa: E402

_MALHA_RE = re.compile(r"^\*\*Malha Neural:\*\*\s*(.*)$", re.MULTILINE)
_LEGENDA_ORIG_RE = re.compile(r"^legenda_original:\s*(.+)$", re.MULTILINE)


def notas_de_figura(raiz: Path) -> List[Path]:
    pasta = raiz / settings.subpasta_figuras
    return sorted(pasta.rglob("*.md")) if pasta.is_dir() else []


def escolher(texto: str, provas: List[str], excluir: List[str],
             alvo: str) -> Optional[str]:
    """A nota trata do assunto? Devolve a prova que casou, ou None. Puro/testável.

    A prova é lida do INGLÊS (`legenda_original`), porque é o texto que o autor
    escreveu — a tradução é justamente o que variou. Duas recusas:
      - `excluir`: o termo aparece, mas como nome próprio ("Trichome Technologies"
        é uma empresa de jardins hidropônicos, não uma foto de tricoma);
      - nota que JÁ diz o alvo em português não ganha nada com o link.
    """
    m = _LEGENDA_ORIG_RE.search(texto)
    if not m:
        return None
    en = m.group(1).lower()
    if any(x and x in en for x in excluir):
        return None
    prova = next((p for p in provas if p and p in en), None)
    if not prova:
        return None
    raiz_alvo = textutils.sem_acento(alvo.lower())[:6]
    if raiz_alvo and raiz_alvo in textutils.sem_acento(texto.lower()):
        return None                       # já encontrável pelo termo do usuário
    return prova


def acrescentar_link(texto: str, alvo: str) -> Tuple[str, bool]:
    """Põe `[[alvo]]` na Malha Neural da nota. Puro. (texto_novo, mudou?)

    A Malha é uma linha só e faz parte do corpo indexado — então o wikilink é ao
    mesmo tempo navegação no Obsidian e texto pesquisável, que é o que faz a
    figura passar a existir para o aterramento léxico.
    """
    link = f"[[{alvo}]]"
    if link in texto:
        return texto, False
    m = _MALHA_RE.search(texto)
    if m:
        linha = m.group(0).rstrip()
        return texto[: m.start()] + f"{linha} {link}" + texto[m.end():], True
    # Sem Malha (acontece nas notas antigas): cria a linha antes das tags, que são
    # sempre a última linha do arquivo no formato deste vault.
    linhas = texto.rstrip().splitlines()
    if linhas and linhas[-1].lstrip().startswith("#"):
        linhas.insert(len(linhas) - 1, f"**Malha Neural:** {link}")
    else:
        linhas.append(f"**Malha Neural:** {link}")
    return "\n".join(linhas) + "\n", True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--alvo", required=True,
                    help="termo do usuário, vira [[Wikilink]] (ex.: Tricomas)")
    ap.add_argument("--prova-en", required=True,
                    help="termos do INGLÊS que provam o assunto, separados por vírgula")
    ap.add_argument("--excluir-en", default="",
                    help="termos do inglês que REPROVAM (nome próprio, homônimo)")
    ap.add_argument("--so-notas", default="",
                    help="lista de sufixos de nota (p0054_f5,p0062_f1) — só estas "
                         "entram, mesmo que a prova case em outras")
    ap.add_argument("--aplicar", action="store_true",
                    help="sem isto, só mostra o que mudaria")
    args = ap.parse_args()

    provas = [p.strip().lower() for p in args.prova_en.split(",") if p.strip()]
    excluir = [p.strip().lower() for p in args.excluir_en.split(",") if p.strip()]

    raiz = Path(settings.caminho_obsidian)
    notas = notas_de_figura(raiz)
    print(f"acervo    : {len(notas)} notas de figura em {raiz / settings.subpasta_figuras}")
    print(f"alvo      : [[{args.alvo}]]")
    print(f"prova (EN): {provas}")
    print(f"exclui    : {excluir or '(nada)'}\n")

    # LISTA JULGADA. A prova léxica diz que a legenda FALA do assunto; ela não sabe
    # do que a foto É. Medido em 2026-07-31, abrindo as 36 imagens que a regex
    # aprovou: `p0063_f8` ("Look at resin glands right on the plant") é a foto de um
    # senhor com uma lupa na mão, e `p0289_f3` ("capture resin glands using a smaller
    # mesh sieve"), que parecia foto de peneira, é uma macro impecável da folha
    # coberta de glândulas. Legenda engana nos dois sentidos — por isso a seleção
    # final é JULGADA (por quem vê a imagem) e entra por aqui, e o script continua
    # servindo para qualquer assunto.
    so = {s.strip().lower() for s in args.so_notas.split(",") if s.strip()}

    escolhidas, ja_ok, fora_da_lista = [], 0, 0
    for nota in notas:
        if so and not any(s in nota.name.lower() for s in so):
            fora_da_lista += 1
            continue
        try:
            texto = nota.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"  ! ilegível: {nota.name} ({exc})")
            continue
        prova = escolher(texto, provas, excluir, args.alvo)
        if not prova:
            continue
        novo, mudou = acrescentar_link(texto, args.alvo)
        if not mudou:
            ja_ok += 1
            continue
        escolhidas.append((nota, novo, prova))

    for nota, _novo, prova in escolhidas:
        m = _LEGENDA_ORIG_RE.search(nota.read_text(encoding="utf-8"))
        legenda = (m.group(1)[:88] if m else "")
        print(f"  + {nota.name}")
        print(f"      prova={prova!r} :: {legenda}")

    print(f"\n{len(escolhidas)} nota(s) a mudar · {ja_ok} já tinham o link"
          + (f" · {fora_da_lista} fora da lista julgada" if so else ""))
    if not args.aplicar:
        print("(dry-run — nada foi escrito; use --aplicar)")
        return 0

    escritas = 0
    for nota, novo, _p in escolhidas:
        try:
            nota.write_text(novo, encoding="utf-8")
            escritas += 1
        except OSError as exc:
            print(f"  ! falhou ao escrever {nota.name}: {exc}")
    print(f"{escritas} nota(s) atualizada(s). Rode scripts/reindexar.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
