"""Backup do trio insubstituível: vault + SQLite + .env (painel 2026-07-24).

POR QUE existe: o vault Obsidian é a ÚNICA cópia do conhecimento destilado — o dump
bruto da conversa é APAGADO após a atomização (etl.summarize_dump), então perder o
disco era perder a base inteira, sem recurso. O SQLite carrega agenda/hábitos/SRS/
histórico; o .env carrega a calibração real (gate/embedding), que NÃO vive nos
defaults do config. O Chroma fica DE FORA de propósito: é índice derivado,
scripts/reindexar.py o reconstrói do vault (restaurar = descompactar + reindexar).

PURO/testável no espírito de agenda.py: caminhos e relógio INJETADOS, sem settings
nem telemetry — o chamador (scheduler) decide a política e loga. O nome do arquivo
carrega a data (mente_AAAA-MM-DD.zip): "o zip de hoje existe" É a idempotência,
então nem restart nem múltiplos ticks geram backup duplicado no mesmo dia.
"""
from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional


def caminho_do_dia(destino_dir: Path, agora: datetime) -> Path:
    """Onde mora o backup de hoje. Existir = já foi feito (idempotência por nome)."""
    return Path(destino_dir) / f"mente_{agora:%Y-%m-%d}.zip"


def executar(
    vault_dir: Path,
    db_path: Path,
    env_path: Optional[Path],
    destino_dir: Path,
    agora: datetime,
    retencao: int = 14,
) -> Path:
    """Gera o zip do dia e aplica a retenção. Devolve o caminho do zip.

    Escreve num .tmp ao lado e renomeia no fim: um crash no meio nunca deixa um
    meio-zip com o nome final (que a idempotência leria como "já rodou hoje").
    Fontes ausentes (vault vazio, DB inexistente, sem .env) são toleradas — backup
    parcial é melhor que nenhum. Erros de IO propagam: o chamador loga.
    """
    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    alvo = caminho_do_dia(destino_dir, agora)
    tmp = alvo.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        vault_dir = Path(vault_dir)
        if vault_dir.is_dir():
            for arq in sorted(vault_dir.rglob("*")):
                if arq.is_file():
                    zf.write(arq, str(Path("vault") / arq.relative_to(vault_dir)))
        db_path = Path(db_path)
        if db_path.exists():
            zf.writestr("telemetria_etl.db", _snapshot_sqlite(db_path))
        if env_path and Path(env_path).exists():
            zf.write(env_path, ".env")
    tmp.replace(alvo)
    _aplicar_retencao(destino_dir, retencao)
    return alvo


def _snapshot_sqlite(db_path: Path) -> bytes:
    """Snapshot CONSISTENTE via API de backup do sqlite3 — copiar o arquivo cru com
    o -wal no ar (o app roda em WAL) entregaria um banco truncado/corrompido."""
    with tempfile.TemporaryDirectory() as td:
        destino = Path(td) / "snapshot.db"
        origem = sqlite3.connect(str(db_path))
        try:
            copia = sqlite3.connect(str(destino))
            try:
                origem.backup(copia)
            finally:
                copia.close()
        finally:
            origem.close()
        return destino.read_bytes()


def _aplicar_retencao(destino_dir: Path, retencao: int) -> List[Path]:
    """Mantém os `retencao` zips mais recentes (ordem lexical = cronológica, o nome
    é AAAA-MM-DD). retencao <= 0 desliga a poda. Devolve os apagados (p/ teste)."""
    if retencao <= 0:
        return []
    zips = sorted(Path(destino_dir).glob("mente_*.zip"))
    excedentes = zips[:-retencao] if len(zips) > retencao else []
    for z in excedentes:
        z.unlink()
    return excedentes
