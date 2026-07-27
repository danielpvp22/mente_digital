"""Qualidade dos átomos: os NOVOS (do livro) contra os que já estavam no vault.

Pergunta que este arquivo responde (pedido do dono, 2026-07-25): a ingestão do
livro produziu átomos MELHORES ou PIORES do que os ~13k que já existiam (import
do Gemini, conversas, colheita web)? Sem isso, "2.038 átomos novos" é volume, não
qualidade — e volume ruim envenena o RAG para sempre.

DETERMINÍSTICO de propósito: nada de LLM-juiz. As métricas abaixo são de FORMA e
INTEGRIDADE, checáveis em segundos e reproduzíveis — dá para rodar de novo depois
de mudar um prompt e comparar. O que elas NÃO medem é se a ideia está correta;
isso exigiria julgamento humano ou um eval semântico, e está fora do escopo aqui.

Uso:
    python eval/qualidade_atomos.py                 # todos os grupos
    python eval/qualidade_atomos.py --amostra 3     # + exemplos dos piores casos
"""
import argparse
import hashlib
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital.config import settings  # noqa: E402
from mente_digital import prompts  # noqa: E402

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_ORIGEM = re.compile(r"^origem:\s*(.+)$", re.M)
_TITULO = re.compile(r"^##\s+(.+)$", re.M)
_MALHA_CANONICA = re.compile(r"^\*\*Malha Neural:\*\*", re.M)
_MALHA_QUALQUER = re.compile(r"^\*\*[^*]{2,40}:\*\*\s*(.*)$", re.M)
_WIKILINK = re.compile(r"\[\[([^\]|]+)")
# Título que não se sustenta sozinho: genérico, ou fragmento continuado.
_TITULO_FRACO = re.compile(
    r"^(introdu|conclus|resumo|continua|观|sobre o|parte \d|cap[ií]tulo \d|"
    r"considera|no[tç]a|texto|trecho)", re.I)


def grupo_de(origem: str) -> str:
    """Rótulo comparável a partir do frontmatter de proveniência."""
    if not origem:
        return "sem-origem"
    if origem.startswith("Livro '"):
        return "LIVRO (novo)"
    if origem.startswith("Consolida"):
        return "consolidado"
    if origem.endswith(".json") or ".json" in origem:
        return "import Gemini"
    return origem.split()[0][:18]


def metricas(texto: str) -> dict:
    """Métricas de FORMA de um átomo. Puro."""
    corpo = _FRONT.sub("", texto).strip()
    titulos = _TITULO.findall(corpo)
    titulo = titulos[0].strip() if titulos else ""
    ideia = _TITULO.sub("", corpo)
    ideia = re.sub(r"^\*\*[^*]+:\*\*.*$", "", ideia, flags=re.M)
    ideia = re.sub(r"#\S+", "", ideia).strip()
    return {
        "tem_titulo": bool(titulo),
        "titulo_forte": bool(titulo) and not _TITULO_FRACO.match(titulo)
                        and len(titulo.split()) >= 2 and titulo[:1].isupper(),
        "malha_canonica": bool(_MALHA_CANONICA.search(corpo)),
        "malha_qualquer": bool(_MALHA_QUALQUER.search(corpo)),
        "tem_wikilink": bool(_WIKILINK.search(corpo)),
        "tag_atomo": prompts.TAG_ATOMO in corpo,
        "tem_proveniencia": bool(_FRONT.search(texto) and _ORIGEM.search(texto)),
        "chars_ideia": len(ideia),
        "hash_ideia": hashlib.md5(re.sub(r"\s+", " ", ideia.lower()).encode(),
                                  usedforsecurity=False).hexdigest(),
        "titulo": titulo,
        "alvos": [a.strip() for a in _WIKILINK.findall(corpo)],
    }


def _pct(n: int, tot: int) -> str:
    return f"{100*n/tot:5.1f}%" if tot else "    - "


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--amostra", type=int, default=0, help="mostra N exemplos ruins por grupo")
    ap.add_argument("--auditoria", action="store_true", help="gera .md revisavel com quem esta fora do padrao")
    args = ap.parse_args()

    vault = Path(settings.caminho_obsidian)
    por_grupo = defaultdict(list)
    titulos_existentes = set()
    for f in vault.rglob("*.md"):
        try:
            t = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = metricas(t)
        m["arquivo"] = f.name
        og = _ORIGEM.search(t)
        por_grupo[grupo_de(og.group(1).strip() if og else "")].append(m)
        if m["titulo"]:
            titulos_existentes.add(m["titulo"].lower())

    cab = (f"{'grupo':16} {'n':>6} {'título':>7} {'t.forte':>8} {'malhaOK':>7} "
           f"{'wiki':>6} {'tag':>6} {'prov':>6} {'chars':>6} {'dup':>6} {'orfao':>6}")
    print(cab)
    print("-" * len(cab))
    for grupo, itens in sorted(por_grupo.items(), key=lambda kv: -len(kv[1])):
        n = len(itens)
        if n < 20:
            continue
        hashes = Counter(i["hash_ideia"] for i in itens)
        dups = sum(v - 1 for v in hashes.values() if v > 1)
        orfaos = sum(1 for i in itens for a in i["alvos"]
                     if a.lower() not in titulos_existentes)
        alvos = sum(len(i["alvos"]) for i in itens) or 1
        print(f"{grupo[:16]:16} {n:6} "
              f"{_pct(sum(i['tem_titulo'] for i in itens), n)} "
              f"{_pct(sum(i['titulo_forte'] for i in itens), n)} "
              f"{_pct(sum(i['malha_canonica'] for i in itens), n)} "
              f"{_pct(sum(i['tem_wikilink'] for i in itens), n)} "
              f"{_pct(sum(i['tag_atomo'] for i in itens), n)} "
              f"{_pct(sum(i['tem_proveniencia'] for i in itens), n)} "
              f"{int(statistics.median(i['chars_ideia'] for i in itens)):6} "
              f"{_pct(dups, n)} {_pct(orfaos, alvos)}")

    print("\ntítulo = tem '## ' | t.forte = título auto-contido (não 'Introdução'/'Parte 2')")
    print("malhaOK = usa o rótulo canônico '**Malha Neural:**' | wiki = tem [[link]]")
    print("dup = ideias idênticas dentro do grupo | orfao = wikilink sem átomo de destino")
    print("NÃO mede se a ideia está CORRETA — isso pede julgamento humano.")

    if args.auditoria:
        # RELATÓRIO REVISÁVEL (pedido do dono, 2026-07-25): quem está fora do padrão,
        # com a PROVENIÊNCIA ao lado — é o frontmatter que diz de onde o átomo veio
        # (livro/capítulo/página, ou o .json da conversa importada), então revisar é
        # abrir a fonte e conferir. Um .md para ler no próprio Obsidian.
        alvo = Path("eval/saidas/atomos_fora_do_padrao.md")
        alvo.parent.mkdir(parents=True, exist_ok=True)
        problemas = {
            "Título fraco (genérico ou fragmento)": lambda m: not m["titulo_forte"],
            "Sem rótulo canônico da Malha": lambda m: not m["malha_canonica"],
            "Sem wikilink algum": lambda m: not m["tem_wikilink"],
            "Sem proveniência no frontmatter": lambda m: not m["tem_proveniencia"],
        }
        linhas = ["# Átomos fora do padrão", "",
                  "Gerado por `eval/qualidade_atomos.py --auditoria`. A coluna ORIGEM é o",
                  "frontmatter do próprio átomo — é por ela que se chega à fonte (página do",
                  "livro, arquivo da conversa importada).", ""]
        for rotulo, teste in problemas.items():
            afetados = [(g, i) for g, itens in por_grupo.items() for i in itens if teste(i)]
            linhas += [f"## {rotulo} — {len(afetados)}", ""]
            for g, i in afetados[:200]:
                linhas.append(f"- `{i['arquivo']}` · **{g}** · título: {i['titulo'][:60]!r}")
            if len(afetados) > 200:
                linhas.append(f"- … e outros {len(afetados)-200}")
            linhas.append("")
        alvo.write_text("\n".join(linhas), encoding="utf-8")
        print(f"\nRelatório: {alvo}")

    if args.amostra:
        for grupo, itens in sorted(por_grupo.items(), key=lambda kv: -len(kv[1])):
            ruins = [i for i in itens if not i["titulo_forte"] or not i["malha_canonica"]]
            if not ruins or len(itens) < 20:
                continue
            print(f"\n--- piores de '{grupo}' ({len(ruins)} de {len(itens)}):")
            for i in ruins[: args.amostra]:
                print(f"  {i['arquivo'][:52]:54} título={i['titulo'][:40]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
