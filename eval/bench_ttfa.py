"""
Bench de TTFA reprodutível + guardrail de qualidade (consultoria TTFT #6).

O que ele mede/garante — e o que NÃO:
- MEDE o overhead de ORQUESTRAÇÃO do pipeline (waterfall extrator/busca/ttft/total
  com serviços instantâneos — fakes, sem GPU/rede) e detecta REGRESSÃO: um await
  serial novo no caminho quente aparece aqui antes de aparecer no hardware.
- GARANTE os guardrails de qualidade da mesa (Otávio): sentinela NUNCA falado,
  resposta nunca vazia, rota esperada por cenário, waterfall preenchido.
- NÃO mede o hardware real: os números absolutos do TTFA de produção vêm do bloco
  `waterfall` do /api/metrics (p50/p95 por estágio), alimentado pelo uso ao vivo.

Uso (env llama-omni):
    python eval/bench_ttfa.py [--turnos 20]

Sai com código != 0 se um guardrail quebrar — dá para pendurar em CI leve.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mente_digital import textutils  # noqa: E402
from mente_digital.agent import Agent  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.prompts import SENTINELA_INSUF  # noqa: E402
from mente_digital.rag import NENHUM, LocalResult  # noqa: E402
from mente_digital.state import AppContext, SessionMemory  # noqa: E402
from mente_digital.telemetry import Database, db  # noqa: E402


# ---------- fakes (mesmo contrato dos usados na suíte, sem depender de pytest) ----------

class FakeLlama:
    def __init__(self, tokens):
        self.tokens = tokens

    def preempt(self):
        return 0

    async def stream(self, prompt, **kwargs):
        for tok in self.tokens:
            yield tok

    async def collect(self, prompt, **kwargs):
        return "".join(self.tokens)


class FakeTts:
    async def synth_base64(self, texto):
        await asyncio.sleep(0)   # o ponto de yield do to_thread real
        return None


class FakeVS:
    def __init__(self, relevante):
        self.relevante = relevante

    async def search(self, termos, texto_busca=None, economico=False, recuperados=None):
        if not self.relevante:
            return LocalResult(NENHUM, None, False, [])
        return LocalResult("átomo do banco sobre arquitetura de software", 0.12, True, [])

    async def sync(self):
        pass


class FakeWeb:
    async def search(self, termo, consulta=None, on_colheita=None):
        await asyncio.sleep(0)
        return "- FONTE WEB: conteúdo encontrado."

    async def prefetch(self, tema):
        return ""


class FakeOptimizer:
    async def optimize(self, texto, hist):
        return "arquitetura software"


def _agent(tokens_llm, vs_relevante):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(tokens_llm)
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = FakeVS(vs_relevante)
    agent = Agent(ctx)
    agent.optimizer = FakeOptimizer()
    return agent


# ---------- cenários ----------

TOKENS_RESPOSTA = ["A arquitetura ", "organiza o sistema ", "em camadas, ", "com baixo acoplamento."]
TOKENS_SENTINELA = ["Não tenho informações suficientes."]

CENARIOS = {
    # nome: (tokens do LLM, banco relevante?, rota esperada no save_latency)
    "banco": (TOKENS_RESPOSTA, True, "banco"),
    "web": (TOKENS_RESPOSTA, False, "web"),
    "sentinela": (TOKENS_SENTINELA, True, "web"),   # local dá sentinela -> escala -> graciosa
}

PERGUNTA = "me fale sobre arquitetura de software"


async def _rodar_cenario(nome, turnos):
    tokens, relevante, rota_esperada = CENARIOS[nome]
    linhas, falhas = [], []

    def _captura(*a, **k):
        linhas.append((a, k))

    db.save_latency = _captura            # singleton: captura em vez de gravar
    db.save_chat = lambda *a, **k: None
    db.save_lacuna = lambda *a, **k: None

    agent = _agent(tokens, relevante)
    for i in range(turnos):
        mem = SessionMemory(settings)     # fresca: senão a RAM do turno 1 vira rota "ram"
        mem.conhecimento_sessao = deque()
        enviados = []

        async def send(msg):
            enviados.append(msg)
            return True

        t0 = time.perf_counter()
        await agent.pipeline_resposta(PERGUNTA, send, mem)
        _ = time.perf_counter() - t0

        texto = "".join(m["texto"] for m in enviados if m.get("tipo") == "token")
        if not texto.strip():
            falhas.append(f"{nome}#{i}: resposta vazia")
        if SENTINELA_INSUF in textutils.normaliza(texto):
            falhas.append(f"{nome}#{i}: SENTINELA FALADO (vazou pro usuário)")

    if not linhas:
        falhas.append(f"{nome}: nenhum save_latency capturado")
    for a, k in linhas:
        if rota_esperada not in a[0]:
            falhas.append(f"{nome}: rota '{a[0]}' != esperada '{rota_esperada}'")
            break
    if linhas and any(k.get("extrator_ms") is None for _, k in linhas):
        falhas.append(f"{nome}: waterfall sem extrator_ms")
    if nome == "banco" and any(k.get("busca_ms") is None for _, k in linhas):
        falhas.append(f"{nome}: waterfall sem busca_ms na rota banco")
    return linhas, falhas


def _p(valores, frac):
    return Database._percentil(sorted(valores), frac)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--turnos", type=int, default=20, help="turnos por cenário")
    args = ap.parse_args()

    # dump da conversa fora do projeto (o idle não roda aqui, mas o append escreve)
    settings.arquivo_chat_dump = os.path.join(tempfile.mkdtemp(prefix="bench_ttfa_"), "dump.md")

    todas_falhas = []
    print(f"bench_ttfa — {args.turnos} turnos/cenário (fakes; overhead de orquestração)")
    print(f"{'cenário':<10} {'rota':<8} {'extrator p50/p95':<18} {'busca p50/p95':<16} "
          f"{'ttft p50/p95':<15} {'total p50/p95'}")
    for nome in CENARIOS:
        linhas, falhas = await _rodar_cenario(nome, args.turnos)
        todas_falhas.extend(falhas)

        def col(idx=None, key=None):
            vals = [
                (k.get(key) if key else a[idx])
                for a, k in linhas
                if (k.get(key) if key else a[idx]) is not None
            ]
            return f"{_p(vals, .5)}/{_p(vals, .95)}ms" if vals else "—"

        rota = linhas[0][0][0] if linhas else "—"
        print(f"{nome:<10} {rota:<8} {col(key='extrator_ms'):<18} "
              f"{col(key='busca_ms'):<16} {col(idx=1):<15} {col(idx=3)}")

    print()
    if todas_falhas:
        print("GUARDRAIL VIOLADO:")
        for f in todas_falhas:
            print(f"  - {f}")
        return 1
    print("Guardrails OK: sentinela nunca falado, respostas não-vazias, rotas esperadas, "
          "waterfall preenchido.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
