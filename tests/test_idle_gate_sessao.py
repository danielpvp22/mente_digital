"""
Gate do idle de conhecimento por SESSÃO VIVA (correção 2026-07-23).

A consolidação (atomizar/indexar/malha/unload) não pode rodar no meio da conversa — ela
atropelava a próxima pergunta. `_disparar_idle_agora` só dispara o ETL quando NÃO há sessão
conectada; com sessão viva, re-arma e espera a desconexão (itens preservados). Puro, sem loop.
"""
from mente_digital.config import settings
from mente_digital.state import AppContext


class _FakeEtl:
    async def run_idle(self, itens):
        return None


def _ctx(monkeypatch):
    ctx = AppContext(settings=settings)
    ctx.etl = _FakeEtl()
    agendadas = []
    monkeypatch.setattr(ctx, "track_task", lambda coro: agendadas.append(coro) or coro)
    return ctx, agendadas


def _nomes(agendadas):
    return [c.cr_code.co_name for c in agendadas]


def _fechar(agendadas):
    for c in agendadas:
        c.close()   # evita "coroutine never awaited" (capturadas, nunca rodadas)


def test_gate_adia_idle_com_sessao_conectada(monkeypatch):
    monkeypatch.setattr(settings, "idle_adiar_com_sessao_viva", True)
    ctx, agendadas = _ctx(monkeypatch)
    ctx.sessoes.add(object())                 # uma sessão viva
    ctx._idle_itens = ["item"]
    ctx._disparar_idle_agora()
    nomes = _nomes(agendadas)
    assert "run_idle" not in nomes            # NÃO consolidou no meio da conversa
    assert "_idle_apos_grace" in nomes        # re-armou o timer (espera a desconexão)
    assert ctx._idle_itens == ["item"]        # itens preservados (nada se perde)
    _fechar(agendadas)


def test_gate_dispara_sem_sessao_conectada(monkeypatch):
    monkeypatch.setattr(settings, "idle_adiar_com_sessao_viva", True)
    ctx, agendadas = _ctx(monkeypatch)        # ctx.sessoes vazio (desconectado)
    ctx._idle_itens = ["item"]
    ctx._disparar_idle_agora()
    assert "run_idle" in _nomes(agendadas)    # consolidou (posteriormente = após desconectar)
    assert ctx._idle_itens == []              # drenou o buffer
    _fechar(agendadas)


def test_gate_desligado_volta_ao_comportamento_antigo(monkeypatch):
    monkeypatch.setattr(settings, "idle_adiar_com_sessao_viva", False)
    ctx, agendadas = _ctx(monkeypatch)
    ctx.sessoes.add(object())                 # sessão viva, mas o botão está off
    ctx._idle_itens = ["item"]
    ctx._disparar_idle_agora()
    assert "run_idle" in _nomes(agendadas)    # botão off: dispara mesmo com sessão
    _fechar(agendadas)
