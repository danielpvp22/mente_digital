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

FIDELIDADE AO PIPELINE: cada pergunta é replayada com o histórico da conversa em que foi
feita (SessionMemory.carregar_conversa, o mesmo caminho do ws.carregar_conversa), passa
pelo QueryOptimizer real (resolve pronome) e pelo _pergunta_com_contexto real. Isto não
é zelo: a 1ª versão julgou follow-ups sem histórico e INVERTEU certo e errado — ver
`casos_reais`.

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
import re
import statistics
import sys
import time
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MENTE_RAG_DEBUG", "false")

import prompts  # noqa: E402
import textutils  # noqa: E402
import tools  # noqa: E402
from agent import SENTINELA_INSUF, Agent, QueryOptimizer  # noqa: E402
from config import settings  # noqa: E402
from llm import LlamaManager  # noqa: E402
from rag import NENHUM, EmbeddingProvider, VectorStore  # noqa: E402
from state import AppContext, SessionMemory  # noqa: E402
from telemetry import db  # noqa: E402

DIR_SAIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saidas")

# Braços disponíveis. Cada um é uma configuração do caminho de RECUPERAÇÃO; o caminho
# de GERAÇÃO é idêntico em todos (mesmo prompt, mesmo modelo, temperature 0) — senão a
# comparação mediria duas coisas de uma vez.
#   cru   — a pergunta natural inteira vai ao embedding (produção hoje)
#   hyde  — o LLM atomiza a pergunta no formato da base ANTES de buscar (prompts.SYS_HYDE)
#   malha — expansão por conceito compartilhado (já medida 3x: não paga; ver config)
CONFIG_BRACO = {
    "cru": {"hyde": False, "malha": False},
    "hyde": {"hyde": True, "malha": False},
    "malha": {"hyde": False, "malha": True},
}
BRACOS: Tuple[str, ...] = ("cru", "hyde")
# id de conversa LEGADA: turnos antigos não têm `conversa_id` e o telemetry._CID os
# agrupa por dia via COALESCE -> o id vira 'YYYY-MM-DD'. Ver `casos_reais`.
_ID_LEGADO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def casos_reais(limite: int) -> List[Tuple[str, SessionMemory]]:
    """Perguntas reais COM o histórico da conversa em que foram feitas.

    Reconstruir o histórico não é luxo — sem ele o teste mede o próprio setup. Medido:
    'quanto ele acelera o processamento de imagens yolo?' foi julgada sem histórico e o
    modelo resolveu o 'ele' para *resize de imagem* (a outra coisa no contexto que
    "acelera"), produzindo 407 chars sobre 2560x1440 -> 1280x720. Parecia a resposta
    BOA do par; era a errada. A irmã explícita ('quanto o TENSOR RT acelera...') deu
    sentinela — e estava CERTA, o vault não tem esse número. Julgar follow-up sem
    histórico inverte certo e errado.

    Em produção quem resolve o pronome é o QueryOptimizer contra `mem.chat_history`, e
    quem dá contexto ao gerador é `_pergunta_com_contexto`. Aqui reusamos os dois, com
    o histórico remontado pelo `SessionMemory.carregar_conversa` — o MESMO caminho que
    o servidor usa ao reabrir uma conversa (ws.carregar_conversa).

    CUIDADO com o histórico LEGADO: `get_conversations` agrupa turnos sem `conversa_id`
    por DIA (o COALESCE em telemetry._CID), o que é ótimo para a sidebar e PÉSSIMO como
    histórico — um dia inteiro de assuntos distintos vira "uma conversa". Medido: 65 dos
    79 turnos do SQLite são legados, e reconstruí-los assim fez o QueryOptimizer extrair
    'função recursiva python tende YOLO' de uma pergunta sobre recursão, e 'yolo 2015'
    de 'como o yolo funciona?'. O vazamento era meu, não do pipeline: em produção o
    turno novo tem `conversa_id` real. Para os legados, tratamos como SEM histórico —
    que é a verdade sobre eles: não se sabe onde uma conversa terminava e a outra começava.

    Fica de fora só o que a produção nunca manda ao Banco: time-sensitive/efêmera, que
    o `talvez_tempo_real` desvia pra web antes (a 1ª versão deste script colheu 10/12
    sentinelas CORRETOS e irrelevantes medindo um caminho que não existe).
    """
    out: List[Tuple[str, SessionMemory]] = []
    vistas: set = set()
    for conv in db.get_conversations(limit=200):
        cid = str(conv["id"])
        legado = bool(_ID_LEGADO.match(cid))
        turnos = db.get_conversation(cid, limit=1000)
        anteriores: List[Tuple[str, str]] = []
        for t in turnos:
            q = (t.get("q") or "").strip()
            a = (t.get("a") or "").strip()
            chave = textutils.normaliza(q)
            if not q or chave in vistas:
                anteriores.append((q, a))
                continue
            if tools.talvez_tempo_real(q) or tools.e_efemero(q):
                anteriores.append((q, a))
                continue
            vistas.add(chave)
            mem = SessionMemory(settings)
            # Só os turnos ANTERIORES a este: o histórico que a pergunta teria de fato.
            # Legado -> lista vazia: agrupamento por dia não é conversa (ver docstring).
            mem.carregar_conversa(cid, [] if legado else list(anteriores))
            out.append((q, mem))
            anteriores.append((q, a))
    return out[:limite]


async def rodar() -> None:
    global BRACOS
    ap = argparse.ArgumentParser(description="Teste ponta a ponta da resposta")
    ap.add_argument("--n", type=int, default=20, help="nº de perguntas (2 decodes cada)")
    ap.add_argument(
        "--bracos", default=",".join(BRACOS),
        help=f"dois braços a comparar, de {sorted(CONFIG_BRACO)} (ex.: cru,malha)",
    )
    args = ap.parse_args()

    escolhidos = tuple(b.strip() for b in args.bracos.split(",") if b.strip())
    if len(escolhidos) != 2 or any(b not in CONFIG_BRACO for b in escolhidos):
        print(f"ERRO: --bracos precisa de DOIS nomes de {sorted(CONFIG_BRACO)}.")
        return
    BRACOS = escolhidos

    os.makedirs(DIR_SAIDA, exist_ok=True)
    pares = casos_reais(args.n)
    if not pares:
        print("ERRO: chat_history vazio.")
        return

    emb = EmbeddingProvider()
    await asyncio.to_thread(emb.load)
    store = VectorStore(emb)
    await store.open()
    if store._store is None:
        print("ERRO: Chroma indisponível.")
        return

    print(f"[1/3] carregando {os.path.basename(settings.caminho_modelo_llama)}...")
    llama = LlamaManager()
    await llama.load()
    if not llama.ready:
        print("ERRO: o modelo não carregou (VRAM? o servidor está rodando?).")
        return

    # O Agent REAL, com um AppContext de verdade: dá o `_pergunta_com_contexto` e o
    # `_texto_busca` (HyDE) exatamente como a produção os executa. Reimplementá-los
    # aqui seria medir a minha cópia, não o pipeline.
    ctx = AppContext(settings=settings, llama=llama, vectorstore=store)
    agente = Agent(ctx)
    otimizador = QueryOptimizer(llama)

    print(f"[2/3] montando contexto de {len(pares)} perguntas nos braços {BRACOS}...")
    casos: List[dict] = []
    for q, mem in pares:
        # CAMINHO DA PRODUÇÃO: o QueryOptimizer resolve o pronome contra o histórico
        # ('quanto ELE acelera...' -> 'tensor rt acelera yolo'), e é o `termos` que faz
        # o aterramento léxico. Pular isto foi o que inverteu certo e errado antes.
        # Roda UMA vez e é compartilhado: os braços variam a busca, não a query.
        termos = await otimizador.optimize(q, mem.chat_history)
        dados: dict = {}
        for braco in BRACOS:
            cfg = CONFIG_BRACO[braco]
            settings.rag_hyde = cfg["hyde"]
            settings.malha_expandir = cfg["malha"]
            # O tempo do _texto_busca é do BRAÇO, não do cenário: com HyDE ele embute
            # uma chamada de LLM ANTES da busca. Medir só o decode da resposta mediria
            # o ganho e esconderia o preço — e o preço é TTFA, o pilar do projeto.
            t0 = time.perf_counter()
            texto_busca = await agente._texto_busca(q, termos)
            ms_busca = round((time.perf_counter() - t0) * 1000)
            r = await store.search(termos, texto_busca=texto_busca)
            dados[braco] = {
                "contexto": r.texto,
                "chars": 0 if r.texto == NENHUM else len(r.texto),
                "relevante": r.relevante,
                "melhor_dist": r.melhor_dist,
                "vizinhos": r.texto.count("[Malha - relacionado]"),
                "ms_pre_busca": ms_busca,
                "texto_busca": texto_busca,
            }
        # Só interessa onde o Banco de fato responderia. Sem âncora local o Agent vai
        # pra web, e aí o braço não muda nada — incluir só diluiria a comparação.
        if dados[BRACOS[0]]["relevante"]:
            casos.append(
                {
                    "pergunta": q,
                    "termos": termos,
                    "turnos_historico": len(mem.chat_history),
                    # O gerador recebe a pergunta COM a conversa recente, como em produção.
                    "pergunta_resp": agente._pergunta_com_contexto(q, mem),
                    **dados,
                }
            )
    print(f"      {len(casos)}/{len(pares)} perguntas com âncora local (as outras iriam pra web).")
    if not casos:
        print("Nada a medir.")
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
                prompts.prompt_resposta_atomos(caso[braco]["contexto"], caso["pergunta_resp"]),
                prompts.SYS_FUSAO,
            )
            resultados[braco].append(
                {
                    "pergunta": caso["pergunta"],
                    "termos": caso["termos"],
                    "turnos_historico": caso["turnos_historico"],
                    "resposta": texto.strip(),
                    "sentinela": _e_sentinela(texto),
                    "chars_resposta": len(texto.strip()),
                    "chars_contexto": caso[braco]["chars"],
                    "melhor_dist": caso[braco]["melhor_dist"],
                    "vizinhos": caso[braco]["vizinhos"],
                    "ms_pre_busca": caso[braco]["ms_pre_busca"],
                    "texto_busca": caso[braco]["texto_busca"],
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
                "dist": _med([x["melhor_dist"] for x in r if x["melhor_dist"] is not None]),
                "chars_ctx": _med([x["chars_contexto"] for x in r]),
                "chars_resp": _med([x["chars_resposta"] for x in r]),
                "pre": _med([x["ms_pre_busca"] for x in r]),
                "ttft": _med([x["ttft_ms"] for x in r]),
                # O que o usuário SENTE: o pré-busca (HyDE) acontece ANTES do 1º token,
                # então ele entra inteiro no tempo até a fala começar.
                "ate_1o_token": _med([x["ms_pre_busca"] + x["ttft_ms"] for x in r]),
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
    linha("melhor_dist (med)", a["dist"], b["dist"], fmt=".3f")
    linha("chars de contexto (med)", a["chars_ctx"], b["chars_ctx"])
    linha("chars de resposta (med)", a["chars_resp"], b["chars_resp"], pior_maior=False)
    linha("ms antes da busca (med)", a["pre"], b["pre"])
    linha("TTFT ms (med)", a["ttft"], b["ttft"])
    linha("ate o 1o token ms (med)", a["ate_1o_token"], b["ate_1o_token"])
    linha("total ms (med)", a["total"], b["total"])
    print("=" * 78)
    print("\nSENTINELA é o veredito principal: menos = o vault cobriu mais a pergunta.")
    print("chars de resposta é PROXY — mais longo não é melhor. Leia as respostas:")
    for braco in BRACOS:
        print(f"  {_saida(braco)}")


if __name__ == "__main__":
    asyncio.run(rodar())
