"""
A/B de MODELOS DE EMBEDDING sobre o vault real (Etapa 3a).

Por que existe: o modelo de embedding (`embedding_model`) é a única peça do RAG que
nunca foi comparada — e ele governa TODA recuperação. É a raiz da complexidade de
calibração (gate `rag_score_confident`, aterramento IDF, roteamento definicional): esses
botões compensam SINAL FRACO de recuperação. Antes de trocar (operação DESTRUTIVA —
reindexa o vault e reescala os thresholds), mede-se se o candidato recupera melhor.

Como mede (label-free, SEM tocar o banco de produção):
  1) Puxa os DOCUMENTOS dos átomos do Chroma atual (não os vetores — re-embeda do zero).
  2) Embeda o corpo de cada átomo com CADA modelo (cosseno; vetores normalizados).
     O corpo TIRA a 1ª linha "## Título" — senão o known-item vira match de substring
     (ambos os modelos acertariam 100% e a métrica não discrimina).
  3) MÉTRICA A — known-item (automática, objetiva): query = título (`section`) do átomo,
     alvo = o próprio átomo entre os 12k distratores. Recall@1/5/10 e MRR@10. Um bom
     embedding conecta o RÓTULO do assunto ao CONTEÚDO mesmo sem as palavras iguais.
  4) MÉTRICA B — perguntas REAIS (do SQLite): top-5 de cada modelo, lado a lado num
     relatório .md para o julgamento humano (como o ab_modelos.py faz com a prosa).

O A/B é RELATIVO: distâncias absolutas não se comparam entre modelos (escalas diferentes),
mas "qual ranqueia o átomo certo mais alto" e "qual top-5 é mais relevante" se comparam.

Uso (o servidor PODE estar rodando — isto só carrega os modelos de embedding, ~0.5GB):
    python eval/ab_embeddings.py
    python eval/ab_embeddings.py --cand intfloat/multilingual-e5-base --n-perguntas 40
    python eval/ab_embeddings.py --device cpu     # se a VRAM estiver apertada
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np  # noqa: E402

from mente_digital import textutils  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.telemetry import db  # noqa: E402

DIR = os.path.dirname(os.path.abspath(__file__))
SAIDAS = os.path.join(DIR, "saidas")


def _device(pref: str) -> str:
    if pref != "auto":
        return pref
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _prefixos(model_id: str) -> Tuple[str, str]:
    """(prefixo_query, prefixo_passagem). A família e5 EXIGE 'query:'/'passage:';
    o MiniLM e a maioria dos outros não usam prefixo."""
    low = model_id.lower()
    if "e5" in low:
        return "query: ", "passage: "
    return "", ""


def strip_heading(doc: str) -> str:
    """Remove a 1ª linha se for um cabeçalho Markdown ('## Título'), devolvendo só o
    corpo. Evita que o known-item (título -> átomo) seja um match trivial de substring."""
    linhas = doc.split("\n")
    if linhas and linhas[0].lstrip().startswith("#"):
        corpo = "\n".join(linhas[1:]).strip()
        return corpo or doc
    return doc


def carregar_corpus() -> Tuple[List[str], List[str], List[str]]:
    """Lê os átomos do Chroma de produção (só documentos/metadados). Devolve
    (titulos, corpos, fontes) alinhados por índice."""
    import chromadb

    cli = chromadb.PersistentClient(path=settings.diretorio_banco_vetorial)
    cols = cli.list_collections()
    if not cols:
        raise SystemExit("ERRO: nenhuma coleção no Chroma — o banco vetorial existe?")
    col = max(cols, key=lambda c: c.count())
    dados = col.get(include=["documents", "metadatas"])
    docs = dados["documents"] or []
    metas = dados["metadatas"] or []
    titulos: List[str] = []
    corpos: List[str] = []
    fontes: List[str] = []
    for doc, m in zip(docs, metas):
        m = m or {}
        titulo = (m.get("section") or "").strip()
        if not titulo:
            titulo = doc.split("\n", 1)[0].lstrip("# ").strip()
        titulos.append(titulo)
        corpos.append(strip_heading(doc))
        fontes.append(os.path.basename(m.get("source") or ""))
    return titulos, corpos, fontes


def embed(model_id: str, texts: List[str], prefix: str, device: str,
          batch: int = 64) -> np.ndarray:
    """Embeda `texts` (com `prefix`) e devolve vetores L2-normalizados (cosseno = dot)."""
    from sentence_transformers import SentenceTransformer

    modelo = SentenceTransformer(model_id, device=device)
    vecs = modelo.encode(
        [prefix + t for t in texts],
        batch_size=batch,
        convert_to_numpy=True,
        normalize_embeddings=True,   # cosseno vira produto escalar; justo p/ os dois
        show_progress_bar=True,
    )
    del modelo
    try:
        import gc

        import torch

        gc.collect()
        torch.cuda.empty_cache()
    except Exception:
        pass
    return vecs.astype("float32")


def perguntas_reais(n: int) -> List[str]:
    """Perguntas reais do histórico (dedup, sem as curtas demais) — as MESMAS do
    ab_modelos.py, para o A/B refletir o uso real."""
    hist = db.get_history(400) or []
    vistas = set()
    out: List[str] = []
    for t in hist:
        q = (t.get("q") or "").strip()
        chave = textutils.normaliza(q)
        if len(q) < 12 or chave in vistas:
            continue
        vistas.add(chave)
        out.append(q)
        if len(out) >= n:
            break
    return out


def known_item(titulos_vec: np.ndarray, corpus_vec: np.ndarray,
               idx: List[int]) -> dict:
    """Recall@1/5/10 e MRR@10 do known-item. `titulos_vec[j]` é a query do átomo
    `idx[j]`; o alvo é `corpus_vec[idx[j]]` entre TODOS os corpos."""
    r1 = r5 = r10 = 0
    rr = 0.0
    for j, i in enumerate(idx):
        scores = titulos_vec[j] @ corpus_vec.T           # (N,)
        rank = int((scores > scores[i]).sum())            # 0 = topo
        if rank < 1:
            r1 += 1
        if rank < 5:
            r5 += 1
        if rank < 10:
            r10 += 1
            rr += 1.0 / (rank + 1)
    n = len(idx)
    return {
        "recall@1": r1 / n, "recall@5": r5 / n, "recall@10": r10 / n,
        "mrr@10": rr / n, "n": n,
    }


def rodar(cand_id: str, n_perguntas: int, amostra: int, device: str) -> None:
    os.makedirs(SAIDAS, exist_ok=True)
    dev = _device(device)
    modelos = {
        "atual": settings.embedding_model,
        "candidato": cand_id,
    }
    print(f"Device: {dev}")
    print(f"  atual     = {modelos['atual']}")
    print(f"  candidato = {modelos['candidato']}\n")

    print("[1/4] Lendo o corpus do Chroma...")
    titulos, corpos, fontes = carregar_corpus()
    n = len(corpos)
    print(f"      {n} átomos.")

    print(f"[2/4] {n_perguntas} perguntas reais do histórico...")
    perguntas = perguntas_reais(n_perguntas)
    print(f"      {len(perguntas)} perguntas.")
    if not perguntas:
        print("      AVISO: histórico vazio — só a métrica known-item vai rodar.")

    random.seed(0)  # amostra reprodutível entre execuções
    idx = sorted(random.sample(range(n), min(amostra, n)))

    resultados = {}
    dumps = {}  # tag -> lista de (pergunta, [(fonte, titulo, score, trecho)])
    for tag, mid in modelos.items():
        pq, pp = _prefixos(mid)
        print(f"\n=== {tag} :: {mid} (query='{pq}' passage='{pp}') ===")
        print("  embedando corpos...")
        corpus_vec = embed(mid, corpos, pp, dev)
        print("  embedando títulos (known-item)...")
        tit_vec = embed(mid, [titulos[i] for i in idx], pq, dev)
        ki = known_item(tit_vec, corpus_vec, idx)
        print(f"  known-item: R@1={ki['recall@1']:.3f} R@5={ki['recall@5']:.3f} "
              f"R@10={ki['recall@10']:.3f} MRR@10={ki['mrr@10']:.3f}")

        top1s, top5s = [], []
        linhas = []
        if perguntas:
            q_vec = embed(mid, perguntas, pq, dev)
            for k, pergunta in enumerate(perguntas):
                scores = q_vec[k] @ corpus_vec.T
                top = np.argsort(-scores)[:5]
                top1s.append(float(scores[top[0]]))
                top5s.append(float(scores[top].mean()))
                achados = [(fontes[t], titulos[t], float(scores[t]),
                            corpos[t][:160].replace("\n", " ")) for t in top]
                linhas.append((pergunta, achados))
        resultados[tag] = {
            "modelo": mid, "known_item": ki,
            "sim_top1_media": statistics.mean(top1s) if top1s else 0.0,
            "sim_top5_media": statistics.mean(top5s) if top5s else 0.0,
        }
        dumps[tag] = linhas

    # ---- Resumo ----
    a, b = resultados["atual"], resultados["candidato"]
    print("\n" + "=" * 78)
    print(f"{'':26}{'atual':>24}{'candidato':>24}")
    print(f"{'modelo':26}{a['modelo'][-24:]:>24}{b['modelo'][-24:]:>24}")
    print("-" * 78)
    for k in ("recall@1", "recall@5", "recall@10", "mrr@10"):
        va, vb = a["known_item"][k], b["known_item"][k]
        seta = "  <== candidato" if vb > va else ("  (atual)" if va > vb else "  =")
        print(f"{'known-item ' + k:26}{va:>24.3f}{vb:>24.3f}{seta}")
    print(f"{'sim média top-1':26}{a['sim_top1_media']:>24.3f}{b['sim_top1_media']:>24.3f}")
    print(f"{'sim média top-5':26}{a['sim_top5_media']:>24.3f}{b['sim_top5_media']:>24.3f}")
    print("=" * 78)
    print("NB: known-item é comparável entre modelos; 'sim média' NÃO é (escalas "
          "diferentes) — serve só para ver a dispersão dentro de cada modelo.")

    # ---- Relatório lado a lado (julgamento humano) ----
    if perguntas:
        ts = time.strftime("%Y%m%d_%H%M%S")
        rel = os.path.join(SAIDAS, f"ab_embeddings_{ts}.md")
        with open(rel, "w", encoding="utf-8") as f:
            f.write(f"# A/B embeddings — {time.strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write(f"- atual: `{a['modelo']}`\n- candidato: `{b['modelo']}`\n\n")
            f.write("Para cada pergunta, top-5 de cada modelo. Marque qual recuperou os "
                    "átomos mais úteis.\n\n")
            for (perg, ach_a), (_, ach_b) in zip(dumps["atual"], dumps["candidato"]):
                f.write(f"\n---\n\n## {perg}\n\n")
                for nome, ach in (("atual", ach_a), ("candidato", ach_b)):
                    f.write(f"**{nome}**\n\n")
                    for fonte, titulo, score, trecho in ach:
                        f.write(f"- `{score:.3f}` **{titulo}** ({fonte}) — {trecho}\n")
                    f.write("\n")
        print(f"\nRelatório lado a lado -> {rel}")


def main() -> None:
    p = argparse.ArgumentParser(description="A/B de modelos de embedding sobre o vault")
    p.add_argument("--cand", default="intfloat/multilingual-e5-base",
                   help="modelo candidato (default: multilingual-e5-base)")
    p.add_argument("--n-perguntas", type=int, default=40)
    p.add_argument("--amostra", type=int, default=600, help="nº de átomos no known-item")
    p.add_argument("--device", default="auto", help="auto|cuda|cpu")
    a = p.parse_args()
    rodar(a.cand, a.n_perguntas, a.amostra, a.device)


if __name__ == "__main__":
    main()
