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
        self.buscas = 0

    async def search(self, termo, consulta=None, on_colheita=None):
        self.buscas += 1
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


async def test_comando_desconhecido_nao_registra_em_sigilo(monkeypatch, tmp_path):
    """Painel 2026-07-24: `mestre_nao_reconhecido` era o ÚNICO vizinho do fluxo-mestre
    sem o gate de confidencial — o texto do comando persistia íntegro em sigilo."""
    agent, mem, dump, salvos = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    registrados = []
    monkeypatch.setattr(
        "mente_digital.comandos_mestre.db.registrar_comando_desconhecido",
        lambda c: registrados.append(c),
    )

    await agent.pipeline_resposta("mestre, modo sigiloso", send, mem)
    await agent.pipeline_resposta("mestre, dança para mim agora", send, mem)
    assert registrados == []          # em sigilo, nem o texto do comando persiste

    await agent.pipeline_resposta("mestre, modo normal", send, mem)
    await agent.pipeline_resposta("mestre, dança para mim agora", send, mem)
    assert len(registrados) == 1      # fora do sigilo, a revisão continua alimentada


# --- priv-01 (painel 2026-07-24): sigilo NÃO escala pra web -------------------
async def test_sigilo_bloqueia_escalada_web(monkeypatch, tmp_path):
    """A promessa falada é 'fica só nesta sessão' — mas a escalada mandava a
    pergunta pro DuckDuckGo. Em sigilo, NADA sai da máquina e o usuário ouve o
    porquê (em vez de silêncio ou resposta inventada)."""
    agent, mem, dump, salvos = _agent(monkeypatch, tmp_path)
    send, enviados = make_send()

    await agent.pipeline_resposta("mestre, modo sigiloso", send, mem)
    await agent.pipeline_resposta("qual o preço atual do bitcoin?", send, mem)

    assert agent.ctx.web.buscas == 0          # nenhuma query saiu da máquina
    falado = "".join(m.get("texto", "") for m in enviados if m.get("tipo") == "token")
    assert "não busco na web" in falado.lower()

    # Fora do sigilo, a escalada volta a funcionar (o botão não quebrou o normal).
    await agent.pipeline_resposta("mestre, modo normal", send, mem)
    await agent.pipeline_resposta("qual o preço atual do bitcoin?", send, mem)
    assert agent.ctx.web.buscas >= 1


# --- priv-02 (painel 2026-07-24): sigilo sobrevive à reconexão ----------------
async def test_sigilo_rastreado_por_conversa_no_ctx(monkeypatch, tmp_path):
    """A SessionMemory é por CONEXÃO: wifi piscando + reconexão automática criava
    uma memory nova com confidencial=False — o sigilo caía em silêncio. O id da
    conversa fica em ctx.sigilosas (RAM-only) e o set_conversa do ws restaura."""
    agent, mem, dump, salvos = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    mem.conversa_id = "c-sigilo-1"

    await agent.pipeline_resposta("mestre, modo sigiloso", send, mem)
    assert "c-sigilo-1" in agent.ctx.sigilosas

    # "Reconexão": memory nova + o restore que o handler set_conversa do ws faz.
    mem2 = SessionMemory(settings)
    mem2.conversa_id = "c-sigilo-1"
    if mem2.conversa_id in agent.ctx.sigilosas:   # a MESMA lógica do ws.py
        mem2.confidencial = True
    assert mem2.confidencial is True

    await agent.pipeline_resposta("mestre, modo normal", send, mem2)
    assert "c-sigilo-1" not in agent.ctx.sigilosas


# --- priv-03 (painel 2026-07-24): invariante executável de estanqueidade ------
async def test_invariante_nenhuma_tabela_de_conteudo_cresce_em_sigilo(monkeypatch, tmp_path):
    """Dois especialistas divergiram sobre 'o sigilo está fechado?' — auditoria
    manual não resolve isso. Este invariante roda um turno sigiloso completo contra
    o Database REAL (o de teste) e afirma que NENHUMA tabela ganhou linha, com
    whitelist explícita do gap conhecido (metricas_latencia é incondicional)."""
    import sqlite3

    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Resposta ", "curta."])
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = FakeVSVazio()
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump",
                        str(tmp_path / "dump.md"))
    agent, mem = Agent(ctx), SessionMemory(settings)
    send, _ = make_send()

    def contagens():
        con = sqlite3.connect(settings.db_telemetria)
        tabelas = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        d = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tabelas}
        con.close()
        return d

    await agent.pipeline_resposta("mestre, modo sigiloso", send, mem)
    antes = contagens()
    await agent.pipeline_resposta("qual foi meu diagnóstico médico?", send, mem)
    await agent.pipeline_resposta("mestre, faça algo impossível xyz", send, mem)
    depois = contagens()

    WHITELIST = {"metricas_latencia"}   # gap documentado: metadado, sem conteúdo
    violacoes = {t: (antes.get(t, 0), n) for t, n in depois.items()
                 if n != antes.get(t, 0) and t not in WHITELIST}
    assert violacoes == {}, f"tabelas que cresceram em sigilo: {violacoes}"


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
