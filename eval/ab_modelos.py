"""
A/B de modelos LLM com CONTEXTO FIXO.

Por que existe: a escolha do modelo (`caminho_modelo_llama`) é a única peça do stack
que nunca foi comparada — o Qwen2.5-Coder-7B-Instruct-Uncensored é herdado do MVP.
"A prosa ficou melhor?" é infalsificável no olho: cada rodada recupera átomos
diferentes, então trocar o modelo e "sentir" compara duas coisas ao mesmo tempo.

Como isto resolve: a recuperação roda UMA vez (`--preparar`) e é serializada em
casos.json. Depois cada modelo (`--rodar`) recebe EXATAMENTE o mesmo prompt. A única
variável é o modelo. Reusa os prompts de produção (prompts.py) e o próprio
LlamaManager — então mede o caminho real (flash_attn, kv_cache_type, chat template),
não uma aproximação.

Um modelo por vez: 7B Q4 não cabe duas vezes em 10GB. Rode `--rodar` uma vez por
modelo (o processo morre e libera a VRAM entre as duas) e depois `--comparar`.

As três tarefas são as que o pipeline realmente faz:
  1. fusao    — prompt_resposta_atomos + SYS_FUSAO sobre átomos REAIS do vault.
                É o "formular texto a partir dos dados atomizados". Métrica dura:
                taxa de sentinela (dizer 'não tenho informações' COM contexto na mão
                é falha de leitura, não humildade). O resto se lê lado a lado.
  2. sintese  — prompt_sintese_conversa + SYS_SINTESE_CONVERSA sobre turnos reais.
                Métrica dura e decisiva: o modelo emite o formato (## + tags)?
                Medido no vault: 169 de 177 átomos saíram SEM a tag, o que torna
                `Agent._consolidar_fontes` um no-op. Isto reproduz esse bug.
  3. roteador — prompt_router + SYS_ROUTER. Métrica dura: JSON parseável por
                tools.parse_decisao E ferramenta correta.

Uso (o servidor precisa estar PARADO — a VRAM é a mesma):
    python eval/ab_modelos.py --preparar
    python eval/ab_modelos.py --rodar --tag atual
    python eval/ab_modelos.py --rodar --tag instruct --modelo modelos/Qwen2.5-7B-Instruct-Q4_K_M.gguf
    python eval/ab_modelos.py --comparar atual instruct
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
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from mente_digital import prompts  # noqa: E402
from mente_digital import textutils  # noqa: E402
from mente_digital import tools  # noqa: E402
from mente_digital.agent import SENTINELA_INSUF, Agent, dividir_atomos  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.llm import LlamaManager  # noqa: E402
from mente_digital.rag import EmbeddingProvider, VectorStore  # noqa: E402
from mente_digital.telemetry import db, telemetry  # noqa: E402

DIR = os.path.dirname(os.path.abspath(__file__))
CASOS = os.path.join(DIR, "casos.json")


def _saida(tag: str) -> str:
    return os.path.join(DIR, f"resultado_{tag}.json")


# Roteador: casos com gabarito. Curtos de propósito — o que se mede aqui é
# obediência de formato (JSON de uma linha) e escolha, não conhecimento.
ROTA_CASOS: List[Tuple[str, str]] = [
    ("calcula 15% de 240", "calcular"),
    ("quanto é 1024 * 32", "calcular"),
    ("que horas são agora", "hora_atual"),
    ("que dia é hoje", "hora_atual"),
    ("salva uma nota: reunião com o time amanhã às 10h", "salvar_nota"),
    ("anota que preciso revisar o pipeline de RAG", "salvar_nota"),
    ("lista as notas do vault", "listar_notas"),
    ("abre a nota sobre arquitetura", "ler_nota"),
    ("pesquisa na web sobre o Python 3.13", "buscar_web"),
    ("o que é um banco vetorial?", "responder"),
    ("como funciona o TensorRT?", "responder"),
]


# ==========================================================================
# Geração instrumentada (mede TTFT e tok/s do decode real)
# ==========================================================================
async def _gerar(llama: LlamaManager, prompt: str, system: str, max_tokens: int,
                 temperature: Optional[float] = None) -> Tuple[str, dict]:
    t0 = time.perf_counter()
    ttft: Optional[float] = None
    pedacos: List[str] = []
    async for tok in llama.stream(
        prompt, max_tokens=max_tokens, system_prompt=system, temperature=temperature
    ):
        if ttft is None:
            ttft = time.perf_counter() - t0
        pedacos.append(tok)
    dur = time.perf_counter() - t0
    n = len(pedacos)
    # tok/s do DECODE: desconta o prefill (TTFT), senão prompt longo vira "modelo lento".
    decode_s = max(dur - (ttft or 0.0), 1e-6)
    return "".join(pedacos), {
        "ttft_ms": round((ttft or dur) * 1000),
        "tokens": n,
        "tok_s": round((n - 1) / decode_s, 1) if n > 1 else 0.0,
        "total_ms": round(dur * 1000),
    }


# ==========================================================================
# FASE 1 — congela os casos (recuperação roda uma vez só)
# ==========================================================================
async def preparar(n_fusao: int, n_sintese: int) -> None:
    historico = await asyncio.to_thread(db.get_history, 400)
    if not historico:
        print("ERRO: chat_history vazio — use o assistente um pouco antes de rodar o A/B.")
        return
    print(f"[1/3] {len(historico)} turnos no SQLite.")

    # Perguntas reais, dedup, sem as curtas demais (que o QueryOptimizer trata à parte).
    vistas = set()
    perguntas: List[str] = []
    for t in historico:
        q = (t.get("q") or "").strip()
        chave = textutils.normaliza(q)
        if len(q) < 12 or chave in vistas:
            continue
        vistas.add(chave)
        perguntas.append(q)
    print(f"[2/3] {len(perguntas)} perguntas únicas. Abrindo o vault...")

    emb = EmbeddingProvider()
    await asyncio.to_thread(emb.load)
    vs = VectorStore(emb)
    await vs.open()
    if not vs.ready:
        print("ERRO: VectorStore não abriu — o banco vetorial existe?")
        return

    # Só entram casos com contexto RELEVANTE: o objetivo é medir a formulação a
    # partir dos átomos. Pergunta que escala pra web não exercita SYS_FUSAO.
    casos = []
    for q in perguntas:
        if len(casos) >= n_fusao:
            break
        termos = textutils.limpar_query(q) or q
        local = await vs.search(termos, texto_busca=q)
        if not local.relevante:
            continue
        casos.append({
            "pergunta": q,
            "termos": termos,
            "contexto": Agent._montar_contexto(local, []),  # o MESMO builder da produção
            "melhor_dist": local.melhor_dist,
            "n_fontes": len(local.fontes),
            "fontes": [os.path.basename(f) for f in local.fontes[:6]],
        })
        print(f"   + [{local.melhor_dist:.3f}] {q[:64]}")

    # Síntese: reconstrói dumps de conversa no MESMO formato de append_chat_dump.
    dumps = []
    for i in range(0, min(len(historico), n_sintese * 3), 3):
        bloco = historico[i:i + 3]
        if len(bloco) < 2:
            break
        txt = "".join(
            f"\n## [{t.get('t','')}]\n**Usuário:** {t.get('q','')}\n**Mente Digital:** {t.get('a','')}\n"
            for t in bloco
        )
        if len(txt) > 200:
            dumps.append(txt)
        if len(dumps) >= n_sintese:
            break

    payload = {
        "gerado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
        "vault": settings.caminho_obsidian,
        "embedding": settings.embedding_model,
        "gate": {
            "rag_score_confident": settings.rag_score_confident,
            "rag_top_k": settings.rag_top_k,
            "rag_max_chunks": settings.rag_max_chunks,
        },
        "fusao": casos,
        "sintese": dumps,
        "roteador": [{"texto": t, "esperado": e} for t, e in ROTA_CASOS],
    }
    with open(CASOS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[3/3] {CASOS}: {len(casos)} casos de fusão, {len(dumps)} dumps, {len(ROTA_CASOS)} rotas.")


# ==========================================================================
# FASE 2 — roda UM modelo sobre os casos congelados
# ==========================================================================
async def rodar(tag: str, modelo: Optional[str], no_think: bool = False) -> None:
    if not os.path.exists(CASOS):
        print(f"ERRO: {CASOS} não existe. Rode --preparar primeiro.")
        return
    with open(CASOS, encoding="utf-8") as f:
        casos = json.load(f)

    if modelo:
        if not os.path.exists(modelo):
            print(f"ERRO: modelo não encontrado: {modelo}")
            return
        settings.caminho_modelo_llama = modelo  # LlamaManager lê daqui no load()
    modelo_usado = settings.caminho_modelo_llama
    print(f"=== {tag} :: {os.path.basename(modelo_usado)} "
          f"({os.path.getsize(modelo_usado)/1e9:.2f} GB) ===")

    llama = LlamaManager()
    await llama.load()
    if not llama.ready:
        print("ERRO: o modelo não carregou (VRAM? o servidor está rodando?).")
        return

    # Qwen3 pensa por padrão (<think>...); "/no_think" desliga (justo vs Qwen2.5 e
    # coerente com o uso de baixa latência). No-op para modelos que ignoram a diretiva.
    sp = (lambda s: "/no_think\n" + s) if no_think else (lambda s: s)

    out = {
        "tag": tag,
        "modelo": os.path.basename(modelo_usado),
        "modelo_path": modelo_usado,
        "rodado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
        "flash_attn": settings.flash_attn,
        "kv_cache_type": settings.kv_cache_type,
        "n_ctx": settings.n_ctx,
        "no_think": no_think,
    }

    # --- 1. FUSÃO ---------------------------------------------------------
    print(f"\n[fusão] {len(casos['fusao'])} casos...")
    fus = []
    for i, c in enumerate(casos["fusao"], 1):
        texto, m = await _gerar(
            llama,
            prompts.prompt_resposta_atomos(c["contexto"], c["pergunta"]),
            sp(prompts.SYS_FUSAO),
            settings.max_tokens_resposta,
        )
        # Mesma detecção do guard de produção (agent._responder_contexto).
        sentinela = SENTINELA_INSUF in textutils.normaliza(texto)
        fus.append({**c, **m, "resposta": texto, "sentinela": sentinela,
                    "chars": len(texto.strip())})
        print(f"   {i:2d}/{len(casos['fusao'])} {m['tok_s']:5.1f} tok/s "
              f"ttft={m['ttft_ms']:4d}ms {'SENTINELA' if sentinela else 'ok'} :: {c['pergunta'][:44]}")
    out["fusao"] = fus

    # --- 2. SÍNTESE (formato) --------------------------------------------
    print(f"\n[síntese] {len(casos['sintese'])} dumps...")
    sin = []
    for i, dump in enumerate(casos["sintese"], 1):
        texto, m = await _gerar(
            llama,
            prompts.prompt_sintese_conversa(dump),
            sp(prompts.SYS_SINTESE_CONVERSA),
            settings.max_tokens_resumo,
        )
        blocos = dividir_atomos(texto)
        # A métrica que importa: o átomo nasce utilizável pelo ciclo de vida?
        # Sem TAG_NOVO, _consolidar_fontes nunca promove (é o bug dos 169/177).
        com_novo = sum(1 for b in blocos if prompts.TAG_NOVO in b)
        com_atomo = sum(1 for b in blocos if prompts.TAG_ATOMO in b)
        nada = texto.strip().upper().strip(".!\n ") == "NADA"
        sin.append({**m, "saida": texto, "blocos": len(blocos), "com_tag_novo": com_novo,
                    "com_tag_atomo": com_atomo, "nada": nada})
        print(f"   {i}/{len(casos['sintese'])} blocos={len(blocos)} "
              f"com #conhecimento_novo={com_novo} com #zettelkasten_atomico={com_atomo}"
              f"{' [NADA]' if nada else ''}")
    out["sintese"] = sin

    # --- 3. ROTEADOR ------------------------------------------------------
    print(f"\n[roteador] {len(casos['roteador'])} casos...")
    menu = tools.criar_registry().menu()
    rot = []
    for c in casos["roteador"]:
        texto, m = await _gerar(
            llama, prompts.prompt_router(menu, c["texto"]), sp(prompts.SYS_ROUTER),
            settings.max_tokens_router, temperature=0.0,
        )
        d = tools.parse_decisao(texto)
        ok = d is not None and d.tool == c["esperado"]
        rot.append({**c, **m, "bruto": texto, "parseou": d is not None,
                    "tool": d.tool if d else None, "ok": ok})
        print(f"   {'OK ' if ok else 'ERR'} esperado={c['esperado']:<12} "
              f"veio={(d.tool if d else 'NAO-PARSEOU'):<14} :: {c['texto'][:38]}")
    out["roteador"] = rot

    with open(_saida(tag), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n-> {_saida(tag)}")
    llama.shutdown()


# ==========================================================================
# FASE 3 — compara
# ==========================================================================
def _agg(r: dict) -> dict:
    fus, sin, rot = r["fusao"], r["sintese"], r["roteador"]
    blocos = sum(s["blocos"] for s in sin)
    # Roteador: pontuar SÓ o que o gate lexical deixa passar. Uma pergunta de
    # conhecimento ("o que é um banco vetorial?") nunca chega ao roteador em
    # produção (talvez_acao=False), então errá-la é irrelevante — pontuar isso
    # mediria um caminho que não existe.
    reais = [x for x in rot if tools.talvez_acao(x["texto"])]
    # Átomos "órfãos": o modelo emitiu as tags mas esqueceu o '## ', então
    # dividir_atomos devolve 0 blocos e _salvar_atomos colapsa a síntese inteira
    # num arquivo só (mata o "um arquivo por átomo"). Falha de formato, não de conteúdo.
    orfaos = sum(1 for s in sin if s["blocos"] == 0 and not s["nada"] and s["saida"].strip())
    return {
        "tok_s": statistics.median([f["tok_s"] for f in fus] or [0]),
        "ttft_ms": statistics.median([f["ttft_ms"] for f in fus] or [0]),
        "sentinela": sum(1 for f in fus if f["sentinela"]),
        "n_fus": len(fus),
        "chars": round(statistics.median([f["chars"] for f in fus] or [0])),
        "blocos": blocos,
        "tag_novo": sum(s["com_tag_novo"] for s in sin),
        "tag_atomo": sum(s["com_tag_atomo"] for s in sin),
        "orfaos": orfaos,
        "n_sin": len(sin),
        "rota_ok": sum(1 for x in reais if x["ok"]),
        "rota_parse": sum(1 for x in reais if x["parseou"]),
        "n_rot": len(reais),
    }


def _pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100*a/b:.0f}%)" if b else "-"


def comparar(tag_a: str, tag_b: str) -> None:
    for t in (tag_a, tag_b):
        if not os.path.exists(_saida(t)):
            print(f"ERRO: falta {_saida(t)} — rode --rodar --tag {t}")
            return
    A = json.load(open(_saida(tag_a), encoding="utf-8"))
    B = json.load(open(_saida(tag_b), encoding="utf-8"))
    a, b = _agg(A), _agg(B)

    print("=" * 88)
    print(f"{'':38} {tag_a:>22} {tag_b:>22}")
    print(f"{'modelo':38} {A['modelo'][:22]:>22} {B['modelo'][:22]:>22}")
    print("-" * 88)
    print("VELOCIDADE (mediana)")
    print(f"{'  decode tok/s (maior=melhor)':38} {a['tok_s']:>22.1f} {b['tok_s']:>22.1f}")
    print(f"{'  TTFT ms (menor=melhor)':38} {a['ttft_ms']:>22.0f} {b['ttft_ms']:>22.0f}")
    print("\nPRECISÃO — FUSÃO sobre átomos reais")
    print(f"{'  sentinela COM contexto (menor=melhor)':38} {_pct(a['sentinela'],a['n_fus']):>22} {_pct(b['sentinela'],b['n_fus']):>22}")
    print(f"{'  tamanho mediano da resposta (chars)':38} {a['chars']:>22} {b['chars']:>22}")
    print("\nPRECISÃO — FORMATO DO ÁTOMO (o bug dos 169/177)")
    print(f"{'  átomos gerados (## detectado)':38} {a['blocos']:>22} {b['blocos']:>22}")
    print(f"{'  com #conhecimento_novo (maior=melhor)':38} {_pct(a['tag_novo'],a['blocos']):>22} {_pct(b['tag_novo'],b['blocos']):>22}")
    print(f"{'  com #zettelkasten_atomico':38} {_pct(a['tag_atomo'],a['blocos']):>22} {_pct(b['tag_atomo'],b['blocos']):>22}")
    print(f"{'  sínteses SEM ## (vira 1 arquivo só)':38} {_pct(a['orfaos'],a['n_sin']):>22} {_pct(b['orfaos'],b['n_sin']):>22}")
    print("\nPRECISÃO — ROTEADOR (só os casos que passam pelo gate talvez_acao)")
    print(f"{'  JSON parseável':38} {_pct(a['rota_parse'],a['n_rot']):>22} {_pct(b['rota_parse'],b['n_rot']):>22}")
    print(f"{'  ferramenta correta':38} {_pct(a['rota_ok'],a['n_rot']):>22} {_pct(b['rota_ok'],b['n_rot']):>22}")
    print("=" * 88)

    # A prosa não tem métrica automática: leia. Contexto idêntico, então a
    # diferença que você vir é do modelo, e de mais nada.
    print("\nLADO A LADO (o julgamento da prosa é seu):\n")
    for fa, fb in zip(A["fusao"], B["fusao"]):
        print("─" * 88)
        print(f"PERGUNTA: {fa['pergunta']}")
        print(f"  [{tag_a}] {fa['resposta'].strip()[:600]}")
        print(f"  [{tag_b}] {fb['resposta'].strip()[:600]}")


def main() -> None:
    p = argparse.ArgumentParser(description="A/B de modelos com contexto fixo")
    p.add_argument("--preparar", action="store_true", help="congela os casos (roda a recuperação 1x)")
    p.add_argument("--rodar", action="store_true", help="roda UM modelo sobre os casos")
    p.add_argument("--comparar", nargs=2, metavar=("A", "B"), help="compara dois resultados")
    p.add_argument("--tag", default="atual", help="nome do resultado (ex.: atual, instruct)")
    p.add_argument("--modelo", default=None, help="caminho do .gguf (default: o do settings)")
    p.add_argument("--n-fusao", type=int, default=12)
    p.add_argument("--n-sintese", type=int, default=3)
    p.add_argument("--no-think", action="store_true",
                   help="prefixa /no_think nos system prompts (Qwen3: desliga o raciocínio)")
    a = p.parse_args()

    if a.preparar:
        asyncio.run(preparar(a.n_fusao, a.n_sintese))
    elif a.rodar:
        asyncio.run(rodar(a.tag, a.modelo, a.no_think))
    elif a.comparar:
        comparar(*a.comparar)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
