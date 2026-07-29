"""Reescreve o CORPO dos átomos QUEBRADOS a partir do job-fonte do livro.

O passivo (medido 2026-07-28): o modelo de 4B estourava o orçamento de saída no
meio de um átomo. Trocar para o 8B conserta o FUTURO (0 truncados em 24
medições); o que já está em disco precisa desta passada. Na Cannabis
Encyclopedia são 577 notas suspeitas — das quais só 459 estão de fato quebradas
(ver a triagem em `mente_digital/reparo.py`).

Como o trecho-fonte é achado: por EMBEDDING, não por casamento léxico. O átomo
está em PT-BR e o livro em inglês — medi a régua léxica ("a última palavra do
átomo aparece no job?") e ela classificou 'alongamento celular' como cortado no
meio, porque o fonte diz 'cell elongation'. O e5 é multilíngue e cruza os dois
idiomas; é o mesmo ranqueamento do deep-fetch da web (`rankear_por_similaridade`).

Guardas (na dúvida, MANTÉM o original — átomo pobre é menos dano que átomo
inventado): saída vazia, sentinela NADA, corpo curto demais, CJK, inglês, corpo
truncado de novo. Todo motivo de recusa é relatado, nunca silencioso.

    python scripts/reparar_atomos_quebrados.py --so-triagem
    python scripts/reparar_atomos_quebrados.py --dry-run --limite 12
    python scripts/reparar_atomos_quebrados.py --aplicar
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import textwrap
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)   # o pydantic lê o .env relativo ao CWD (ver ocr_agora.py)

from mente_digital import livro as livro_mod  # noqa: E402
from mente_digital import idioma, prompts, rag, reparo  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.llm import LlamaManager, preparar_offline  # noqa: E402

# Fatia do capítulo usada como CANDIDATO a trecho-fonte. Menor que o lote da
# ingestão (3500) de propósito: aqui o alvo é UMA ideia, e um candidato grande
# dilui o embedding — o trecho certo fica escondido no meio do capítulo.
CHARS_CANDIDATO = 1200
ORCAMENTO_TRECHO = 3000     # chars de fonte que vão no prompt (protege o n_ctx)
MAX_TOKENS_CORPO = 320      # 1-3 frases; teto curto também é anti-derivação
# Candidato curto demais é lixo do fatiamento e ainda GANHA do trecho certo: no
# primeiro dry-run o top-1 de um átomo foi o parágrafo '.', com similaridade
# 0,854 — cosseno de um texto minúsculo contra qualquer coisa é alto.
MIN_CHARS_CANDIDATO = 200


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


def _jobs_por_origem() -> Dict[str, Path]:
    """`origem` do frontmatter -> arquivo do job. `livro.origem_do_job` é o ponto
    único que monta essa string nos dois lados, então o casamento é por igualdade."""
    fora: Dict[str, Path] = {}
    for j in (Path(settings.dir_ingestao) / "processados").glob("*.json"):
        try:
            job = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        fora[livro_mod.origem_do_job(job)] = j
    return fora


class Alvo:
    __slots__ = ("caminho", "texto", "categoria", "atomo", "origem")

    def __init__(self, caminho: Path, texto: str, categoria: str,
                 atomo: reparo.Atomo, origem: str) -> None:
        self.caminho, self.texto = caminho, texto
        self.categoria, self.atomo, self.origem = categoria, atomo, origem


def _varrer(origem_filtro: str) -> Tuple[List[Alvo], Counter]:
    vault = Path(settings.caminho_obsidian)
    alvos: List[Alvo] = []
    contagem: Counter = Counter()
    for md in vault.rglob("*.md"):
        if ".obsidian" in md.parts:
            continue
        try:
            texto = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        origem = (rag.parse_frontmatter(texto).get("origem") or "").strip()
        if origem_filtro and origem_filtro.lower() not in origem.lower():
            continue
        cat, atomo = reparo.triar(texto)
        if not atomo.titulo:
            continue
        contagem[cat] += 1
        if cat in reparo.PRECISAM_LLM or cat in (reparo.RESIDUO, reparo.IDIOMA):
            alvos.append(Alvo(md, texto, cat, atomo, origem))
    return alvos, contagem


def _relatar_triagem(contagem: Counter, alvos: List[Alvo], jobs: Dict[str, Path]) -> None:
    total = sum(contagem.values())
    print(f"\n{total} átomo(s) na origem escolhida:")
    for cat in (reparo.OK, reparo.FIGURA, reparo.RESIDUO, reparo.IDIOMA,
                reparo.VAZIO, reparo.TRUNCADO):
        print(f"  {contagem[cat]:6d}  {cat}")
    sem_job = [a for a in alvos if a.categoria in reparo.PRECISAM_LLM and a.origem not in jobs]
    if sem_job:
        print(f"\n  {len(sem_job)} quebrado(s) SEM job-fonte — ficam de fora "
              "(sem o trecho não há como reescrever sem inventar):")
        for a in sem_job[:3]:
            print(f"    {a.origem[:100]}")


def _amostrar(alvos: List[Alvo], n: int) -> None:
    por_cat: Dict[str, List[Alvo]] = defaultdict(list)
    for a in alvos:
        por_cat[a.categoria].append(a)
    for cat, lista in por_cat.items():
        print(f"\n--- amostra {cat} ({len(lista)}) ---")
        for a in lista[:n]:
            corpo = a.atomo.corpo.replace("\n", " | ")
            print(f"  ## {a.atomo.titulo[:78]}\n     {corpo[:150]}")


def _mostrar(alvo: "Alvo", novo_corpo: str, similaridade: float) -> None:
    """Antes/depois INTEIROS. Corte de exibição é inaceitável aqui: a amostra
    existe para o dono julgar o texto, e o defeito que mais escapa (o modelo
    escorregando para o inglês) mora justamente no fim da frase."""
    print(f"\n  ## {alvo.atomo.titulo}   [{alvo.categoria}, fonte sim={similaridade:.3f}]")
    for rotulo, texto in (("ANTES ", alvo.atomo.corpo), ("DEPOIS", novo_corpo)):
        linhas = textwrap.wrap(texto.replace("\n", " "), 96) or [""]
        print(f"     {rotulo}: {linhas[0]}")
        for extra in linhas[1:]:
            print(f"             {extra}")


async def _reparar(alvos: List[Alvo], jobs: Dict[str, Path], dry: bool,
                   amostra: int) -> Tuple[int, Counter]:
    """Reescreve os corpos. Agrupa por job para embeddar o capítulo UMA vez."""
    emb = rag.EmbeddingProvider()
    await asyncio.to_thread(emb.load)
    if emb.instance is None:
        print("Sem embeddings: não dá para achar o trecho-fonte. Abortado.")
        return 0, Counter()
    llama = LlamaManager(preparar_offline(settings.caminho_modelo_atomizacao))
    await llama.load()

    por_job: Dict[str, List[Alvo]] = defaultdict(list)
    for a in alvos:
        por_job[a.origem].append(a)

    feitos, recusas = 0, Counter()
    mostrados = 0
    t0 = time.perf_counter()
    try:
        for origem, lista in por_job.items():
            job = json.loads(jobs[origem].read_text(encoding="utf-8"))
            candidatos = [c for c in livro_mod.fatiar_lotes(job.get("texto", ""), CHARS_CANDIDATO)
                          if len(c) >= MIN_CHARS_CANDIDATO]
            if not candidatos:
                recusas["job sem texto"] += len(lista)
                continue
            vecs = await asyncio.to_thread(emb.instance.embed_documents, candidatos)
            for a in lista:
                consulta = reparo.consulta_do_atomo(a.atomo)
                qv = await asyncio.to_thread(emb.instance.embed_query, consulta)
                rank = rag.rankear_por_similaridade(qv, list(zip(candidatos, vecs)))
                trecho = reparo.juntar_trechos(rank, ORCAMENTO_TRECHO)
                saida = await llama.collect(
                    prompts.prompt_reparar_corpo(
                        a.atomo.titulo, reparo.corpo_sem_ingles(a.atomo.corpo), trecho,
                        idioma.linha_glossario(idioma.termos_do_glossario(trecho))),
                    system_prompt=prompts.SYS_SINTESE,
                    max_tokens=MAX_TOKENS_CORPO,
                )
                novo_corpo = reparo.sem_eco_do_titulo(
                    reparo.limpar_saida(saida), a.atomo.titulo)
                # Pós-checagem com PROVA no original: troca as traduções erradas
                # já conhecidas ("cogumelos" por "buds") só onde o inglês daquele
                # trecho de fato dizia aquilo. O prompt previne; isto pega o que
                # escapou, sem inventar correção onde não há prova.
                novo_corpo, _n = idioma.aplicar_correcoes(novo_corpo, trecho)
                ok, motivo = reparo.corpo_aceitavel(novo_corpo, a.atomo)
                if not ok:
                    recusas[motivo] += 1
                    continue
                if mostrados < amostra:
                    mostrados += 1
                    _mostrar(a, novo_corpo, rank[0][0])
                if not dry:
                    _escrever(a, reparo.montar(a.atomo, novo_corpo))
                feitos += 1
    finally:
        await llama.unload()
    dt = time.perf_counter() - t0
    print(f"\n{'repararia' if dry else 'reparou'} {feitos} átomo(s) em {dt:.0f}s "
          f"({dt / max(1, feitos):.1f}s cada)")
    return feitos, recusas


_BACKUP: Optional[Path] = None


def _escrever(alvo: Alvo, novo: str) -> None:
    """Escreve o átomo reparado, guardando o original. O vault NÃO está no git:
    sem cópia, uma passada ruim não tem desfazer (mesma decisão do
    `desembrulhar_angulares.py`)."""
    if _BACKUP is not None:
        destino = _BACKUP / alvo.caminho.name
        destino.write_text(alvo.texto, encoding="utf-8")
    alvo.caminho.write_text(novo, encoding="utf-8")


def main() -> int:
    global _BACKUP
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--origem", default="The Cannabis Encyclopedia",
                    help="filtro por substring da proveniência ('' = vault inteiro)")
    ap.add_argument("--limite", type=int, default=0, help="0 = todos os quebrados")
    ap.add_argument("--amostra", type=int, default=10, help="quantos antes/depois imprimir")
    ap.add_argument("--tambem-idioma", action="store_true",
                    help="inclui os átomos ÍNTEGROS que têm frase em inglês no corpo")
    ap.add_argument("--so-triagem", action="store_true",
                    help="só conta os buckets (não carrega LLM nem embeddings)")
    ap.add_argument("--dry-run", action="store_true", help="roda o reparo mas NÃO escreve")
    ap.add_argument("--aplicar", action="store_true", help="escreve no vault")
    ap.add_argument("--sem-backup", action="store_true",
                    help="não copia os originais antes de escrever (NÃO recomendado)")
    args = ap.parse_args()
    if not (args.so_triagem or args.dry_run or args.aplicar):
        ap.error("escolha --so-triagem, --dry-run ou --aplicar")

    alvos, contagem = _varrer(args.origem)
    jobs = _jobs_por_origem()
    _relatar_triagem(contagem, alvos, jobs)

    residuos = [a for a in alvos if a.categoria == reparo.RESIDUO]
    # IDIOMA (frase inglesa no meio de um átomo íntegro) só entra quando pedido:
    # o átomo NÃO está quebrado, e reescrevê-lo é mexer no que já funciona.
    categorias = reparo.PRECISAM_LLM + ((reparo.IDIOMA,) if args.tambem_idioma else ())
    quebrados = [a for a in alvos if a.categoria in categorias and a.origem in jobs]
    # Corpo em INGLÊS não é mais excluído. A razão de excluí-lo era o modelo
    # continuar no idioma do corpo parcial que o prompt mostra — a causa foi
    # consertada em `reparo.corpo_sem_ingles` (o inglês não chega ao prompt), e
    # excluí-los deixava um buraco: `traduzir_atomos_ingles.py` seleciona por
    # TÍTULO, e esses têm título em português. Ficariam sem dono nenhum.
    em_ingles = sum(1 for a in quebrados if idioma.em_ingles(a.atomo.corpo))
    if em_ingles:
        print(f"\n{em_ingles} alvo(s) com o corpo INTEIRO em inglês — o corpo parcial "
              "não vai ao prompt; o modelo reescreve do título + trecho-fonte")
    if args.limite:
        quebrados = quebrados[: args.limite]
    print(f"\nconserto SEM LLM (só formatação): {len(residuos)}")
    print(f"reescrita pelo job-fonte:         {len(quebrados)}")

    if args.so_triagem:
        _amostrar(residuos + quebrados, args.amostra)
        print("\n(só-triagem: NADA foi escrito)")
        return 0
    if _servidor_no_ar():
        print("\nO servidor está NO AR — os dois disputariam a mesma GPU. Feche-o.")
        return 3
    if args.aplicar and not args.sem_backup:
        _BACKUP = Path("dados/backups") / f"reparo_{datetime.now():%Y%m%d_%H%M%S}"
        _BACKUP.mkdir(parents=True, exist_ok=True)
        print(f"backup dos originais em: {_BACKUP}")

    # Formatação primeiro: é puro, instantâneo e independe da GPU.
    for a in residuos:
        if args.aplicar:
            _escrever(a, reparo.montar(a.atomo, a.atomo.corpo))
    print(f"{'limparia' if args.dry_run else 'limpou'} a formatação de {len(residuos)} átomo(s)")

    feitos, recusas = asyncio.run(_reparar(quebrados, jobs, args.dry_run, args.amostra))
    if recusas:
        print("\nmantidos como estavam (guarda disparou):")
        for motivo, n in recusas.most_common():
            print(f"  {n:5d}  {motivo}")
    if args.dry_run:
        print("\n(dry-run: NADA foi escrito)")
    elif feitos or residuos:
        print("\nRode `python scripts/reindexar.py` — a busca só vê o texto novo depois.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
