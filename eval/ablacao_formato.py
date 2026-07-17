"""
Ablação do FORMATO do átomo, em escala, sobre a base e as perguntas REAIS.

Pergunta que responde: quanto da recuperação o FORMATO ganha (título com assunto
prefixado, Malha Neural, tags) — separado do CONTEÚDO?

Método: para cada pergunta real do SQLite, pega os átomos que a busca vetorial de
fato devolve, e mede a distância de cosseno (a métrica do Chroma) do MESMO átomo em
variantes mecânicas de si mesmo. O conteúdo é constante; só a forma muda. Assim o
efeito medido é do formato, e não "um átomo melhor ganha de um átomo pior".

Por que isso importa: um teste anterior comparou um átomo escrito à mão (0.301) com
o melhor da base (0.327) e o chamou de ganho de formato. Não era — eram átomos sobre
assuntos diferentes, e o de mão simplesmente respondia à pergunta. Este script existe
para não repetir esse erro.

Nada é indexado nem gravado. Roda sem GPU (embeddings na CPU).

Uso:
    python eval/ablacao_formato.py [--top N] [--perguntas N]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MENTE_EMBEDDING_DEVICE", "cpu")
os.environ.setdefault("MENTE_RAG_DEBUG", "false")

from rag import EmbeddingProvider, VectorStore  # noqa: E402
from telemetry import db  # noqa: E402

_MALHA_LINHA = re.compile(r"^\s*\*\*Malha Neural:\*\*.*$", re.MULTILINE)
_TAGS_LINHA = re.compile(r"^\s*#[\w_]+(?:\s+#[\w_]+)*\s*$", re.MULTILINE)
_TITULO = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$", re.MULTILINE)


def sem_malha(texto: str) -> str:
    return _MALHA_LINHA.sub("", texto).strip()


def sem_tags(texto: str) -> str:
    return _TAGS_LINHA.sub("", texto).strip()


def titulo_sem_assunto(texto: str) -> str:
    """'## Clima no Paraguai: Efeito de Forno' -> '## Efeito de Forno'.

    Desfaz o `garantir_assunto` do importador — que é justamente o que se quer medir.
    Sem ':' no título, devolve o texto intacto (o átomo não tem assunto prefixado).
    """
    m = _TITULO.search(texto)
    if not m or ":" not in m.group(1):
        return texto
    curto = m.group(1).split(":", 1)[1].strip()
    if not curto:
        return texto
    return texto[: m.start()] + f"## {curto}" + texto[m.end():]


def so_corpo(texto: str) -> str:
    """Tira título, malha e tags — sobra a afirmação nua."""
    t = _TITULO.sub("", sem_tags(sem_malha(texto)))
    return "\n".join(ln for ln in t.splitlines() if ln.strip()).strip()


VARIANTES = {
    "1. como está (formato completo)": lambda t: t,
    "2. sem a linha da Malha": sem_malha,
    "3. sem as tags": sem_tags,
    "4. título SEM o assunto prefixado": titulo_sem_assunto,
    "5. só o corpo (sem título/malha/tags)": so_corpo,
}


def _dist(a, b) -> float:
    """Distância de cosseno — a mesma que o Chroma usa (hnsw:space=cosine)."""
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 1.0
    return 1 - sum(x * y for x, y in zip(a, b)) / (na * nb)


def perguntas_reais(limite: int) -> list[str]:
    vistas: set = set()
    out: list[str] = []
    for t in db.get_history(limit=1000):
        q = (t.get("q") or "").strip()
        if q and q.lower() not in vistas:
            vistas.add(q.lower())
            out.append(q)
    return out[:limite]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5, help="átomos por pergunta")
    ap.add_argument("--perguntas", type=int, default=100)
    args = ap.parse_args()

    emb = EmbeddingProvider()
    await asyncio.to_thread(emb.load)
    store = VectorStore(emb)
    await store.open()
    if store._store is None:
        print("Chroma indisponível.")
        return

    qs = perguntas_reais(args.perguntas)
    print(f"perguntas reais: {len(qs)} | átomos por pergunta: {args.top}\n")

    e = emb.instance
    por_variante: dict = {nome: [] for nome in VARIANTES}
    pares = 0

    for q in qs:
        res = await asyncio.to_thread(
            store._store.similarity_search_with_score, q, k=args.top
        )
        if not res:
            continue
        qv = await asyncio.to_thread(e.embed_query, q)
        textos_orig = [d.page_content for d, _ in res]
        for nome, fn in VARIANTES.items():
            variantes = [fn(t) for t in textos_orig]
            # Variante que colapsou (átomo sem o elemento) não vira par: mediria ruído.
            validos = [(i, v) for i, v in enumerate(variantes) if v.strip()]
            if not validos:
                continue
            vecs = await asyncio.to_thread(e.embed_documents, [v for _, v in validos])
            for (i, _), vec in zip(validos, vecs):
                por_variante[nome].append((_dist(qv, vec), i, q))
        pares += len(textos_orig)

    print(f"pares pergunta-átomo medidos: {pares}\n")
    base = {(i, q): d for d, i, q in por_variante["1. como está (formato completo)"]}
    print(f"{'variante':42} | mediana |  média  | delta médio vs completo")
    print("-" * 92)
    for nome, linhas in VARIANTES.items():
        vals = [d for d, _, _ in por_variante[nome]]
        if not vals:
            continue
        deltas = [d - base[(i, q)] for d, i, q in por_variante[nome] if (i, q) in base]
        dm = statistics.mean(deltas) if deltas else 0.0
        sinal = "  (referência)" if nome.startswith("1.") else f"  {dm:+.4f}"
        print(
            f"{nome:42} |  {statistics.median(vals):.3f}  |  {statistics.mean(vals):.3f}  |{sinal}"
        )

    print()
    print("Leitura: delta POSITIVO = tirar aquele elemento AFASTA o átomo da pergunta")
    print("(ou seja, o elemento estava ajudando). Perto de zero = o elemento não paga.")


if __name__ == "__main__":
    asyncio.run(main())
