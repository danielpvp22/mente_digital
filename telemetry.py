"""
Telemetria + persistência.

- TelemetryStream: motor de logs coloridos no terminal (track/warn/error).
  Agora é thread-safe (lock no write) porque tokens chegam de threads da GPU.
- Database: SQLite para log de ETL, histórico persistente (sobrevive a restart)
  e métricas. Métodos são síncronos; chame-os via asyncio.to_thread no código async.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

from config import settings


class TelemetryStream:
    _C = {
        "reset": "\033[0m", "blue": "\033[94m", "cyan": "\033[96m",
        "yellow": "\033[93m", "red": "\033[91m", "gray": "\033[90m",
    }

    def __init__(self) -> None:
        self.start_time = time.perf_counter()
        self.last_time = self.start_time
        self._lock = threading.Lock()

    @staticmethod
    def _fmt(t: float) -> str:
        return f"{t:.4f}s"

    def track(self, module: str, message: str, level: str = "INFO") -> None:
        now = time.perf_counter()
        with self._lock:
            total = now - self.start_time
            delta = now - self.last_time
            self.last_time = now
            c = self._C
            lvl = c["blue"] if level == "INFO" else c["yellow"] if level == "WARN" else c["red"]
            sys.stdout.write(
                f"{c['gray']}[{self._fmt(total)}]{c['reset']} "
                f"{c['cyan']}(+{self._fmt(delta)}){c['reset']} "
                f"{lvl}[{module}]{c['reset']} {message}\n"
            )
            sys.stdout.flush()

    def warn(self, module: str, message: str) -> None:
        self.track(module, message, "WARN")

    def error(self, module: str, message: str, exc: Optional[BaseException] = None) -> None:
        self.track(module, message, "ERROR")
        if exc is not None:
            with self._lock:
                sys.stderr.write(self._C["red"])
                traceback.print_exception(type(exc), exc, exc.__traceback__)
                sys.stderr.write(self._C["reset"] + "\n")
                sys.stderr.flush()


telemetry = TelemetryStream()


class Database:
    """SQLite fino. Uma conexão por operação (simples e seguro entre threads)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def init(self) -> None:
        with self._conn() as conn:
            c = conn.cursor()
            c.execute(
                """CREATE TABLE IF NOT EXISTS log_etl
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT,
                    tipo_acao TEXT, arquivo_gerado TEXT, status TEXT)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS chat_history
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT,
                    pergunta TEXT, resposta TEXT)"""
            )
            # Latência por resposta: TTFT (1º token) e TTFA (1º áudio) são o pilar
            # que valida a arquitetura de streaming. Sem medir, calibra-se no escuro.
            c.execute(
                """CREATE TABLE IF NOT EXISTS metricas_latencia
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT,
                    rota TEXT, ttft_ms INTEGER, ttfa_ms INTEGER, total_ms INTEGER)"""
            )
            conn.commit()

    def log_etl(self, tipo_acao: str, arquivo: str, status: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO log_etl (data_hora, tipo_acao, arquivo_gerado, status) "
                    "VALUES (?, ?, ?, ?)",
                    (datetime.now().isoformat(), tipo_acao, arquivo, status),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao gravar log de ETL", exc)

    def save_chat(self, pergunta: str, resposta: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO chat_history (data_hora, pergunta, resposta) VALUES (?, ?, ?)",
                    (datetime.now().isoformat(), pergunta, resposta),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao gravar histórico", exc)

    def save_latency(
        self,
        rota: str,
        ttft_ms: Optional[int],
        ttfa_ms: Optional[int],
        total_ms: Optional[int],
    ) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO metricas_latencia (data_hora, rota, ttft_ms, ttfa_ms, total_ms) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(), rota, ttft_ms, ttfa_ms, total_ms),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao gravar latência", exc)

    def get_history(self, limit: int = 200) -> list[dict]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT pergunta, resposta, data_hora FROM chat_history "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [{"q": q, "a": a, "t": t} for q, a, t in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler histórico", exc)
            return []

    def metrics(self) -> dict:
        try:
            with self._conn() as conn:
                total_chat = conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0]
                total_etl = conn.execute("SELECT COUNT(*) FROM log_etl").fetchone()[0]
                por_status = dict(
                    conn.execute("SELECT status, COUNT(*) FROM log_etl GROUP BY status").fetchall()
                )
                ultimos_etl = [
                    {"data": d, "tipo": ti, "arquivo": a, "status": s}
                    for d, ti, a, s in conn.execute(
                        "SELECT data_hora, tipo_acao, arquivo_gerado, status "
                        "FROM log_etl ORDER BY id DESC LIMIT 10"
                    ).fetchall()
                ]
                lat = conn.execute(
                    "SELECT COUNT(*), AVG(ttft_ms), AVG(ttfa_ms), AVG(total_ms) "
                    "FROM metricas_latencia"
                ).fetchone()
                latencia = {
                    "amostras": lat[0] or 0,
                    "ttft_ms_medio": round(lat[1]) if lat[1] is not None else None,
                    "ttfa_ms_medio": round(lat[2]) if lat[2] is not None else None,
                    "total_ms_medio": round(lat[3]) if lat[3] is not None else None,
                }
            return {
                "total_conversas": total_chat,
                "total_etl": total_etl,
                "etl_por_status": por_status,
                "ultimos_etl": ultimos_etl,
                "latencia": latencia,
            }
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao calcular métricas", exc)
            return {}


db = Database(settings.db_telemetria)
