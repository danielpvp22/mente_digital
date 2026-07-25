"""Backup diário (painel 2026-07-24, ops-backup-01): vault + SQLite + .env.

Puro, sem GPU/rede: o módulo recebe caminhos e relógio injetados (tmp_path aqui).
O que se trava: conteúdo do zip, snapshot SQLite VÁLIDO (restaurável), idempotência
por nome (o "já rodou hoje" do scheduler) e a retenção dos N mais recentes.
"""
import sqlite3
import zipfile
from datetime import datetime

from mente_digital import backup


def _monta_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "a.md").write_text("# Átomo A", encoding="utf-8")
    (vault / "sub" / "b.md").write_text("# Átomo B", encoding="utf-8")
    return vault


def test_backup_zipa_vault_sqlite_e_env(tmp_path):
    vault = _monta_vault(tmp_path)
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE x(a)")
    con.execute("INSERT INTO x VALUES (1)")
    con.commit()
    con.close()
    env = tmp_path / ".env"
    env.write_text("MENTE_X=1", encoding="utf-8")

    alvo = backup.executar(vault, db, env, tmp_path / "backups",
                           datetime(2026, 7, 24, 3, 0), retencao=14)

    assert alvo.name == "mente_2026-07-24.zip"
    assert not alvo.with_suffix(".tmp").exists()  # o .tmp foi renomeado, não sobrou
    with zipfile.ZipFile(alvo) as zf:
        nomes = set(zf.namelist())
        assert {"vault/a.md", "vault/sub/b.md", "telemetria_etl.db", ".env"} <= nomes
        raw = zf.read("telemetria_etl.db")
    # O snapshot é um SQLite VÁLIDO e restaurável (API backup, não cópia crua).
    restaurado = tmp_path / "restaurado.db"
    restaurado.write_bytes(raw)
    con = sqlite3.connect(restaurado)
    assert con.execute("SELECT a FROM x").fetchone() == (1,)
    con.close()


def test_fontes_ausentes_geram_backup_parcial(tmp_path):
    # Vault inexistente + DB inexistente + sem .env: backup parcial > nenhum.
    alvo = backup.executar(tmp_path / "nao_existe", tmp_path / "nada.db", None,
                           tmp_path / "backups", datetime(2026, 7, 24), retencao=0)
    with zipfile.ZipFile(alvo) as zf:
        assert zf.namelist() == []


def test_idempotencia_por_nome_e_retencao(tmp_path):
    vault = _monta_vault(tmp_path)
    dest = tmp_path / "backups"
    for dia in range(1, 6):
        backup.executar(vault, tmp_path / "nada.db", None, dest,
                        datetime(2026, 7, dia), retencao=3)
    zips = sorted(p.name for p in dest.glob("mente_*.zip"))
    # Retenção 3: os dois primeiros dias foram podados, os 3 recentes ficam.
    assert zips == ["mente_2026-07-03.zip", "mente_2026-07-04.zip",
                    "mente_2026-07-05.zip"]
    # O contrato de idempotência do scheduler: o caminho do dia existente.
    assert backup.caminho_do_dia(dest, datetime(2026, 7, 5)).exists()
    assert not backup.caminho_do_dia(dest, datetime(2026, 7, 6)).exists()
