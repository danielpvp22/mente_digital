"""
Métricas do ciclo do conhecimento (painel 2026-07): eventos DEDUP/PROMOCAO/PURGA
persistidos em log_etl, snapshot diário da base (total/origem/#novo) e o bloco
'base' agregado. SQLite temporário e fakes — sem Chroma/GPU.
"""
from __future__ import annotations


import pytest

from mente_digital import prompts
from mente_digital import telemetry
from mente_digital.agent import EtlProcessor
from mente_digital.config import settings
from mente_digital.state import AppContext


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    caminho = str(tmp_path / "metricas_test.db")
    monkeypatch.setattr(telemetry.db, "path", caminho)
    telemetry.db.init()
    return telemetry.db


def test_snapshot_salva_e_agrega(db_tmp):
    db_tmp.salvar_snapshot_base(100, {"Web": 60, "Local": 40}, 25)
    base = db_tmp.metricas_base()
    assert base["snapshot"]["total"] == 100
    assert base["snapshot"]["por_origem"] == {"Web": 60, "Local": 40}
    assert base["snapshot"]["novos"] == 25


def test_snapshot_hoje_faz_throttle(db_tmp):
    assert db_tmp.snapshot_base_hoje() is False
    db_tmp.salvar_snapshot_base(10, {}, 1)
    assert db_tmp.snapshot_base_hoje() is True


def test_ciclo_7d_conta_eventos(db_tmp):
    db_tmp.log_etl("DEDUP", "atomo x", "descartado")
    db_tmp.log_etl("DEDUP", "atomo y", "descartado")
    db_tmp.log_etl("PROMOCAO", "nota.md", "tag_removida")
    db_tmp.log_etl("IDLE_WEB", "outro.md", "CONCLUIDO")   # fora do ciclo: não conta
    ciclo = db_tmp.metricas_base()["ciclo_7d"]
    assert ciclo.get("DEDUP") == 2
    assert ciclo.get("PROMOCAO") == 1
    assert "IDLE_WEB" not in ciclo


# -- snapshot no idle (EtlProcessor._snapshot_base) ----------------------------
class _FakeStore:
    # limit/offset porque o dump agora é PAGINADO (o get() inteiro estourava o teto
    # de variáveis do SQLite acima de ~32k chunks) — ver rag.dump_paginado.
    def get(self, include=None, limit=None, offset=0):
        docs = [f"átomo com {prompts.TAG_NOVO}", "átomo maduro"]
        metas = [{"origin": "Web"}, {"origin": "Local"}]
        fim = len(docs) if limit is None else offset + limit
        return {"documents": docs[offset:fim], "metadatas": metas[offset:fim]}


class _VS:
    def __init__(self):
        self._store = _FakeStore()


async def test_snapshot_base_no_idle(db_tmp):
    ctx = AppContext(settings=settings)
    ctx.vectorstore = _VS()
    etl = EtlProcessor(ctx)

    await etl._snapshot_base()

    base = db_tmp.metricas_base()
    assert base["snapshot"]["total"] == 2
    assert base["snapshot"]["novos"] == 1
    assert base["snapshot"]["por_origem"] == {"Web": 1, "Local": 1}

    # Segunda chamada no MESMO dia: throttle (não duplica a linha).
    await etl._snapshot_base()
    with __import__("sqlite3").connect(db_tmp.path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM base_snapshot").fetchone()[0]
    assert total == 1


async def test_snapshot_sem_store_e_noop(db_tmp):
    ctx = AppContext(settings=settings)
    etl = EtlProcessor(ctx)   # ctx.vectorstore None (default do AppContext)
    await etl._snapshot_base()
    assert db_tmp.metricas_base()["snapshot"] is None
