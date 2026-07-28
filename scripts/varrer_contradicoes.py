"""Varredura RETROATIVA de contradições entre a edição nova e a superada.

O #24 detecta contradição no ato da atomização, mas capado por ciclo
(`contradicao_max_por_ciclo`, 3): é um gotejamento. Depois de importar um livro
inteiro por cima de outro, o que se quer é a passada sobre a base TODA — foi o
pedido do dono em 2026-07-28, e o de antes: "se duas notas dizem o contrário uma
da outra, vale a informação nova; a antiga sai da base".

TRÊS MODOS, e a ordem não é conveniência — é o que impede uma varredura de LLM
de apagar nota boa em massa:
    --mapear    quantos pares existem, SEM chamar o LLM (só embedding+busca)
    --dry-run   o veredito de cada par, sem gravar nada
    --aplicar   aposenta a nota superada dos pares confirmados

PRÉ-FILTRO SEM LLM: só vira pergunta o par cujo vizinho mais próximo vem da obra
DECLARADA como superada e cai na banda de "mesmo tema, não duplicata"
[dedup_dist_max, contradicao_dist_max). Sem isso seriam ~5.900 chamadas para
achar um punhado de pares — e cada chamada é uma chance de veredito errado.

CONTENÇÃO: a resolução usa o MESMO `obras.substitui` do resto (relação declarada
preferida x superada), então é impossível esta varredura tocar numa nota do
Raven, do Amabis ou escrita à mão — mesmo que o LLM alucine uma contradição.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import contradicao, obras, prompts, rag  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.etl import EtlProcessor  # noqa: E402
from mente_digital.llm import LlamaManager  # noqa: E402
from mente_digital.state import AppContext  # noqa: E402
from mente_digital.telemetry import telemetry  # noqa: E402


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


async def _mapear(vs, marca_nova: str, limite: int, teto: float = 0.0):
    """Os pares candidatos. Sem LLM: embedding + busca, nada mais.

    `teto` > 0 troca a banda de contradição pela banda de PARÁFRASE
    (dist < obras_dedup_dist_max): é o modo dedup retroativo, que não precisa de
    juiz nenhum — cosseno já separa "mesma ideia, outras palavras" nessa faixa.

    Por que a passada retroativa existe: no momento da escrita, `_duplicata` olha
    só o vizinho MAIS PRÓXIMO (k=1). Quando o gêmeo mais próximo de um átomo novo
    era outro átomo novo, o gêmeo da obra superada nunca chegou a ser avaliado."""
    preferidas = obras.marcas(settings.obras_preferidas)
    superadas = obras.marcas(settings.obras_substituidas)
    pasta = Path(settings.dir_conhecimento_novo)
    pares, lidos = [], 0
    for p in sorted(pasta.glob("*.md")):
        texto = p.read_text(encoding="utf-8", errors="ignore")
        fm = rag.parse_frontmatter(texto)
        origem = fm.get("origem", "")
        if marca_nova.lower() not in origem.lower():
            continue
        corpo = rag.strip_frontmatter(texto).strip()
        if not corpo:
            continue
        lidos += 1
        try:
            res = await asyncio.to_thread(
                lambda c=corpo: vs._store.similarity_search_with_score(
                    c, k=5, filter={"tipo": "texto"}))
        except Exception as exc:
            telemetry.warn("VARREDURA", f"busca falhou em {p.name}: {exc}")
            continue
        for doc, dist in res:
            md = doc.metadata or {}
            if str(md.get("source", "")) == str(p):
                continue                        # ele mesmo
            o_antiga = str(md.get("origem") or "")
            if not obras.casa_alguma(o_antiga, superadas):
                continue
            if teto > 0:
                if not (settings.dedup_dist_max <= dist < teto):
                    continue
            elif not (settings.dedup_dist_max <= dist < settings.contradicao_dist_max):
                continue
            if not obras.substitui(origem, o_antiga, preferidas, superadas):
                continue
            pares.append((p, corpo, doc, dist))
            break                               # um par por átomo novo
        if limite and len(pares) >= limite:
            break
    return pares, lidos


def _primeira_linha(texto: str, pular_titulo: bool = True) -> str:
    """A linha de conteúdo do átomo, para o dono julgar sem abrir o arquivo."""
    linhas = [ln.strip() for ln in (texto or "").splitlines() if ln.strip()]
    if pular_titulo and linhas and linhas[0].startswith("#"):
        linhas = linhas[1:]
    for ln in linhas:
        if not ln.startswith(("**Malha", "#", "![[")):
            return ln[:200]
    return ""


def _titulo(texto: str) -> str:
    for ln in (texto or "").splitlines():
        if ln.strip().startswith("##"):
            return ln.lstrip("#").strip()[:120]
    return (texto or "").strip().splitlines()[0][:120] if texto.strip() else ""


def _escrever_lista(alvo: Path, pares, teto: float) -> None:
    """Relatório AMOSTRÁVEL: um par por bloco, do mais próximo ao mais distante.

    Ordenado por distância de propósito — é assim que se amostra uma decisão de
    corte: os primeiros são os clones óbvios, e em algum ponto da lista eles
    deixam de ser. Onde isso acontece é o limiar certo, e só o dono sabe ler
    isso no conteúdo dele."""
    alvo.parent.mkdir(parents=True, exist_ok=True)
    out = [
        "# Pares de paráfrase — candidatos a aposentadoria",
        "",
        f"- **{len(pares)} par(es)** na banda [{settings.dedup_dist_max}, {teto})",
        f"- obra preferida: `{settings.obras_preferidas}`",
        f"- obra superada (a que SAI): `{settings.obras_substituidas}`",
        "- ordem: distância crescente (os primeiros são os mais parecidos)",
        "- **NADA foi apagado.** Isto é só a lista para amostragem.",
        "",
        "Como ler: em algum ponto da lista os pares deixam de ser a mesma ideia e",
        "passam a ser só o mesmo assunto. O número dessa linha é o limiar certo.",
        "",
        "---",
        "",
    ]
    for i, (p, corpo, doc, dist) in enumerate(pares, 1):
        antigo = doc.page_content or ""
        src = os.path.basename(str((doc.metadata or {}).get("source", "")))
        out += [
            f"## {i}. dist {dist:.4f}",
            "",
            f"**FICA (nova)** — `{p.name}`",
            f"> {_titulo(corpo)}",
            f"> {_primeira_linha(corpo)}",
            "",
            f"**SAI (antiga)** — `{src}`",
            f"> {_titulo(antigo)}",
            f"> {_primeira_linha(antigo)}",
            "",
        ]
    alvo.write_text("\n".join(out), encoding="utf-8")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--marca", default="The Cannabis Encyclopedia",
                    help="substring da proveniência da edição NOVA")
    ap.add_argument("--limite", type=int, default=0, help="teto de pares (0 = todos)")
    ap.add_argument("--mapear", action="store_true", help="só conta, sem LLM")
    ap.add_argument("--saida", default="",
                    help="grava a lista dos pares num .md amostrável (modo --dedup)")
    ap.add_argument("--dedup", action="store_true",
                    help="banda de PARÁFRASE (obras_dedup_dist_max) em vez da de "
                         "contradição; não usa LLM")
    ap.add_argument("--dry-run", action="store_true", help="pergunta ao LLM, não grava")
    args = ap.parse_args()

    if _servidor_no_ar():
        print("O servidor está no ar — os dois disputariam a mesma GPU. Feche-o.")
        return 3

    ctx = AppContext(settings=settings)
    ctx.interactive_idle.set()
    ctx.vectorstore = rag.VectorStore(rag.EmbeddingProvider())
    await asyncio.to_thread(ctx.vectorstore.load_embeddings)
    await ctx.vectorstore.open()

    t0 = time.perf_counter()
    teto = settings.obras_dedup_dist_max if args.dedup else 0.0
    pares, lidos = await _mapear(ctx.vectorstore, args.marca, args.limite, teto)
    print(f"\n{lidos} átomo(s) da edição nova varridos em {time.perf_counter()-t0:.0f}s")
    print(f"{len(pares)} par(es) candidato(s) — vizinho da obra superada na banda "
          f"[{settings.dedup_dist_max}, {teto or settings.contradicao_dist_max})\n")
    if args.dedup:
        # PARÁFRASE não passa por juiz: nesta faixa o cosseno já decidiu. O que
        # sobrou aqui é a LACUNA da escrita (k=1), não caso duvidoso.
        pares.sort(key=lambda t: t[3])          # mais próximos primeiro
        if args.saida:
            _escrever_lista(Path(args.saida), pares, teto)
            print(f"lista -> {args.saida}")
        for p, _c, doc, dist in pares[:20]:
            print(f"  {dist:.3f} {p.name[:50]}")
            print(f"        vs {os.path.basename(str((doc.metadata or {}).get('source','')))[:50]}")
        if not args.dry_run and pares:
            etl = EtlProcessor(ctx)
            n = 0
            for _p, _c, doc, _d in pares:
                if await etl._aposentar(doc, "duplicata de parafrase (retroativo)"):
                    n += 1
            print(f"\n{n} nota(s) antiga(s) aposentada(s)")
        else:
            print(f"\n(dry-run: nada gravado — {len(pares)} nota(s) sairiam)")
        return 0
    if args.mapear or not pares:
        for p, _c, doc, dist in pares[:15]:
            print(f"  {dist:.3f} {p.name[:52]}")
            print(f"        vs {os.path.basename(str((doc.metadata or {}).get('source','')))[:52]}")
        return 0

    ctx.llama = LlamaManager()
    await ctx.llama.load()
    etl = EtlProcessor(ctx)
    confirmados = negados = aposentados = 0
    try:
        for p, corpo, doc, dist in pares:
            veredito = await ctx.llama.collect(
                prompts.prompt_contradicao(corpo, doc.page_content),
                max_tokens=settings.max_tokens_contradicao,
                system_prompt=prompts.SYS_CONTRADICAO,
            )
            motivo = contradicao.parse_veredito(veredito)
            if not motivo:
                negados += 1
                continue
            confirmados += 1
            print(f"\n  [{dist:.3f}] {motivo[:100]}")
            print(f"    NOVA : {corpo.splitlines()[0][:78]}")
            print(f"    VELHA: {doc.page_content.strip().splitlines()[0][:78]}")
            if not args.dry_run and await etl._aposentar(
                    doc, "contradiz a edicao nova (varredura retroativa)"):
                aposentados += 1
    finally:
        await ctx.llama.unload()

    print(f"\n{confirmados} contradição(ões) confirmada(s), {negados} negada(s) "
          f"em {len(pares)} par(es)")
    print(f"{aposentados} nota(s) antiga(s) aposentada(s)"
          + ("  (dry-run: nada gravado)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
