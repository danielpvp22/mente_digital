"""Copia para a NOTA DA FIGURA a legenda em português que já está no vault.

O DEFEITO (medido em 2026-07-29, Cannabis Encyclopedia): a mesma legenda existe em
dois lugares — a linha do bloco `## Figuras`, que a síntese do capítulo carrega, e
a nota da própria figura. O bloco está em PORTUGUÊS e a nota continua no INGLÊS do
site: 1.452 das 1.818 notas (825 pela régua estrita do título, 627 só pela do
corpo). A pergunta do dono chega em português e o aterramento léxico do gate é
casamento de PALAVRA — legenda em inglês fica difícil de alcançar. É o mesmo
diagnóstico que o `traduzir_legendas_figuras.py` consertou no Cervantes em PDF.

POR QUE COPIAR EM VEZ DE TRADUZIR: a tradução já foi paga. Traduzir de novo custa
~30 min de GPU e coloca no vault um texto NOVO que ninguém conferiu; copiar não
introduz uma palavra sequer que já não estivesse na base — só a põe também do lado
onde a busca precisa dela. A sobra (12 notas: 11 sem linha de bloco, 1 com o par
também em inglês) é o único caso que pede LLM, e só com `--llm`.

O par é achado pelo ARQUIVO da imagem, nunca por semelhança de texto: é a mesma
âncora exata que a co-locação de figura já usa. Por cima da cópia roda
`idioma.aplicar_correcoes`, que exige o inglês original como PROVA — a mesma
guarda que impediu "borboleta-monarca" de virar "bud-monarca".

Reversível e idempotente: o inglês vai para `legenda_original:` no frontmatter, e
nota que já o tem é pulada. Ainda assim grava backup, porque o vault não tem um.

    python scripts/sincronizar_legendas_figuras.py --dry-run
    python scripts/sincronizar_legendas_figuras.py --aplicar
    python scripts/sincronizar_legendas_figuras.py --aplicar --llm   # inclui a sobra
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import List, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import figuras, idioma, prompts  # noqa: E402
from mente_digital.config import settings  # noqa: E402


def esta_em_ingles(legenda: str) -> bool:
    """Une as DUAS réguas já existentes em vez de inventar uma terceira.

    `em_ingles` é assimétrica (exige zero português) e pega o caso limpo; ela
    sozinha perde a legenda MISTA, que é 43% das afetadas aqui — frase inglesa com
    um "de" no meio. `corpo_em_ingles` cobre essa, exigindo só que o inglês
    predomine. Nenhuma das duas julga texto de menos de 3 palavras, então rótulo
    curto ("Soil Tests", "Cannabis sativa") fica de fora — e ele é justamente o
    `attach_only`, que nem disputa vaga na busca.

    O `limpar_proveniencia` no segundo termo NÃO é enfeite: sem ele, a FÓRMULA
    QUÍMICA entre parênteses vira voto de português. Medido na primeira passada —
    "how ozone (O3) sheds a molecule to become oxygen (O2)" conta 3 inglesas
    contra 3 portuguesas, porque "(O3)" e "(O2)" viram o token "o" e "a molecule"
    dá o "a". 14 legendas inteiramente em inglês escaparam assim, e só o
    post-check as encontrou. `em_ingles` já limpa por dentro; esta não limpava."""
    return (idioma.em_ingles(legenda)
            or idioma.corpo_em_ingles(idioma.limpar_proveniencia(legenda)))


def curta(legenda: str) -> bool:
    """Curta demais para QUALQUER régua de idioma julgar (o corte do `idioma`)."""
    return len(idioma.limpar_proveniencia(legenda).split()) < idioma.MIN_PALAVRAS


def rotulo_generico(legenda: str) -> bool:
    """"página 28" é o rótulo que o `bloco_markdown` põe quando NÃO há legenda.

    Sem esta guarda, uma figura sem legenda trocaria o rótulo de seção ("Perlite")
    por um número de página — perda pura, e logo depois indexada."""
    return bool(re.match(r"^p[áa]gina\s+\d+$", (legenda or "").strip(), re.IGNORECASE))


def mapear_blocos(vault: Path, figs: Path) -> dict:
    """`{imagem: legenda PT}` varrendo TODA nota fora de Figuras/.

    Varre pelo CONTEÚDO (a linha que `bloco_markdown` escreve) e não pelo nome do
    arquivo: o bloco `## Figuras` às vezes fica dentro da nota-síntese e às vezes
    vira arquivo próprio, porque `_salvar_atomos` fatia o corpo por `##`."""
    mapa: dict = {}
    for p in vault.rglob("*.md"):
        if figs in p.parents:
            continue
        try:
            texto = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if "![[" in texto:
            mapa.update(figuras.legendas_do_bloco(texto))
    return mapa


def _stem_da_nota(p: Path) -> str:
    nome = p.name[:-3]                      # tira ".md"
    return nome[:-5] if nome.endswith(".webp") else nome


async def traduzir_sobras(sobras: List[Tuple[Path, str]], aplicar: bool,
                          backup: Path = None) -> int:
    """As que a cópia não alcança: figura cuja página não deixou bloco.

    Roda com o modelo OFFLINE (o mesmo da atomização), não com o do servidor —
    aqui não há ninguém esperando e a VRAM está livre. Duas guardas antes de
    gravar: saída vazia e saída que CONTINUOU em inglês são puladas, porque uma
    legenda errada em português é pior que a original em inglês (esta pelo menos
    é do livro).
    """
    from mente_digital import llm as llm_mod

    caminho = llm_mod.preparar_offline(settings.caminho_modelo_atomizacao)
    llama = llm_mod.LlamaManager(caminho)
    await llama.load()
    feitas = 0
    try:
        for p, legenda in sobras:
            glossario = idioma.linha_glossario(idioma.termos_do_glossario(legenda))
            saida = (await llama.collect(
                prompts.prompt_traduzir_legenda(legenda, glossario),
                system_prompt=prompts.SYS_LEGENDA_PT, max_tokens=160,
            )).strip().strip('"').split("\n")[0].strip()

            if not saida or idioma.em_ingles(saida) or idioma.corpo_em_ingles(saida):
                print(f"  [pulou] {p.name[-22:]}  {legenda[:60]}")
                continue
            saida, _ = idioma.aplicar_correcoes(saida, legenda)
            novo = figuras.trocar_legenda(p.read_text(encoding="utf-8"), saida)
            if novo is None:
                continue
            print(f"  {p.name[-22:]}\n     EN: {legenda[:70]}\n     PT: {saida[:70]}")
            if aplicar:
                if backup is not None:
                    shutil.copy2(p, backup / p.name)
                p.write_text(novo, encoding="utf-8")
            feitas += 1
    finally:
        await llama.unload()
    return feitas


def rodar(obra: str, limite: int, aplicar: bool, usar_llm: bool = False) -> int:
    vault = Path(settings.caminho_obsidian)
    figs = vault / settings.subpasta_figuras
    if not figs.is_dir():
        print(f"pasta de figuras não existe: {figs}")
        return 1

    mapa = mapear_blocos(vault, figs)
    print(f"legendas de bloco encontradas: {len(mapa)}")

    notas = [p for p in sorted(figs.rglob("*.md"))
             if not obra or obra.lower() in str(p).lower()]
    print(f"notas de figura a examinar: {len(notas)}\n")

    backup = None
    if aplicar:
        backup = RAIZ / "dados" / "backups" / f"legendas_{time.strftime('%Y%m%d_%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)

    ja_pt = sem_par = par_ingles = pulou = so_titulo = 0
    trocadas = correcoes = 0
    amostra = []
    sobras: List[Tuple[Path, str]] = []
    for p in notas:
        texto = p.read_text(encoding="utf-8")
        atual = figuras.legenda_da_nota(texto)
        pt = mapa.get(_stem_da_nota(p))
        if not esta_em_ingles(atual):
            # CURTA (1-2 palavras): nenhuma régua de idioma julga "Perlite" ou
            # "Soil Tests" — abaixo de MIN_PALAVRAS elas se calam, de propósito.
            # E são justamente as que mais aparecem na resposta, porque chegam por
            # CO-LOCAÇÃO (rótulo de seção, `attach_only`). Aqui a evidência não é
            # o idioma, é o PAR: a linha do bloco é a versão traduzida da MESMA
            # imagem, casada por arquivo (âncora exata). Se as duas discordam e a
            # do bloco não está em inglês, a do bloco é a tradução — medido em
            # 205 de 229 curtas: "Soil Tests"/"Testes de Solo", "Perlite"/"Perlita".
            if not (curta(atual) and pt and pt != atual
                    and not esta_em_ingles(pt) and not rotulo_generico(pt)):
                ja_pt += 1
                continue
        elif pt is None:
            sem_par += 1
            sobras.append((p, atual))
            continue
        elif esta_em_ingles(pt):
            par_ingles += 1
            sobras.append((p, atual))
            continue

        pt, n = idioma.aplicar_correcoes(pt, atual)
        novo = figuras.trocar_legenda(texto, pt)
        if novo is None:                    # já tocada, ou nada a fazer
            pulou += 1
            continue

        correcoes += n
        trocadas += 1
        # O título nasce cortado em 120 chars e o corpo guarda a legenda inteira.
        # Quando a linha do corpo não é reconhecível como repetição do título, ela
        # fica intocada de propósito — e isso precisa APARECER no relatório, senão
        # a passada se anuncia completa tendo consertado metade daquelas notas.
        if not any(ln.strip() == pt for ln in novo.split("\n")):
            so_titulo += 1
        if len(amostra) < 8:
            amostra.append((atual, pt))
        if aplicar:
            shutil.copy2(p, backup / p.name)
            p.write_text(novo, encoding="utf-8")
        if limite and trocadas >= limite:
            break

    verbo = "trocaria" if not aplicar else "trocou"
    print(f"{verbo}: {trocadas}")
    print(f"  já em português (intactas) ....... {ja_pt}")
    print(f"  sem par no bloco ................. {sem_par}")
    print(f"  par também em inglês ............. {par_ingles}")
    print(f"  puladas (já tocadas/sem título) .. {pulou}")
    print(f"  correções do glossário por cima .. {correcoes}")
    print(f"  só o título trocou (corpo alheio)  {so_titulo}")

    print("\n--- amostra ---")
    for en, pt in amostra:
        print(f"\n  EN: {en[:100]}")
        print(f"  PT: {pt[:100]}")

    if usar_llm and sobras:
        print(f"\n--- LLM nas {len(sobras)} sem par ---")
        trocadas += asyncio.run(traduzir_sobras(sobras, aplicar, backup))

    if aplicar and trocadas:
        print(f"\nbackup dos originais em {backup}")
        print("Rode `python scripts/reindexar.py` para a busca ver o texto novo.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--obra", default="", help="filtro no caminho da nota (ex.: encyclopedia)")
    ap.add_argument("--limite", type=int, default=0, help="0 = todas")
    ap.add_argument("--dry-run", action="store_true", help="só relata (default)")
    ap.add_argument("--aplicar", action="store_true", help="grava no vault")
    ap.add_argument("--llm", action="store_true",
                    help="traduz com o modelo offline as que não têm par no bloco")
    args = ap.parse_args()
    if args.aplicar and args.dry_run:
        print("--aplicar e --dry-run são mutuamente exclusivos.")
        return 2
    return rodar(args.obra, args.limite, args.aplicar, args.llm)


if __name__ == "__main__":
    raise SystemExit(main())
