"""
Waterfall de latência por estágio (consultoria TTFT #1): o tracker ganha vad/extrator/
busca, o pipeline os preenche, o SQLite os persiste (migração idempotente) e o
/api/metrics expõe p50/p95 por estágio. Sem percentil por estágio, TTFT/TTFA dizem
QUANTO demorou mas não ONDE — e otimização vira chute.
"""
from __future__ import annotations

import sqlite3
from collections import deque

from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.rag import LocalResult
from mente_digital.state import AppContext, SessionMemory
from mente_digital.telemetry import Database, LatencyTracker

from conftest import FakeLlama, FakeTts, make_send


def test_tracker_nasce_com_estagios_vazios():
    t = LatencyTracker()
    assert t.vad_ms is None and t.extrator_ms is None and t.busca_ms is None


# ---------- o pipeline preenche os estágios ----------

class _VS:
    async def search(self, termos, texto_busca=None, economico=False, recuperados=None):
        return LocalResult("átomo do banco sobre o tema", 0.3, True, [])

    async def sync(self):
        pass


class _Web:
    async def search(self, termo, consulta=None, on_colheita=None):
        return "- FONTE WEB."

    async def prefetch(self, tema):
        return ""


class _Optimizer:
    async def optimize(self, texto, hist):
        return "arquitetura software"


def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Resposta ", "aterrada ", "no ", "banco."])
    ctx.tts = FakeTts()
    ctx.web = _Web()
    ctx.vectorstore = _VS()
    capturas = []
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr(
        "mente_digital.agent.db.save_latency", lambda *a, **k: capturas.append((a, k))
    )
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = _Optimizer()
    mem = SessionMemory(settings)
    mem.conhecimento_sessao = deque()
    return agent, mem, capturas


async def test_pipeline_preenche_extrator_e_busca(monkeypatch, tmp_path):
    agent, mem, capturas = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert len(capturas) == 1
    _, kwargs = capturas[0]
    assert isinstance(kwargs["extrator_ms"], int) and kwargs["extrator_ms"] >= 0
    assert isinstance(kwargs["busca_ms"], int) and kwargs["busca_ms"] >= 0
    assert kwargs["vad_ms"] is None            # turno digitado: não há endpoint de voz


async def test_vad_e_stt_atravessam_ate_a_persistencia(monkeypatch, tmp_path):
    agent, mem, capturas = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta(
        "me fale sobre arquitetura de software", send, mem, stt_ms=120, vad_ms=700
    )

    _, kwargs = capturas[0]
    assert kwargs["vad_ms"] == 700


# ---------- persistência + percentis ----------

def _db(tmp_path) -> Database:
    d = Database(str(tmp_path / "lat.db"))
    d.init()
    return d


def test_save_e_percentis_por_estagio(tmp_path):
    d = _db(tmp_path)
    for i in range(10):
        d.save_latency(
            "banco", ttft_ms=100 + i, ttfa_ms=200 + i, total_ms=300 + i,
            stt_ms=None, decode_tok_s=100.0, n_tokens=42,
            vad_ms=700, extrator_ms=5 + i, busca_ms=10,
        )
    p = d.latencia_percentis()
    assert p["amostras"] == 10
    assert p["estagios"]["vad_ms"]["p50"] == 700
    assert p["estagios"]["busca_ms"]["p95"] == 10
    assert 100 <= p["estagios"]["ttft_ms"]["p50"] <= 109
    # estágio TODO-NULL (stt de turno digitado) simplesmente não aparece
    assert "stt_ms" not in p["estagios"]


def test_metrics_expoe_o_waterfall(tmp_path):
    d = _db(tmp_path)
    d.save_latency("web", 50, 90, 200, vad_ms=1200)
    m = d.metrics()
    assert m["waterfall"]["amostras"] == 1
    assert m["waterfall"]["estagios"]["vad_ms"]["p50"] == 1200


def test_migracao_adiciona_colunas_em_banco_antigo(tmp_path):
    caminho = str(tmp_path / "antigo.db")
    con = sqlite3.connect(caminho)
    con.execute(
        "CREATE TABLE metricas_latencia (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "data_hora TEXT, rota TEXT, ttft_ms INTEGER, ttfa_ms INTEGER, total_ms INTEGER)"
    )
    con.execute(
        "INSERT INTO metricas_latencia (data_hora, rota, ttft_ms, ttfa_ms, total_ms) "
        "VALUES ('2026-01-01T00:00:00', 'web', 1, 2, 3)"
    )
    con.commit()
    con.close()

    d = Database(caminho)
    d.init()                                   # migração idempotente
    cols = [r[1] for r in sqlite3.connect(caminho).execute(
        "PRAGMA table_info(metricas_latencia)"
    ).fetchall()]
    assert {"vad_ms", "extrator_ms", "busca_ms"} <= set(cols)
    # linha legada (colunas novas NULL) não quebra os percentis
    p = d.latencia_percentis()
    assert p["amostras"] == 1
    assert "vad_ms" not in p["estagios"]

    d.save_latency("banco", 10, 20, 30, vad_ms=700, extrator_ms=1, busca_ms=2)
    assert d.latencia_percentis()["estagios"]["vad_ms"]["p50"] == 700
