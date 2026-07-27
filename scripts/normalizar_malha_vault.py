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

SEGUNDA PASSADA (--gerar-malha): o conserto acima só canoniza o que TEM colchete.
Medido: 484 dos 672 átomos fora do padrão não têm conceito nenhum — o Python não
inventa link (auto-link a partir do título seria ruído para inflar métrica). Para
esses existe o modo `--gerar-malha`, que pergunta ao LLM 1 a 3 conceitos por átomo
e grava a linha canônica. É a única parte que usa GPU: ~5s por átomo, então rode
com o servidor FECHADO e conte ~40 min para 484.

O que a malha afeta, para calibrar a expectativa: navegação por GRAFO e as
conexões/contradições. O RAG de resposta NÃO depende dela (o gate usa os tokens do
corpo e a busca vetorial usa o texto), então átomo sem malha continua sendo
encontrado — ele só não é navegável.

Uso:
    python scripts/normalizar_malha_vault.py                       # DRY-RUN
    python scripts/normalizar_malha_vault.py --aplicar             # canoniza rótulo
    python scripts/normalizar_malha_vault.py --gerar-malha --aplicar  # + LLM nos sem-link
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
    ap.add_argument("--gerar-malha", action="store_true",
                    help="2ª passada: pede ao LLM os conceitos dos átomos sem link (usa GPU)")
    ap.add_argument("--limite", type=int, default=0, help="máx. de átomos na 2ª passada")
    args = ap.parse_args()
    pendentes_llm = []

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
            if args.gerar_malha:
                pendentes_llm.append(f)   # a 2ª passada cuida destes (com GPU)
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

    if args.gerar_malha and pendentes_llm:
        alvos = pendentes_llm[: args.limite] if args.limite else pendentes_llm
        print(f"\n2ª PASSADA (LLM): {len(alvos)} átomo(s) sem conceito — ~5s cada.")
        if not args.aplicar:
            print("  (dry-run: nada será chamado nem gravado; use --aplicar)")
            return 0
        import asyncio
        print(f"  {asyncio.run(_gerar_malhas(alvos))} átomo(s) ganharam malha.")
    return 0


async def _gerar_malhas(alvos) -> int:
    """Pede ao LLM 1-3 conceitos por átomo e grava a linha canônica.

    Sobe só o LLM (nem embeddings): esta passada não deduplica nada. A SINTAXE volta
    a ser imposta pelo Python (`normalizar_malha`) — o modelo entrega os conceitos,
    e resposta sem colchete nenhum é descartada em vez de virar texto solto."""
    from mente_digital import prompts
    from mente_digital.atomos import normalizar_malha
    from mente_digital.llm import LlamaManager

    llama = LlamaManager()
    await llama.load()
    feitos = 0
    try:
        for i, f in enumerate(alvos, 1):
            texto = f.read_text(encoding="utf-8")
            titulo = next((ln[3:].strip() for ln in texto.splitlines()
                           if ln.startswith("## ")), "")
            ideia = "\n".join(ln for ln in texto.splitlines()
                              if ln and not ln.startswith(("#", "---", "origem:",
                                                           "colhido_em:", "**")))[:600]
            if not titulo or not ideia.strip():
                continue
            try:
                bruto = await llama.collect(
                    prompts.prompt_malha_de_atomo(titulo, ideia),
                    max_tokens=64, system_prompt=prompts.SYS_SINTESE, preemptible=True)
            except Exception:
                continue
            links = normalizar_malha(bruto.strip().splitlines()[0] if bruto.strip() else "")
            if "[[" not in links:
                continue                     # sem conceito utilizável: não inventa
            linhas = texto.rstrip().splitlines()
            # A malha entra ANTES da linha de tags (o formato canônico do átomo).
            pos = next((k for k, ln in enumerate(linhas) if ln.startswith("#zettel")),
                       len(linhas))
            linhas.insert(pos, f"**Malha Neural:** {links}")
            f.write_text("\n".join(linhas) + "\n", encoding="utf-8")
            feitos += 1
            if i % 25 == 0:
                print(f"    {i}/{len(alvos)}…", flush=True)
    finally:
        await llama.unload()
    return feitos


if __name__ == "__main__":
    raise SystemExit(main())
