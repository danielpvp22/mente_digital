"""obs-01 + rot-03 (painel 2026-07-24): a instrumentação nova persiste de verdade.

obs-01: o veredito do retrieval (melhor_dist/relevante/sentinela/escalou_web) agora
tem colunas em metricas_latencia — antes a qualidade da recuperação era invisível
em produção. rot-03: cada decisão crua do roteador vira linha em router_log — o
corpus do eval de roteamento cresce com o uso. Aqui trava-se o roundtrip no
Database REAL de teste (migração idempotente incluída, pois o init já rodou).
"""
import sqlite3

from mente_digital.config import settings
from mente_digital.telemetry import db


def _ultima_linha(tabela: str, cols: str):
    con = sqlite3.connect(settings.db_telemetria)
    row = con.execute(
        f"SELECT {cols} FROM {tabela} ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return row


def test_save_latency_persiste_veredito_do_retrieval():
    db.save_latency("banco", 1100, 1500, 3000, melhor_dist=0.42, relevante=1,
                    sentinela=0, escalou_web=0)
    assert _ultima_linha(
        "metricas_latencia", "melhor_dist, relevante, sentinela, escalou_web"
    ) == (0.42, 1, 0, 0)


def test_save_router_log_roundtrip_e_truncamento():
    db.save_router_log("mestre", "liga o timer de 10 minutos", "x" * 2000,
                       "criar_lembrete", True)
    origem, texto, bruto, tool, parse_ok = _ultima_linha(
        "router_log", "origem, texto, bruto, tool, parse_ok")
    assert (origem, tool, parse_ok) == ("mestre", "criar_lembrete", 1)
    assert texto == "liga o timer de 10 minutos"
    assert len(bruto) == 800          # truncado: o valor está no par pergunta→decisão


def test_router_log_registra_parse_fail():
    db.save_router_log("pipeline", "pergunta qualquer", "prosa sem json", None, False)
    tool, parse_ok = _ultima_linha("router_log", "tool, parse_ok")
    assert tool is None and parse_ok == 0
