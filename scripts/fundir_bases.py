"""Funde a edição ANTIGA dentro da NOVA, enriquecendo-a (ordem do dono).

Contexto: a Cannabis Encyclopedia foi re-atomizada com o 8B, e três testes cegos
deram a base ANTIGA como melhor (17 a 10) — sempre por um motivo só: as
respostas da base nova perdiam o DADO DURO ("5 a 14 dias" de secagem, "±0,5
ponto" de ajuste de pH, "6 a 9 semanas" até a colheita). Ao fatiar mais fino, o
modelo separou o número do seu contexto.

Em vez de escolher uma base e apagar a outra, o dono pediu FUSÃO: a nova é a
espinha dorsal, a antiga entra dentro dela, e contradição resolve a favor da
nova. Um átomo antigo pode corresponder a dois ou três novos — enriquecemos o
MAIS PRÓXIMO, não todos, para não replicar o mesmo fato pela base inteira.

O gate é LÉXICO e barato (`fusao.vale_enriquecer`): átomo antigo cujos números
já estão no par novo não paga chamada de GPU. Sem ele seriam 5.907 chamadas.

    python scripts/fundir_bases.py --so-triagem
    python scripts/fundir_bases.py --dry-run --limite 15
    python scripts/fundir_bases.py --aplicar
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import sys
import textwrap
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import fusao, idioma, prompts, rag, reparo  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.llm import LlamaManager, preparar_offline  # noqa: E402

ANTIGA = RAIZ / "dados/arquivo_obras/encyclopedia_pre8b"
K_VIZINHOS = 3          # quantos átomos novos considerar como "o mesmo assunto"
# DISTÂNCIA máxima para considerar "a mesma ideia dita de outro jeito". Não é
# chute: a medição do projeto pôs os pares de paráfrase entre 0,042 e 0,128
# (mediana 0,088) — é a mesma faixa que calibrou `obras_dedup_dist_max=0,08`.
# Comecei com 0,45 (similaridade 0,55) e a triagem pareou "1 a 1,5 galão de solo
# por mês de vida da planta" com "Limpeza do Solo com Solução Nutricional", que é
# LIXIVIAÇÃO — outra ideia. Fundir aquilo corromperia o átomo novo.
DIST_MAX = 0.15
MAX_TOKENS_FUSAO = 420


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


def _ler_antigos() -> List[Tuple[Path, str, str]]:
    """(caminho, titulo, corpo) dos átomos da edição antiga."""
    fora = []
    for md in sorted(ANTIGA.rglob("*.md")):
        try:
            texto = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        cat, a = reparo.triar(texto)
        if not a.titulo or cat == reparo.FIGURA or not a.corpo.strip():
            continue
        if a.titulo.startswith("Síntese"):
            continue          # nota-síntese é resumo de capítulo, não átomo
        fora.append((md, a.titulo, a.corpo))
    return fora


async def _vizinhos(vs, consulta: str):
    """[(distância, caminho, texto)] da base NOVA, mais próximos primeiro."""
    def _busca():
        return vs._store.similarity_search_with_score(consulta, k=K_VIZINHOS)
    try:
        res = await asyncio.to_thread(_busca)
    except Exception:
        return []
    fora = []
    for doc, dist in res:
        fonte = (doc.metadata or {}).get("source")
        if not fonte:
            continue
        fora.append((float(dist), Path(fonte), doc.page_content))
    return fora


async def main_async(args) -> int:
    antigos = _ler_antigos()
    print(f"{len(antigos)} átomo(s) na edição antiga")
    if _servidor_no_ar():
        print("O servidor está NO AR — os dois disputariam a GPU. Feche-o.")
        return 3

    emb = rag.EmbeddingProvider()
    await asyncio.to_thread(emb.load)
    vs = rag.VectorStore(emb)
    await vs.open()
    if not vs.ready:
        print("Chroma não abriu.")
        return 3

    motivos: Counter = Counter()
    candidatos = []
    orfaos: List[Tuple[Path, str]] = []      # (caminho, titulo) sem par na base nova
    t0 = time.perf_counter()
    for i, (caminho, titulo, corpo) in enumerate(antigos, 1):
        viz = [v for v in await _vizinhos(vs, f"{titulo}\n{corpo}") if v[0] <= DIST_MAX]
        vale, motivo = fusao.vale_enriquecer(f"{titulo} {corpo}", [t for _d, _p, t in viz])
        # Agrupa pela CLASSE do motivo: "37 palavras de conteúdo ausentes" e "48
        # palavras…" são o mesmo caso, e como chaves distintas viravam 200 linhas
        # de relatório com contagem 1 cada.
        chave = ("dado duro ausente" if motivo.startswith("dado duro")
                 else "palavras de conteúdo ausentes" if "palavras" in motivo else motivo)
        motivos[chave] += 1
        if vale:
            candidatos.append((caminho, titulo, corpo, viz))
        elif not viz:
            orfaos.append((caminho, titulo))
        if i % 500 == 0:
            print(f"  triados {i}/{len(antigos)} ({time.perf_counter()-t0:.0f}s)", flush=True)

    print(f"\ntriagem em {time.perf_counter()-t0:.0f}s:")
    for m, n in motivos.most_common():
        print(f"  {n:6d}  {m}")
    print(f"\ncandidatos a enriquecer: {len(candidatos)}")

    # ÓRFÃOS: átomo antigo sem NENHUM vizinho dentro da faixa de paráfrase. Não é
    # caso de fusão — é ideia que a re-atomização pode ter perdido inteira, e o
    # dono mandou preservá-la (2026-07-28). Move para a pasta da obra no vault:
    # a `origem` deles é a mesma âncora de página, então as figuras continuam
    # ligadas e o dedup do ETL não os vê (não passam por _salvar_atomos).
    if args.adotar_orfaos and orfaos:
        destino = Path(settings.dir_conhecimento_novo) / args.pasta_obra
        destino.mkdir(parents=True, exist_ok=True)
        movidos = colididos = 0
        for caminho, _titulo in orfaos:
            alvo = destino / caminho.name
            if alvo.exists():
                colididos += 1
                continue
            if args.aplicar:
                shutil.move(str(caminho), str(alvo))
            movidos += 1
        verbo = "adotaria" if not args.aplicar else "adotou"
        print(f"{verbo} {movidos} órfão(s) em {destino}"
              + (f" ({colididos} com nome já existente, pulados)" if colididos else ""))
    if args.so_orfaos:
        return 0
    if args.so_triagem:
        for caminho, titulo, corpo, viz in candidatos[:args.amostra]:
            print(f"\n  ANTIGO ## {titulo}\n     {corpo[:150]}")
            print(f"  NOVO (dist={viz[0][0]:.3f}) {viz[0][2][:150]}")
        return 0

    alvos = candidatos[: args.limite] if args.limite else candidatos
    backup = None
    if args.aplicar:
        backup = Path("dados/backups") / f"fusao_{datetime.now():%Y%m%d_%H%M%S}"
        backup.mkdir(parents=True, exist_ok=True)
        print(f"backup dos originais em: {backup}")

    llama = LlamaManager(preparar_offline(settings.caminho_modelo_atomizacao))
    await llama.load()
    feitos = 0
    recusas: Counter = Counter()
    contradicoes: List[str] = []
    mostrados = 0
    t0 = time.perf_counter()
    try:
        for caminho, titulo, corpo, viz in alvos:
            _dist, alvo_path, _texto = viz[0]
            try:
                bruto = alvo_path.read_text(encoding="utf-8")
            except OSError:
                recusas["átomo novo sumiu do disco"] += 1
                continue
            atomo = reparo.separar(bruto)
            if not atomo.titulo:
                recusas["átomo novo sem título"] += 1
                continue
            saida = await llama.collect(
                prompts.prompt_fundir_enriquecendo(atomo.corpo, atomo.titulo, corpo),
                system_prompt=prompts.SYS_SINTESE,
                max_tokens=MAX_TOKENS_FUSAO,
            )
            if fusao.nao_e_a_mesma_ideia(saida):
                recusas["o par não é a mesma ideia"] += 1
                continue
            if fusao.houve_contradicao(saida):
                contradicoes.append(f"{atomo.titulo} <- {titulo}")
                recusas["contradição (fica só o novo)"] += 1
                continue
            fundido = fusao.limpar_fusao(saida)
            fundido, _n = idioma.aplicar_correcoes(fundido, corpo)
            ok, motivo = reparo.corpo_aceitavel(fundido, atomo)
            if not ok:
                recusas[motivo] += 1
                continue
            if not fusao.preserva_o_novo(atomo.corpo, fundido):
                recusas["a fusão apagaria dado do átomo novo"] += 1
                continue
            if mostrados < args.amostra:
                mostrados += 1
                print(f"\n  ## {atomo.titulo}")
                for rot, txt in (("novo   ", atomo.corpo), ("antigo ", corpo),
                                 ("FUNDIDO", fundido)):
                    linhas = textwrap.wrap(txt.replace("\n", " "), 92) or [""]
                    print(f"     {rot}: {linhas[0]}")
                    for extra in linhas[1:]:
                        print(f"              {extra}")
            if args.aplicar:
                (backup / alvo_path.name).write_text(bruto, encoding="utf-8")
                alvo_path.write_text(reparo.montar(atomo, fundido), encoding="utf-8")
            feitos += 1
    finally:
        await llama.unload()

    dt = time.perf_counter() - t0
    print(f"\n{'enriqueceria' if args.dry_run else 'enriqueceu'} {feitos} átomo(s) "
          f"em {dt:.0f}s ({dt/max(1, feitos):.1f}s cada)")
    if recusas:
        print("\nnão fundidos:")
        for m, n in recusas.most_common():
            print(f"  {n:5d}  {m}")
    if contradicoes:
        destino = Path("dados/contradicoes_fusao.md")
        destino.write_text("# Contradições entre as duas edições\n\n"
                           + "\n".join(f"- {c}" for c in contradicoes) + "\n",
                           encoding="utf-8")
        print(f"\n{len(contradicoes)} contradição(ões) listadas em {destino} "
              "(ficou o átomo NOVO; revisão sua)")
    if args.dry_run:
        print("\n(dry-run: NADA foi escrito)")
    elif feitos:
        print("\nRode `python scripts/reindexar.py`.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--so-triagem", action="store_true", help="só conta (sem GPU)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--amostra", type=int, default=8)
    ap.add_argument("--adotar-orfaos", action="store_true",
                    help="move para o vault os átomos antigos SEM par na base nova")
    ap.add_argument("--pasta-obra", default="the-cannabis-encyclopedia-jorge-cervante",
                    help="subpasta da obra no Conhecimento_Novo (destino dos órfãos)")
    ap.add_argument("--so-orfaos", action="store_true",
                    help="só a adoção dos órfãos; não carrega o LLM")
    args = ap.parse_args()
    if not (args.so_triagem or args.so_orfaos or args.dry_run or args.aplicar):
        ap.error("escolha --so-triagem, --so-orfaos, --dry-run ou --aplicar")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
