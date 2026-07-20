"""
SQLite endurecido (painel 2026-07): WAL persistido no arquivo, escrita concorrente
sem "database is locked", e conexão FECHADA de fato ao fim de cada operação.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from telemetry import Database


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.init()
    return db


def test_init_deixa_o_banco_em_wal(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db.path)
    try:
        modo = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert modo.lower() == "wal"   # persistido no arquivo, vale p/ toda conexão futura


def test_escrita_concorrente_nao_trava(tmp_path):
    # A concorrência real do app: turno gravando enquanto scheduler/ETL escrevem.
    db = _db(tmp_path)
    erros = []

    def escreve(n: int) -> None:
        try:
            for i in range(10):
                db.save_chat(f"pergunta {n}-{i}", f"resposta {n}-{i}", f"conv-{n}")
        except Exception as exc:   # "database is locked" cairia aqui
            erros.append(exc)

    threads = [threading.Thread(target=escreve, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert erros == []
    conn = sqlite3.connect(db.path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0]
    finally:
        conn.close()
    assert total == 40   # nenhuma escrita perdida


def test_conexao_fecha_de_fato(tmp_path):
    db = _db(tmp_path)
    with db._conn() as conn:
        conn.execute("SELECT 1")
    # Fora do with a conexão está FECHADA (não à mercê do GC).
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_transacao_ainda_commita(tmp_path):
    # A semântica dos 44 call sites: commit no sucesso do bloco.
    db = _db(tmp_path)
    db.save_chat("p", "r", "c1")
    conn = sqlite3.connect(db.path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0]
    finally:
        conn.close()
    assert total == 1
