"""
Síntese sob Demanda (#23): "o que eu sei sobre X" tem fluxo próprio (map-reduce) e não
estoura o contexto. Detector puro + orquestração com fakes.
"""
from mente_digital.agent import Agent, extrair_tema_sintese
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send, textos_de_tokens


# -- detector puro -------------------------------------------------------------
def test_extrai_tema():
    assert extrair_tema_sintese("o que eu sei sobre fotossíntese?") == "fotossíntese"
    assert extrair_tema_sintese("me resuma sobre o café") == "café"
    assert extrair_tema_sintese("faça uma síntese sobre redes neurais") == "redes neurais"


def test_nao_e_pedido_de_sintese():
    assert extrair_tema_sintese("qual a capital da França?") is None
    assert extrair_tema_sintese("o que eu sei sobre") is None   # sem tema


# -- lotes (map-reduce cabe no orçamento) --------------------------------------
def test_lotes_respeitam_orcamento():
    itens = ["a" * 400 for _ in range(10)]
    lotes = Agent._lotes_por_chars(itens, 1000)
    assert all(sum(len(x) for x in lote) <= 1000 or len(lote) == 1 for lote in lotes)
    assert sum(len(l) for l in lotes) == 10   # nenhum átomo perdido


# -- orquestração --------------------------------------------------------------
class FakeVSComAtomos:
    def __init__(self, atomos):
        self._atomos = atomos

    async def buscar_conteudos(self, query, k):
        return list(self._atomos)

    async def sync(self):
        pass


def _agent(monkeypatch, atomos):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Você ", "sabe ", "bastante ", "sobre ", "o ", "tema."])
    ctx.tts = FakeTts()
    ctx.vectorstore = FakeVSComAtomos(atomos)
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    return Agent(ctx), SessionMemory(settings)


async def test_sintese_responde_com_atomos(monkeypatch):
    atomos = [f"Átomo {i}: fato sobre o café." for i in range(20)]
    agent, mem = _agent(monkeypatch, atomos)
    send, enviados = make_send()

    await agent.pipeline_resposta("o que eu sei sobre café?", send, mem)

    # Falou uma resposta (tokens do reduce) e registrou o turno na RAM.
    assert textos_de_tokens(enviados)
    assert len(mem.chat_history) == 1


async def test_sintese_sem_atomos_e_graciosa(monkeypatch):
    agent, mem = _agent(monkeypatch, [])
    send, enviados = make_send()

    await agent.pipeline_resposta("o que eu sei sobre astrofísica?", send, mem)
    txt = textos_de_tokens(enviados)
    assert "astrofísica" in txt and "não encontrei" in txt.lower()
