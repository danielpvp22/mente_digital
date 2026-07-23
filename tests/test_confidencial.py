"""
Modo Confidencial (#5): "mestre, modo sigiloso" faz o turno viver só na RAM —
sem dump, sem SQLite, sem fila de ETL. O follow-up (chat_history) segue funcionando.

Sem GPU/rede: LLM, TTS, vectorstore e web são fakes.
"""
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.rag import NENHUM, LocalResult
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class FakeWeb:
    def __init__(self):
        self.prefetches = 0

    async def search(self, termo, consulta=None):
        return "- FONTE WEB: conteúdo genérico sobre o tema."

    async def prefetch(self, tema):
        self.prefetches += 1
        return "- CONTEXTO AMPLO."


class FakeVSVazio:
    async def search(self, termos, texto_busca=None, economico=False):
        return LocalResult(NENHUM, None, False)

    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Arquitetura ", "é ", "importante."])
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = FakeVSVazio()
    salvos = []
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: salvos.append(a))
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    # Hermético: não deixar o pipeline tocar o DB real do projeto (lacunas etc.).
    monkeypatch.setattr("mente_digital.agent.db.save_lacuna", lambda *a, **k: None)
    dump = tmp_path / "dump.md"
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(dump))
    return Agent(ctx), SessionMemory(settings), dump, salvos


async def test_modo_sigiloso_nao_persiste_mas_mantem_ram(monkeypatch, tmp_path):
    agent, mem, dump, salvos = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    # Liga o modo pela palavra-mestre.
    await agent.pipeline_resposta("mestre, modo sigiloso", send, mem)
    assert mem.confidencial is True
    salvos.clear()  # o próprio meta-comando não conta

    # Uma pergunta normal (não-efêmera) em modo sigiloso.
    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert salvos == []                       # nada no SQLite
    assert list(mem.fila_etl) == []           # nada pra ETL/atomização
    assert agent.ctx.web.prefetches == 0      # nem a curiosidade do pre-fetch
    assert (not dump.exists()) or dump.read_text(encoding="utf-8") == ""  # dump limpo
    assert len(mem.chat_history) >= 1         # RAM preservada -> follow-up funciona


async def test_desligar_volta_a_persistir(monkeypatch, tmp_path):
    agent, mem, dump, salvos = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, modo sigiloso", send, mem)
    await agent.pipeline_resposta("mestre, modo normal", send, mem)
    assert mem.confidencial is False
    salvos.clear()

    await agent.pipeline_resposta("me fale sobre redes neurais", send, mem)
    assert len(salvos) == 1                    # voltou a gravar no SQLite


# --- Disconnect NÃO atomiza sob confidencial (#34, privacidade) --------------
# Os guards por-turno já mantêm o sigiloso fora de dump/fila/SQLite; o FURO era o
# disconnect: LiveSession._finalizar_sessao chamava etl.run_idle() sem checar o modo,
# atomizando dump+web em átomos PERMANENTES. Agora a sessão confidencial pula o idle.
class _WsFake:
    async def accept(self): ...
    async def send_json(self, _): ...


def _live(monkeypatch, confidencial):
    from mente_digital.ws import LiveSession
    ctx = AppContext(settings=settings)
    s = LiveSession(ctx, _WsFake())
    s.memory.confidencial = confidencial
    s.memory.enfileirar_etl("tema secreto", "- conteúdo sensível")  # havia algo na fila
    rodou = []

    class _Etl:
        def run_idle(self, itens):
            rodou.append(itens)
            async def _c(): ...
            return _c()

    ctx.etl = _Etl()
    # Carência 0 = idle imediato (síncrono), p/ este teste medir o guard confidencial
    # (atomiza vs descarta), não o debounce — que tem teste próprio em test_idle_debounce.
    monkeypatch.setattr(settings, "idle_grace_seconds", 0.0)
    monkeypatch.setattr(ctx, "track_task", lambda coro: coro.close())  # não agenda no loop
    return s, rodou


def test_disconnect_confidencial_nao_dispara_idle(monkeypatch):
    s, rodou = _live(monkeypatch, confidencial=True)
    s._finalizar_sessao()
    assert rodou == []                          # run_idle NEM foi chamado -> nada atomizado
    assert list(s.memory.fila_etl) == []        # a fila foi drenada (limpa a RAM)


def test_disconnect_normal_dispara_idle(monkeypatch):
    s, rodou = _live(monkeypatch, confidencial=False)
    s._finalizar_sessao()
    assert len(rodou) == 1                       # sessão normal atomiza como sempre
    assert rodou[0] == [("tema secreto", "- conteúdo sensível")]
