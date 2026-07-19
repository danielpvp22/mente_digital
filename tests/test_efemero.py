"""
Bloqueio de ingestão EFÊMERA (agent.pipeline_resposta + tools.e_efemero).

O que este arquivo protege: um dado com validade de horas (cotação, previsão do
tempo, hora) NÃO pode virar átomo permanente no Zettelkasten — mas TEM que ser
respondido normalmente e continuar visível para o follow-up na mesma sessão.

Medido no vault antes do fix: 48 dos 177 átomos auto-colhidos (27%) eram previsão
do tempo e cotação — inclusive coisas como "## Clima em Lisboa para amanhã: 19°C a
27°C", eternizadas e recuperadas para sempre pelo rag_top_k=40. UMA pergunta sobre
o tempo gerou ~40 átomos, que depois viraram os matches MAIS FORTES do vault para
qualquer pergunta sobre clima.

Sem GPU/rede: LLM, TTS, vectorstore e web são fakes.
"""
from agent import Agent
from config import settings
from rag import NENHUM, LocalResult
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class FakeWeb:
    """WebSearcher falso: sempre devolve dados, e conta os pre-fetches."""

    def __init__(self) -> None:
        self.prefetches = 0

    async def search(self, termo, consulta=None):
        return "- FONTE WEB: o dólar está a R$ 5,42 agora."

    async def prefetch(self, tema):
        self.prefetches += 1
        return "- CONTEXTO AMPLO: história do dólar."


class FakeVectorStoreVazio:
    """Banco local sem nada relevante -> o pipeline escala pra web."""

    async def search(self, termos, texto_busca=None, economico=False):
        return LocalResult(NENHUM, None, False)

    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["O ", "dólar ", "está ", "a ", "R$ ", "5,42."])
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = FakeVectorStoreVazio()
    # Hermético: sem SQLite real e sem escrever no dump do projeto.
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    dump = tmp_path / "dump.md"
    monkeypatch.setattr("agent.settings.arquivo_chat_dump", str(dump))
    return Agent(ctx), SessionMemory(settings), dump


async def test_pergunta_efemera_responde_mas_nao_vira_atomo(monkeypatch, tmp_path):
    agent, mem, dump = _agent(monkeypatch, tmp_path)
    send, enviados = make_send()

    await agent.pipeline_resposta("qual é a cotação do dólar hoje?", send, mem)

    # Responde normalmente — o bloqueio é de INGESTÃO, não de resposta.
    assert any(m.get("tipo") == "token" for m in enviados)
    # Mas nada disso pode virar Zettelkasten permanente:
    assert list(mem.fila_etl) == []      # fila do ETL intacta
    assert agent.ctx.web.prefetches == 0              # nem a "curiosidade" do pre-fetch
    assert not dump.exists() or dump.read_text(encoding="utf-8") == ""  # dump limpo


async def test_pergunta_efemera_ainda_fica_na_RAM_para_o_follow_up(monkeypatch, tmp_path):
    # A RAM morre com a sessão, então guardar ali é seguro — e é o que faz o
    # "e amanhã?" seguinte enxergar o dado que acabou de ser buscado.
    agent, mem, _dump = _agent(monkeypatch, tmp_path)
    send, _enviados = make_send()

    await agent.pipeline_resposta("qual é a cotação do dólar hoje?", send, mem)

    assert len(mem.conhecimento_sessao) == 1
    assert mem.chat_history                # turno registrado normalmente


async def test_pergunta_duravel_continua_alimentando_o_vault(monkeypatch, tmp_path):
    # O contra-teste que impede o gate de virar um bloqueio geral: a auto-colheita
    # é o coração do projeto e tem que continuar acontecendo.
    agent, mem, dump = _agent(monkeypatch, tmp_path)
    send, _enviados = make_send()

    await agent.pipeline_resposta("como o TensorRT acelera a inferência do YOLO?", send, mem)

    # DOIS itens: a busca web (_responder_web) e o contexto amplo (_prefetch) —
    # ambos viram átomo #conhecimento_novo no idle. É o caminho que faz a base
    # crescer da curiosidade do usuário, e ele tem que continuar intacto.
    assert len(mem.fila_etl) == 2
    assert agent.ctx.web.prefetches == 1                    # curiosidade preservada
    assert dump.read_text(encoding="utf-8").strip()         # resposta foi pro dump
