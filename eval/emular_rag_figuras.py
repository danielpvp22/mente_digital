"""EMULA o pipeline de resposta com as figuras indexadas, sem subir o main.py.

Pergunta real -> busca vetorial (MESMO embedding e5-base, MESMA metrica de
cosseno, MESMO chunking do VectorStore) sobre um corpus efemero feito das notas
de FIGURA do Cervantes + atomos de TEXTO do mesmo livro. Mostra quem ganha a
disputa, se o gate de relevancia aprovaria, e o CONTEXTO exato que iria ao LLM.

Nada e gravado: o banco de producao continua sem as figuras.
"""
import glob
import json
import random
import sys
from pathlib import Path

RAIZ = Path(r"D:\projetos\mente_digital")
sys.path.insert(0, str(RAIZ))
import os  # noqa: E402

os.chdir(RAIZ)
import numpy as np  # noqa: E402

from mente_digital import rag, textutils  # noqa: E402
from mente_digital.config import settings  # noqa: E402

PERGUNTAS = [
    "Qual a faixa de pH ideal do solo para cannabis e o que acontece fora dela?",
    "Como identificar deficiencia de magnesio nas folhas da planta?",
    "Qual a diferenca entre solo argiloso, arenoso e franco para o cultivo?",
]
N_TEXTO = 1200          # atomos de texto sorteados, para a figura ter competicao
FIG_DIR = "dados/Cerebro_Digital/Figuras/cervantesmarijuana-horticulture-medical"


def carregar(caminho, tipo):
    try:
        txt = Path(caminho).read_text(encoding="utf-8")
    except OSError:
        return []
    corpo = rag.strip_frontmatter(txt) if hasattr(rag, "strip_frontmatter") else txt
    saida = []
    for ch in rag.split_markdown(corpo, {"source": caminho}, settings.chunk_size,
                                 settings.chunk_overlap):
        conteudo = ch.page_content if hasattr(ch, "page_content") else str(ch)
        if conteudo.strip():
            saida.append({"tipo": tipo, "fonte": Path(caminho).name, "texto": conteudo})
    return saida


print("carregando corpus…", flush=True)
docs = []
for f in glob.glob(f"{FIG_DIR}/*.md"):
    docs += carregar(f, "FIGURA")
n_fig = len(docs)

txt_files = [f for f in glob.glob("dados/Cerebro_Digital/Conhecimento_Novo/*.md")]
random.seed(11)
alvo = [f for f in txt_files if "Marijuana Horticulture" in
        (Path(f).read_text(encoding="utf-8", errors="ignore")[:400])]
print(f"  atomos de texto do Cervantes disponiveis: {len(alvo)}")
for f in random.sample(alvo, min(N_TEXTO, len(alvo))):
    docs += carregar(f, "TEXTO")
print(f"  {n_fig} chunks de FIGURA + {len(docs)-n_fig} chunks de TEXTO = {len(docs)}",
      flush=True)

print("carregando e5-base (cpu)…", flush=True)
prov = rag.EmbeddingProvider()
prov.load()
emb = prov.instance                      # ja embrulhado com os prefixos e5
if emb is None:
    raise SystemExit("embeddings nao carregaram")
print("embedando o corpus (leva alguns minutos na CPU)…", flush=True)
M = np.array(emb.embed_documents([d["texto"] for d in docs]), dtype=np.float32)
M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)

rel = []
for pergunta in PERGUNTAS:
    q = np.array(emb.embed_query(pergunta), dtype=np.float32)
    q /= np.linalg.norm(q) + 1e-9
    dist = 1.0 - (M @ q)                       # distancia de cosseno, como o Chroma
    ordem = np.argsort(dist)[:10]
    chaves = textutils.palavras_chave(pergunta)
    print(f"\n{'='*78}\nPERGUNTA: {pergunta}\n{'='*78}")
    linhas = []
    for i, idx in enumerate(ordem, 1):
        d = docs[idx]
        ancorado = textutils.contem_alguma(d["texto"], chaves) \
            if hasattr(textutils, "contem_alguma") else None
        gate = dist[idx] < settings.rag_score_confident or ancorado
        print(f"{i:2}. [{d['tipo']:6}] dist={dist[idx]:.4f} "
              f"{'GATE-OK' if gate else 'gate-nao'} ancora={ancorado} {d['fonte'][:52]}")
        print(f"       {' '.join(d['texto'].split())[:150]}")
        linhas.append({"rank": i, "tipo": d["tipo"], "dist": round(float(dist[idx]), 4),
                       "gate": bool(gate), "fonte": d["fonte"],
                       "trecho": " ".join(d["texto"].split())[:300]})
    rel.append({"pergunta": pergunta, "top": linhas})

    # CONTEXTO que iria ao LLM (mesmo orcamento de chars do agente)
    ctx, usado = [], 0
    for idx in ordem:
        t = docs[idx]["texto"]
        if usado + len(t) > settings.rag_context_char_budget:
            break
        ctx.append(t)
        usado += len(t)
    figs = [c for c in ctx if "![[" in c]
    print(f"\n  --> contexto: {len(ctx)} chunks, {usado} chars, "
          f"{len(figs)} deles com imagem embutida")
    for c in figs[:2]:
        alvo_img = c.split("![[")[1].split("]]")[0]
        print(f"      imagem no contexto: {alvo_img}")

Path("eval/emulacao_figuras.json").write_text(
    json.dumps(rel, ensure_ascii=False, indent=1), encoding="utf-8")
print("\nrelatorio -> eval/emulacao_figuras.json")
