"""
Instrumentação de latência (aditiva, 2026-07): o dono pediu "coletar quanto tempo
cada ação demorou, para testes futuros". Estes testes cobrem só o que é PURO e
determinístico SEM GPU/torch/coqui:
  - LatencyTracker.mark (timeline) e registrar_synth (acumuladores de síntese TTS);
  - a migração idempotente das colunas novas em metricas_latencia;
  - o filtro de outlier (o artefato wall-clock não polui percentis/AVG);
  - o round-trip dos campos novos por save_latency.
Segue o estilo AAA e os fakes já usados; o SQLite é sempre um DB tmp (o conftest já
redireciona MENTE_DB_TELEMETRIA, e aqui usamos Database(str(tmp_path/...))).
"""
from __future__ import annotations

import sqlite3

from mente_digital.config import settings
from mente_digital.telemetry import Database, LatencyTracker


# ---------- LatencyTracker: mark + registrar_synth (puros) ----------

def test_mark_acumula_eventos_com_nome_e_tempo():
    # Arrange: relógio determinístico — 1º valor vira t0, os seguintes são os marcos.
    valores = iter([10.0, 10.1, 10.35])
    t = LatencyTracker(clock=lambda: next(valores))

    # Act
    t.mark("lock")
    t.mark("prefill")

    # Assert: cada evento é (nome, ms-desde-t0), tempo monotônico e >= 0.
    assert t.events == [("lock", 100.0), ("prefill", 350.0)]
    assert all(ms >= 0 for _, ms in t.events)


def test_registrar_synth_soma_total_mantem_max_e_conta_frases():
    # Arrange
    t = LatencyTracker()

    # Act
    t.registrar_synth(120.0)
    t.registrar_synth(80.0)
    t.registrar_synth(200.0)

    # Assert
    assert t.tts_frases == 3
    assert t.tts_synth_ms_total == 400.0
    assert t.tts_synth_ms_max == 200.0


def test_tracker_nasce_com_campos_novos_none():
    t = LatencyTracker()
    assert t.lock_wait_ms is None and t.prefill_ms is None
    assert t.decode_tok_s_gpu is None and t.reload_frio is None
    assert t.vram_peak_mb is None
    assert t.tts_synth_ms_total is None and t.tts_synth_ms_max is None and t.tts_frases is None
    assert t.events == []


# ---------- migração idempotente ----------

def _db(tmp_path) -> Database:
    d = Database(str(tmp_path / "lat.db"))
    d.init()
    return d


def test_migracao_idempotente_cria_colunas_novas(tmp_path):
    # Arrange + Act: init duas vezes não pode quebrar (ALTER TABLE guardado por coluna).
    d = _db(tmp_path)
    d.init()

    # Assert: as colunas novas existem.
    cols = [r[1] for r in sqlite3.connect(str(tmp_path / "lat.db")).execute(
        "PRAGMA table_info(metricas_latencia)"
    ).fetchall()]
    novas = {"lock_wait_ms", "prefill_ms", "decode_tok_s_gpu", "reload_frio",
             "vram_peak_mb", "tts_synth_ms_total", "tts_synth_ms_max", "tts_frases"}
    assert novas <= set(cols)


def test_migracao_promove_banco_antigo_sem_colunas(tmp_path):
    # Arrange: um banco "antigo" com só as colunas originais + 1 linha legada.
    caminho = str(tmp_path / "antigo.db")
    con = sqlite3.connect(caminho)
    con.execute(
        "CREATE TABLE metricas_latencia (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "data_hora TEXT, rota TEXT, ttft_ms INTEGER, ttfa_ms INTEGER, total_ms INTEGER)"
    )
    con.execute(
        "INSERT INTO metricas_latencia (data_hora, rota, ttft_ms, ttfa_ms, total_ms) "
        "VALUES ('2026-01-01T00:00:00', 'banco', 1, 2, 3)"
    )
    con.commit()
    con.close()

    # Act
    d = Database(caminho)
    d.init()

    # Assert: colunas adicionadas e a linha legada (colunas novas NULL) não quebra nada.
    cols = [r[1] for r in sqlite3.connect(caminho).execute(
        "PRAGMA table_info(metricas_latencia)"
    ).fetchall()]
    assert {"lock_wait_ms", "vram_peak_mb", "tts_frases"} <= set(cols)
    assert d.latencia_percentis()["amostras"] == 1


# ---------- filtro de outlier ----------

def test_outlier_de_ttft_e_excluido_dos_percentis(tmp_path, monkeypatch):
    # Arrange: tetos default; 5 linhas sãs + 1 artefato wall-clock (id272-like).
    monkeypatch.setattr(settings, "latencia_ttft_teto_ms", 120000.0, raising=False)
    monkeypatch.setattr(settings, "latencia_tok_s_teto", 300.0, raising=False)
    d = _db(tmp_path)
    for i in range(5):
        d.save_latency("banco", ttft_ms=100 + i, ttfa_ms=200, total_ms=300,
                       decode_tok_s=100.0, n_tokens=42)
    # O envenenador: TTFT absurdo E tok/s absurdo (os dois gatilhos do saneamento).
    d.save_latency("banco", ttft_ms=346800, ttfa_ms=200, total_ms=347000,
                   decode_tok_s=2142.0, n_tokens=3)

    # Act
    p = d.latencia_percentis()

    # Assert: o outlier fica de fora — 5 amostras, p95 do TTFT são (não 346800).
    assert p["amostras"] == 5
    assert p["estagios"]["ttft_ms"]["p95"] <= 104
    assert p["estagios"]["decode_tok_s"]["p95"] == 100.0


def test_outlier_de_tok_s_sozinho_tambem_e_excluido(tmp_path, monkeypatch):
    # Arrange: TTFT ok, mas tok/s acima do teto (o outro gatilho, isolado).
    monkeypatch.setattr(settings, "latencia_ttft_teto_ms", 120000.0, raising=False)
    monkeypatch.setattr(settings, "latencia_tok_s_teto", 300.0, raising=False)
    d = _db(tmp_path)
    d.save_latency("banco", ttft_ms=100, ttfa_ms=200, total_ms=300, decode_tok_s=90.0)
    d.save_latency("banco", ttft_ms=110, ttfa_ms=200, total_ms=300, decode_tok_s=9999.0)

    # Act
    p = d.latencia_percentis()

    # Assert
    assert p["amostras"] == 1
    assert p["estagios"]["ttft_ms"]["p50"] == 100


# ---------- round-trip dos campos novos ----------

def test_save_latency_persiste_e_le_de_volta_campos_novos(tmp_path):
    # Arrange
    d = _db(tmp_path)

    # Act
    d.save_latency(
        "banco", ttft_ms=120, ttfa_ms=250, total_ms=800,
        stt_ms=90, decode_tok_s=110.0, n_tokens=50,
        vad_ms=700, extrator_ms=5, busca_ms=12,
        lock_wait_ms=3.5, prefill_ms=180.0, decode_tok_s_gpu=125.4,
        reload_frio=1, vram_peak_mb=9421.0,
        tts_synth_ms_total=640.0, tts_synth_ms_max=210.0, tts_frases=4,
    )

    # Assert
    row = sqlite3.connect(str(tmp_path / "lat.db")).execute(
        "SELECT lock_wait_ms, prefill_ms, decode_tok_s_gpu, reload_frio, vram_peak_mb, "
        "tts_synth_ms_total, tts_synth_ms_max, tts_frases FROM metricas_latencia"
    ).fetchone()
    assert row == (3.5, 180.0, 125.4, 1, 9421.0, 640.0, 210.0, 4)
