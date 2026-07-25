"""
Fila Offline / Disjuntor da web (#31): circuit breaker anti-shadowban.
- A máquina pura (disjuntor.py) com clock injetado.
- A integração no WebSearcher: cooldown pula o DDG, enfileira, e drena ao reabrir.
"""

import pytest

from mente_digital import disjuntor
from mente_digital.rag import WebSearcher


class _Relogio:
    """Clock monotônico controlável para o teste avançar o tempo à mão."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ----------------------- a máquina pura -----------------------
def test_abre_apos_limite_de_falhas():
    clk = _Relogio()
    d = disjuntor.Disjuntor(limite_falhas=3, cooldown_seg=900, clock=clk)
    d.registrar_falha()
    d.registrar_falha()
    assert not d.aberto()          # 2 falhas < limite 3
    d.registrar_falha()
    assert d.aberto()              # 3ª falha abre
    assert d.segundos_restantes() == 900


def test_sucesso_zera_o_contador():
    clk = _Relogio()
    d = disjuntor.Disjuntor(limite_falhas=2, cooldown_seg=900, clock=clk)
    d.registrar_falha()
    d.registrar_sucesso()          # zera antes de abrir
    d.registrar_falha()
    assert not d.aberto()          # só 1 falha desde o sucesso


def test_cooldown_expira_com_o_tempo():
    clk = _Relogio()
    d = disjuntor.Disjuntor(limite_falhas=1, cooldown_seg=900, clock=clk)
    d.registrar_falha()
    assert d.aberto()
    clk.t += 899
    assert d.aberto()              # ainda dentro da janela
    clk.t += 2
    assert not d.aberto()          # janela passou -> fecha sozinho


# ----------------------- a integração -----------------------
@pytest.mark.asyncio
async def test_cooldown_nao_toca_o_ddg_e_enfileira(monkeypatch):
    ws = WebSearcher()
    clk = _Relogio()
    ws._disjuntor = disjuntor.Disjuntor(limite_falhas=2, cooldown_seg=900, clock=clk)

    chamadas = {"n": 0}

    async def _ddg_falha(termo, k):
        chamadas["n"] += 1
        raise RuntimeError("rate limit")

    monkeypatch.setattr(ws, "_ddg", _ddg_falha)

    # Duas falhas abrem o disjuntor (cada uma chega a bater no DDG).
    await ws.search("pergunta A")
    await ws.search("pergunta B")
    assert ws._disjuntor.aberto()
    assert chamadas["n"] == 2

    # Com o disjuntor aberto, a próxima NEM chama o DDG — e vai para a fila offline.
    res = await ws.search("pergunta C")
    from mente_digital.rag import NENHUM
    assert res == NENHUM
    assert chamadas["n"] == 2                     # <- não bateu no DDG
    assert ("pergunta C", None) in list(ws._pendentes)


@pytest.mark.asyncio
async def test_drena_a_fila_quando_o_cooldown_passa(monkeypatch):
    ws = WebSearcher()
    clk = _Relogio()
    ws._disjuntor = disjuntor.Disjuntor(limite_falhas=1, cooldown_seg=900, clock=clk)

    modo = {"falha": True}
    vistos = []

    async def _ddg(termo, k):
        vistos.append(termo)
        if modo["falha"]:
            raise RuntimeError("rate limit")
        return [{"title": "t", "body": "corpo", "href": "http://x"}]

    monkeypatch.setattr(ws, "_ddg", _ddg)

    await ws.search("consulta1")                  # falha -> abre + enfileira
    assert ws._disjuntor.aberto()
    await ws.search("consulta2")                  # aberto -> enfileira sem DDG
    assert len(ws._pendentes) == 2

    # O DDG "volta" e o tempo passa: a próxima busca drena a fila em background.
    modo["falha"] = False
    clk.t += 901
    await ws.search("consulta3")                  # fecha, dispara o drain
    if ws._retry_task is not None:
        await ws._retry_task                       # espera a drenagem terminar

    # as duas pendentes foram reexecutadas e agora estão no cache
    assert ws._cache.get("consulta1") is not None
    assert ws._cache.get("consulta2") is not None
