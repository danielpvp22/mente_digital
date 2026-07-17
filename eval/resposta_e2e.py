"""
Teste PONTA A PONTA da resposta: a mudança melhora o que o usuário lê?

Todo o resto da bateria (ablacao_formato, o [LOCAL] melhor_dist) mede RECUPERAÇÃO —
distância de cosseno, que é proxy. Proxy não responde "a resposta ficou melhor". Este
script fecha o loop: monta o contexto real, roda a passada de fusão REAL (mesmo
prompt_resposta_atomos + SYS_FUSAO que o Agent usa no estágio Banco) e compara o texto
gerado entre dois braços.

Braços (--braco):
  sem_malha  — contexto só com os matches da busca vetorial (produção, default)
  com_malha  — + a vizinhança por conceito compartilhado (MENTE_MALHA_EXPANDIR=true)

O que mede, por pergunta:
  - sentinela: o modelo disse "não tenho informações suficientes"? É o veredito que o
    próprio pipeline usa para escalar pra web — a métrica mais honesta de "o vault
    respondeu". Menos sentinela = a base cobriu mais.
  - chars da resposta: proxy de substância (o A/B de modelos já usou mediana de chars).
    Cuidado: mais longo != melhor. Serve para detectar colapso, não para eleger vencedor.
  - ttft_ms / total_ms: o CUSTO. A malha soma ~48% de contexto -> prefill -> TTFT, que é
    o pilar que o projeto inteiro protege. Um ganho de resposta que dobre o TTFT não é
    ganho no live.

Por que os dois braços numa passada só: o contexto é montado por braço mas o modelo é
carregado UMA vez, então a comparação não mistura estado de VRAM/cache entre execuções.

As respostas cruas vão para eval/saidas/e2e_<braço>.json — leia-as. O julgamento fica
com o humano de propósito: usar o mesmo 7B local como juiz das próprias respostas é
evidência fraca, e fingir o contrário seria pior que não medir.

Uso:
    python eval/resposta_e2e.py                 # roda os dois braços e compara
    python eval/resposta_e2e.py --n 12          # menos perguntas (é caro: 2 decodes cada)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MENTE_RAG_DEBUG", "false")

import prompts  # noqa: E402
import textutils  # noqa: E402
import tools  # noqa: E402
from agent import SENTINELA_INSUF  # noqa: E402
from config import settings  # noqa: E402
from llm import LlamaManager  # noqa: E402
from rag import NENHUM, EmbeddingProvider, VectorStore  # noqa: E402
from telemetry import db  # noqa: E402

DIR_SAIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saidas")
BRACOS = ("sem_malha", "com_malha")


def _saida(braco: str) -> str:
    return os.path.join(DIR_SAIDA, f"e2e_{braco}.json")


def _e_sentinela(texto: str) -> bool:
    """Mesma régua do Agent: normaliza e procura a frase exata do sentinela."""
    return SENTINELA_INSUF in textutils.normaliza(texto)


async def _gerar(llama: LlamaManager, prompt: str, system: str) -> Tuple[str, dict]:
    """Decode medindo TTFT e total. Espelha eval/ab_modelos._gerar."""
    t0 = time.perf_counter()
    ttft: Optional[float] = None
    pedacos: List[str] = []
    async for tok in llama.stream(
        prompt, max_tokens=settings.max_tokens_resposta, system_prompt=system, temperature=0.0
    ):
        if ttft is None:
            ttft = time.perf_counter() - t0
        pedacos.append(tok)
    dur = time.perf_counter() - t0
    return "".join(pedacos), {
        "ttft_ms": round((ttft or dur) * 1000),
        "total_ms": round(dur * 1000),
    }


def perguntas_reais(limite: int) -> List[str]:
    """Perguntas do SQLite que a produção REALMENTE manda para o estágio Banco.

    Dois cortes, e ambos vieram de um erro medido: a 1ª versão deste script rodou a
    passada do Banco sobre 'clima em Lisboa amanhã' e 'cotação do dólar hoje' e colheu
    10 sentinelas em 12 — sentinelas CORRETOS (o vault não tem a previsão de amanhã),
    mas irrelevantes: o `talvez_tempo_real` desvia essas perguntas para a web antes do
    Banco. Medir o Banco nelas é medir um caminho que não existe.

    - time-sensitive / efêmera -> a produção vai direto pra web (agent.pipeline_resposta)
    - curta demais -> follow-up ('E aí', 'e o eth?') que depende do histórico da sessão,
      que este teste não reconstrói: mediria o setup, não o braço.
    """
    vistas: set = set()
    out: List[str] = []
    for t in db.get_history(limit=1000):
        q = (t.get("q") or "").strip()
        chave = textutils.normaliza(q)
        if len(q) < 12 or chave in vistas:
            continue
        vistas.add(chave)
        if tools.talvez_tempo_real(q) or tools.e_efemero(q):
            continue
        out.append(q)
    return out[:limite]


async def rodar() -> None:
    ap = argparse.ArgumentParser(description="Teste ponta a ponta da resposta")
    ap.add_argument("--n", type=int, default=20, help="nº de perguntas (2 decodes cada)")
    args = ap.parse_args()

    os.makedirs(DIR_SAIDA, exist_ok=True)
    qs = perguntas_reais(args.n)
    if not qs:
        print("ERRO: chat_history vazio.")
        return

    emb = EmbeddingProvider()
    await asyncio.to_thread(emb.load)
    store = VectorStore(emb)
    await store.open()
    if store._store is None:
        print("ERRO: Chroma indisponível.")
        return

    # CONTEXTO PRIMEIRO, MODELO DEPOIS: montar os dois contextos antes de carregar o
    # .gguf mantém os embeddings e o LLM fora da VRAM ao mesmo tempo (a 3080 tem 10 GB;
    # o commit do import documenta o llama.cpp derramando pra RAM em silêncio quando
    # falta VRAM, e um teste que mede latência não pode ser a vítima disso).
    print(f"[1/3] montando contexto de {len(qs)} perguntas nos {len(BRACOS)} braços...")
    casos: List[dict] = []
    for q in qs:
        ctx: dict = {}
        for braco in BRACOS:
            settings.malha_expandir = braco == "com_malha"
            r = await store.search(q, texto_busca=q)
            ctx[braco] = {
                "contexto": r.texto,
                "chars": 0 if r.texto == NENHUM else len(r.texto),
                "relevante": r.relevante,
                "melhor_dist": r.melhor_dist,
                "vizinhos": r.texto.count("[Malha - relacionado]"),
            }
        # Só interessa onde o Banco de fato responderia. Sem âncora local o Agent vai
        # pra web, e aí o braço não muda nada — incluir só diluiria a comparação.
        if ctx[BRACOS[0]]["relevante"]:
            casos.append({"pergunta": q, **ctx})
    print(f"      {len(casos)}/{len(qs)} perguntas com âncora local (as outras iriam pra web).")
    if not casos:
        print("Nada a medir.")
        return

    print(f"[2/3] carregando {os.path.basename(settings.caminho_modelo_llama)}...")
    llama = LlamaManager()
    await llama.load()
    if not llama.ready:
        print("ERRO: o modelo não carregou (VRAM? o servidor está rodando?).")
        return

    print("[3/3] gerando respostas (2 decodes por pergunta)...\n")
    resultados: dict = {b: [] for b in BRACOS}
    for i, caso in enumerate(casos, 1):
        # ORDEM ALTERNADA entre os pares. Com ordem fixa, a 1ª versão mediu o braço de
        # DOBRO de contexto com TTFT MENOR (201 vs 231ms) — impossível, prefill não
        # encolhe com prompt maior. Era o 2º decode se beneficiando do estado quente.
        # Alternando, o efeito de ordem cai nos dois braços em vez de premiar um.
        ordem = BRACOS if i % 2 else tuple(reversed(BRACOS))
        for braco in ordem:
            texto, m = await _gerar(
                llama,
                prompts.prompt_resposta_atomos(caso[braco]["contexto"], caso["pergunta"]),
                prompts.SYS_FUSAO,
            )
            resultados[braco].append(
                {
                    "pergunta": caso["pergunta"],
                    "resposta": texto.strip(),
                    "sentinela": _e_sentinela(texto),
                    "chars_resposta": len(texto.strip()),
                    "chars_contexto": caso[braco]["chars"],
                    "vizinhos": caso[braco]["vizinhos"],
                    **m,
                }
            )
        print(f"  [{i}/{len(casos)}] {caso['pergunta'][:58]}")

    for braco in BRACOS:
        with open(_saida(braco), "w", encoding="utf-8") as f:
            json.dump(resultados[braco], f, ensure_ascii=False, indent=1)

    _comparar(resultados)


def _med(vals: List[float]) -> float:
    return statistics.median(vals) if vals else 0.0


def _comparar(resultados: dict) -> None:
    print("\n" + "=" * 78)
    print(f"{'métrica':28} | {BRACOS[0]:>12} | {BRACOS[1]:>12} | delta")
    print("-" * 78)
    linhas = []
    for braco in BRACOS:
        r = resultados[braco]
        linhas.append(
            {
                "n": len(r),
                "sentinela": sum(1 for x in r if x["sentinela"]),
                "chars_ctx": _med([x["chars_contexto"] for x in r]),
                "chars_resp": _med([x["chars_resposta"] for x in r]),
                "ttft": _med([x["ttft_ms"] for x in r]),
                "total": _med([x["total_ms"] for x in r]),
            }
        )
    a, b = linhas

    def linha(nome: str, ka, kb, fmt: str = ".0f", pior_maior: bool = True) -> None:
        d = kb - ka
        seta = ""
        if abs(d) > 1e-9:
            ruim = (d > 0) == pior_maior
            seta = " (pior)" if ruim else " (melhor)"
        print(f"{nome:28} | {ka:12{fmt}} | {kb:12{fmt}} | {d:+.0f}{seta}")

    print(f"{'perguntas':28} | {a['n']:12d} | {b['n']:12d} |")
    linha("respostas com sentinela", a["sentinela"], b["sentinela"])
    linha("chars de contexto (med)", a["chars_ctx"], b["chars_ctx"])
    linha("chars de resposta (med)", a["chars_resp"], b["chars_resp"], pior_maior=False)
    linha("TTFT ms (med)", a["ttft"], b["ttft"])
    linha("total ms (med)", a["total"], b["total"])
    print("=" * 78)
    print("\nSENTINELA é o veredito principal: menos = o vault cobriu mais a pergunta.")
    print("chars de resposta é PROXY — mais longo não é melhor. Leia as respostas:")
    for braco in BRACOS:
        print(f"  {_saida(braco)}")


if __name__ == "__main__":
    asyncio.run(rodar())
