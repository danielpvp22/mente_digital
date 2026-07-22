"""
Fase (b) do QueryOptimizer (consultoria TTFT #9): quando o extrator VAI pagar um
decode LLM, a recuperação vetorial parte em paralelo e o search() recebe o resultado
pronto via `recuperados`. Fail-open em toda ponta: optimizer/store sem os métodos
novos (fakes antigos), botão desligado ou gate poupando o LLM caem no caminho serial.
"""
from __future__ import annotations

from collections import deque

from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.otimizador import QueryOptimizer
from mente_digital.rag import LocalResult
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class _Web:
    def __init__(self):
        self.searches = 0

    async def search(self, termo, consulta=None):
        self.searches += 1
        return "- FONTE WEB: genérico."

    async def prefetch(self, tema):
        return ""


class VSComRecuperar:
    """Vectorstore que expõe `recuperar` e RASTREIA o que o search() recebeu."""

    SENTINELA = [("doc-especulado", 0.3)]

    def __init__(self):
        self.recuperar_chamado = 0
        self.recebeu = "NAO_PASSOU"   # distinto de None: kwarg nem foi passado

    async def recuperar(self, consulta, k=None):
        self.recuperar_chamado += 1
        return list(self.SENTINELA)

    async def search(self, termos, texto_busca=None, economico=False, recuperados=None):
        self.recebeu = recuperados
        return LocalResult("átomo do banco sobre o tema", 0.3, True, [])

    async def sync(self):
        pass


class VSSemRecuperar:
    """O formato ANTIGO (como os fakes pré-consultoria): sem `recuperar`, search de
    3 argumentos — o pipeline precisa degradar ao serial sem nem tentar o kwarg."""

    def __init__(self):
        self.search_calls = 0

    async def search(self, termos, texto_busca=None, economico=False):
        self.search_calls += 1
        return LocalResult("átomo do banco", 0.3, True, [])

    async def sync(self):
        pass


class OptimizerQuePaga:
    def __init__(self, termos, paga=True):
        self._termos = termos
        self.paga = paga

    def pagaria_llm(self, texto, hist):
        return self.paga

    async def optimize(self, texto, hist):
        return self._termos


def _agent(monkeypatch, tmp_path, vs, optimizer):
    monkeypatch.setattr(settings, "optimizer_overlap", True)
    monkeypatch.setattr(settings, "rag_hyde", False)
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Resposta ", "real ", "do ", "banco."])
    ctx.tts = FakeTts()
    ctx.web = _Web()
    ctx.vectorstore = vs
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = optimizer
    mem = SessionMemory(settings)
    mem.conhecimento_sessao = deque()   # sem RAM: o estágio Banco tem que rodar
    return agent, mem


async def test_overlap_especula_e_entrega_ao_search(monkeypatch, tmp_path):
    vs = VSComRecuperar()
    agent, mem = _agent(monkeypatch, tmp_path, vs, OptimizerQuePaga("arquitetura software"))
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert vs.recuperar_chamado == 1
    assert vs.recebeu == VSComRecuperar.SENTINELA   # o search usou a especulação


async def test_gate_poupando_o_llm_nao_especula(monkeypatch, tmp_path):
    vs = VSComRecuperar()
    agent, mem = _agent(
        monkeypatch, tmp_path, vs, OptimizerQuePaga("arquitetura software", paga=False)
    )
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert vs.recuperar_chamado == 0
    assert vs.recebeu is None          # search chamado SEM resultado especulado


async def test_botao_desligado_nao_especula(monkeypatch, tmp_path):
    vs = VSComRecuperar()
    agent, mem = _agent(monkeypatch, tmp_path, vs, OptimizerQuePaga("arquitetura software"))
    monkeypatch.setattr(settings, "optimizer_overlap", False)
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert vs.recuperar_chamado == 0


async def test_store_antigo_sem_recuperar_degrada_ao_serial(monkeypatch, tmp_path):
    # Fakes pré-consultoria (search de 3 args): nada de kwarg novo, nada de crash.
    vs = VSSemRecuperar()
    agent, mem = _agent(monkeypatch, tmp_path, vs, OptimizerQuePaga("arquitetura software"))
    send, enviados = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert vs.search_calls == 1
    assert any(m.get("tipo") == "token" for m in enviados)


# ---------- o espelho puro pagaria_llm ----------

def test_pagaria_llm_espelha_o_gate(monkeypatch):
    monkeypatch.setattr(settings, "optimizer_gate", True)
    opt = QueryOptimizer(None)
    hist = deque([("pergunta anterior sobre esp32", "resposta")])

    # auto-contida com gate ligado: o LLM é poupado -> não especula
    assert opt.pagaria_llm("como funciona o tensor rt?", hist) is False
    # referencia o turno anterior: o LLM roda -> especula
    assert opt.pagaria_llm("e como isso se compara?", hist) is True
    # curtíssima/stopword: caminho de continuação, sem LLM
    assert opt.pagaria_llm("ok", hist) is False
    # sem histórico, mesmo com pronome, contexto é NENHUM -> gate poupa
    assert opt.pagaria_llm("e como isso se compara?", deque()) is False

    # gate DESLIGADO: até a auto-contida paga o LLM -> especula
    monkeypatch.setattr(settings, "optimizer_gate", False)
    assert opt.pagaria_llm("como funciona o tensor rt?", hist) is True
