"""
Roteamento de pergunta DEFINICIONAL (Part A + lever B, Onda 3): "o que é X" / "quem foi
Y" / "me explica Z". Em vez de mandar TODA definição pra web (Part A puro), o app consulta
o vault e só escala pra web se ele for FRACO — menos de `definicional_min_atomos` átomos
distintos casando o tema (o Tarkov era 1 nota-piada). Vault bem coberto responde LOCAL.
Pergunta PESSOAL (meu/eu/nosso) é excluída e segue local sempre.

Prova o WIRING no _pipeline contando as buscas Banco vs web. Sem GPU/rede: tudo fake.
"""
from collections import deque

from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.rag import LocalResult
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class SpyVS:
    """Vectorstore falso que CONTA as buscas e reporta um nº de `fontes` controlável."""

    def __init__(self, fontes):
        self.search_calls = 0
        self._fontes = list(fontes)

    async def search(self, termos, texto_busca=None, economico=False):
        self.search_calls += 1
        # relevante=True; `fontes` = átomos distintos que entraram (força do vault).
        return LocalResult("átomo do banco sobre o tema", 0.3, True, list(self._fontes))

    async def sync(self):
        pass


class FakeWeb:
    def __init__(self):
        self.searches = 0

    async def search(self, termo, consulta=None):
        self.searches += 1
        return "- FONTE WEB: definição geral."

    async def prefetch(self, tema):
        return "- CONTEXTO AMPLO."


class FakeOptimizer:
    def __init__(self, termos):
        self._termos = termos

    async def optimize(self, texto, history):
        return self._termos


def _agent(monkeypatch, tmp_path, vs):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Resposta ", "real ", "aqui."])
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = vs
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = FakeOptimizer("rag conceito")
    mem = SessionMemory(settings)
    mem.conhecimento_sessao = deque()   # sem RAM: isola a decisão Banco-vs-web
    return agent, mem


async def test_definicional_vault_fraco_vai_pra_web(monkeypatch, tmp_path):
    # 1 átomo < min 3 (o caso Tarkov): o Banco é consultado mas DESCARTADO -> web.
    monkeypatch.setattr("mente_digital.agent.settings.rotear_definicional_web", True)
    monkeypatch.setattr("mente_digital.agent.settings.definicional_min_atomos", 3)
    vs = SpyVS(fontes=["so_uma_nota.md"])
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("o que é RAG?", send, mem)

    assert vs.search_calls == 1            # B RODA o local (não pula cego como o A puro)
    assert agent.ctx.web.searches == 1     # vault fraco -> escalou pra web


async def test_definicional_imperativo_nu_vai_pra_web(monkeypatch, tmp_path):
    # Teste real 2507: "explica RAG" (sem "me") ANTES caía no local e devolvia a nota-piada.
    # Agora casa o gatilho imperativo nu; vault fraco (1 átomo) escala pra web como "o que é".
    monkeypatch.setattr("mente_digital.agent.settings.rotear_definicional_web", True)
    monkeypatch.setattr("mente_digital.agent.settings.definicional_min_atomos", 3)
    vs = SpyVS(fontes=["so_uma_nota.md"])
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("explica RAG", send, mem)

    assert agent.ctx.web.searches == 1     # imperativo nu roteou igual "o que é RAG"


async def test_definicional_vault_forte_usa_local(monkeypatch, tmp_path):
    # 4 átomos >= min 3: tema bem coberto -> responde LOCAL, sem pagar web.
    monkeypatch.setattr("mente_digital.agent.settings.rotear_definicional_web", True)
    monkeypatch.setattr("mente_digital.agent.settings.definicional_min_atomos", 3)
    vs = SpyVS(fontes=["a.md", "b.md", "c.md", "d.md"])
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("o que é RAG?", send, mem)

    assert vs.search_calls == 1
    assert agent.ctx.web.searches == 0     # vault forte: local basta


async def test_min_atomos_e_o_botao_de_ajuste(monkeypatch, tmp_path):
    # O mesmo vault de 2 átomos: com min=2 passa (local), com min=3 falha (web).
    monkeypatch.setattr("mente_digital.agent.settings.rotear_definicional_web", True)
    for minimo, web_esperado in ((2, 0), (3, 1)):
        monkeypatch.setattr("mente_digital.agent.settings.definicional_min_atomos", minimo)
        vs = SpyVS(fontes=["a.md", "b.md"])
        agent, mem = _agent(monkeypatch, tmp_path, vs)
        send, _ = make_send()
        await agent.pipeline_resposta("o que é RAG?", send, mem)
        assert agent.ctx.web.searches == web_esperado, minimo


async def test_definicional_desligado_confia_no_local(monkeypatch, tmp_path):
    # Botão off: cascata normal, confia no local mesmo com 1 átomo.
    monkeypatch.setattr("mente_digital.agent.settings.rotear_definicional_web", False)
    vs = SpyVS(fontes=["so_uma.md"])
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("o que é RAG?", send, mem)

    assert agent.ctx.web.searches == 0     # off: local confiado, sem web


async def test_pergunta_pessoal_usa_local_mesmo_fraco(monkeypatch, tmp_path):
    # "reservando local pra pessoais": pergunta pessoal não é definicional -> local,
    # mesmo com 1 átomo (o gate de força só vale para definição geral).
    monkeypatch.setattr("mente_digital.agent.settings.rotear_definicional_web", True)
    monkeypatch.setattr("mente_digital.agent.settings.definicional_min_atomos", 3)
    vs = SpyVS(fontes=["so_uma.md"])
    agent, mem = _agent(monkeypatch, tmp_path, vs)
    send, _ = make_send()

    await agent.pipeline_resposta("o que é o meu projeto?", send, mem)

    assert agent.ctx.web.searches == 0     # pessoal -> local, sem web
