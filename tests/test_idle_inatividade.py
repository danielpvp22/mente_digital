"""
Fase de idle por INATIVIDADE (chat aberto, mas parado) e o unload guardado do modelo.

Sem WebSocket nem GPU: exercita a lógica de gate do timer (ws.LiveSession._check_
inatividade) e do descarregamento (agent.EtlProcessor.run_idle) com fakes.
"""
import asyncio


from mente_digital.agent import EtlProcessor
from mente_digital.config import settings
from mente_digital.state import AppContext


# --- timer de inatividade (ws.LiveSession._check_inatividade) ---------------
class _WsFake:
    async def accept(self): ...
    async def send_json(self, _): ...


def _sessao(monkeypatch):
    """LiveSession com WS/ctx falsos; registra se o idle foi disparado."""
    from mente_digital.ws import LiveSession
    ctx = AppContext(settings=settings)
    s = LiveSession(ctx, _WsFake())
    disparos = []
    monkeypatch.setattr(s, "_finalizar_sessao", lambda: disparos.append(True))
    return s, disparos


def test_inatividade_dispara_idle_apos_o_limite(monkeypatch):
    s, disparos = _sessao(monkeypatch)
    s.last_activity = 0.0                     # muito no passado -> passou do limite
    s._check_inatividade()
    assert disparos == [True]


def test_atividade_recente_nao_dispara(monkeypatch):
    import time
    s, disparos = _sessao(monkeypatch)
    s.last_activity = time.time()             # agora mesmo
    s._check_inatividade()
    assert disparos == []


def test_nao_dispara_com_pipeline_em_voo(monkeypatch):
    s, disparos = _sessao(monkeypatch)
    s.last_activity = 0.0

    async def _dormir():
        await asyncio.sleep(10)

    async def montar():
        s.pipeline_task = asyncio.ensure_future(_dormir())
        s._check_inatividade()               # usuário espera resposta: não interrompe
        s.pipeline_task.cancel()

    asyncio.run(montar())
    assert disparos == []                     # esperava resposta -> idle adiado


def test_nao_dispara_duas_vezes(monkeypatch):
    s, disparos = _sessao(monkeypatch)
    s.last_activity = 0.0
    s._finalizada = True                      # idle já rodou
    s._check_inatividade()
    assert disparos == []


def test_marcar_ativa_rearma_o_timer(monkeypatch):
    s, _ = _sessao(monkeypatch)
    s.last_activity = 0.0
    s._finalizada = True
    s._marcar_ativa()                         # nova mensagem
    assert s._finalizada is False
    assert s.last_activity > 0.0              # timer rearmado


# --- unload guardado (agent.EtlProcessor.run_idle) --------------------------
class _LlamaFake:
    def __init__(self):
        self.descarregou = False

    async def unload(self):
        self.descarregou = True


def _etl(monkeypatch, interativo_livre: bool):
    ctx = AppContext(settings=settings)
    ctx.llama = _LlamaFake()
    if interativo_livre:
        ctx.interactive_idle.set()            # ninguém interagindo
    else:
        ctx.interactive_idle.clear()          # usuário voltou
    etl = EtlProcessor(ctx)
    # ETL real é irrelevante aqui: o foco é o unload no fim. Stubamos TODAS as etapas
    # pesadas — inclusive a pesquisa proativa, que senão leria o DB real (não hermético)
    # e cairia em vectorstore=None quando houvesse lacunas de outros testes.
    async def _noop(*a, **k): ...
    monkeypatch.setattr(etl, "process_queue", _noop)
    monkeypatch.setattr(etl, "summarize_dump", _noop)
    monkeypatch.setattr(etl, "pesquisa_proativa", _noop)
    return etl, ctx


def test_run_idle_descarrega_o_modelo_quando_ocioso(monkeypatch):
    # Fixa o botão ON explicitamente (hermético): o default pode vir do .env local
    # como false (máquina dedicada) — este teste é sobre a LÓGICA do unload, não o default.
    monkeypatch.setattr(settings, "idle_descarregar_modelo", True)
    etl, ctx = _etl(monkeypatch, interativo_livre=True)
    asyncio.run(etl.run_idle([]))
    assert ctx.llama.descarregou is True


def test_run_idle_mantem_o_modelo_se_a_interacao_voltou(monkeypatch):
    etl, ctx = _etl(monkeypatch, interativo_livre=False)
    asyncio.run(etl.run_idle([]))
    assert ctx.llama.descarregou is False     # usuário voltou no meio -> não puxa a GPU


def test_run_idle_respeita_o_botao_de_desligar(monkeypatch):
    etl, ctx = _etl(monkeypatch, interativo_livre=True)
    monkeypatch.setattr(settings, "idle_descarregar_modelo", False)
    asyncio.run(etl.run_idle([]))
    assert ctx.llama.descarregou is False     # máquina dedicada: nunca descarrega
