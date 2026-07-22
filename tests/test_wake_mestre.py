"""
F3 — wake-word "mestre" (modo tipo Alexa): a sessão live começa DORMENTE, só a
palavra-mestre acorda, e o silêncio faz dormir de novo. Sem WebSocket nem GPU — testa
a lógica de gate (ws.LiveSession._deve_processar / _check_sono) com fakes.
"""
import asyncio
import time

from mente_digital.config import settings
from mente_digital.state import AppContext


class _WsFake:
    def __init__(self):
        self.enviados = []

    async def accept(self):
        ...

    async def send_json(self, data):
        self.enviados.append(data)


def _sessao(monkeypatch, wake: bool):
    """LiveSession com WS/ctx falsos. `mestre_wake` é setado ANTES de construir (o
    __init__ deriva `dormente` dele). `track_task` é stubado (sem event loop no teste)."""
    from mente_digital.ws import LiveSession
    monkeypatch.setattr(settings, "mestre_wake", wake)
    ctx = AppContext(settings=settings)
    s = LiveSession(ctx, _WsFake())
    monkeypatch.setattr(ctx, "track_task", lambda coro: getattr(coro, "close", lambda: None)())
    return s


def test_wake_desligado_sempre_processa(monkeypatch):
    s = _sessao(monkeypatch, wake=False)
    assert s.dormente is False
    assert asyncio.run(s._deve_processar("qualquer coisa")) is True


def test_dormente_ignora_fala_comum(monkeypatch):
    s = _sessao(monkeypatch, wake=True)
    assert s.dormente is True
    # voz de outra pessoa por perto: não começa por "mestre" -> ignorada, segue dormindo
    assert asyncio.run(s._deve_processar("que horas são")) is False
    assert s.dormente is True


def test_mestre_com_comando_acorda_e_processa(monkeypatch):
    s = _sessao(monkeypatch, wake=True)
    ok = asyncio.run(s._deve_processar("mestre, que horas são"))
    assert ok is True
    assert s.dormente is False
    assert any(m.get("acao") == "ativar_live" for m in s.ws.enviados)   # sinal p/ o front


def test_mestre_sozinho_acorda_mas_nao_processa(monkeypatch):
    s = _sessao(monkeypatch, wake=True)
    ok = asyncio.run(s._deve_processar("mestre"))
    assert ok is False              # não vira um comando vazio (que o fluxo recusaria)
    assert s.dormente is False      # mas acordou


def test_ativo_processa_fala_comum(monkeypatch):
    s = _sessao(monkeypatch, wake=True)
    s.dormente = False              # já acordado
    assert asyncio.run(s._deve_processar("me fala sobre o tensorrt")) is True


def test_sono_apos_silencio(monkeypatch):
    s = _sessao(monkeypatch, wake=True)
    s.dormente = False              # ativo
    s.last_activity = 0.0           # muito no passado -> passou do teto de sono
    s._check_sono()
    assert s.dormente is True


def test_nao_dorme_com_pipeline_em_voo(monkeypatch):
    s = _sessao(monkeypatch, wake=True)
    s.dormente = False
    s.last_activity = 0.0

    async def _dormir():
        await asyncio.sleep(10)

    async def montar():
        s.pipeline_task = asyncio.ensure_future(_dormir())
        s._check_sono()             # usuário espera resposta: não dorme
        s.pipeline_task.cancel()

    asyncio.run(montar())
    assert s.dormente is False


def test_atividade_recente_nao_dorme(monkeypatch):
    s = _sessao(monkeypatch, wake=True)
    s.dormente = False
    s.last_activity = time.time()   # agora mesmo
    s._check_sono()
    assert s.dormente is False
