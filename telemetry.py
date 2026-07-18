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
from datetime import datetime, timedelta
from typing import Optional

import textutils
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
                    pergunta TEXT, resposta TEXT, conversa_id TEXT)"""
            )
            # Migração: bancos antigos não têm a coluna conversa_id (agrupa turnos em
            # CONVERSAS no histórico). Adiciona se faltar — turnos legados ficam com NULL
            # e são agrupados por dia via COALESCE nas consultas abaixo.
            cols = [r[1] for r in c.execute("PRAGMA table_info(chat_history)").fetchall()]
            if "conversa_id" not in cols:
                c.execute("ALTER TABLE chat_history ADD COLUMN conversa_id TEXT")
            # Latência por resposta: TTFT (1º token) e TTFA (1º áudio) são o pilar
            # que valida a arquitetura de streaming. Sem medir, calibra-se no escuro.
            c.execute(
                """CREATE TABLE IF NOT EXISTS metricas_latencia
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT,
                    rota TEXT, ttft_ms INTEGER, ttfa_ms INTEGER, total_ms INTEGER)"""
            )
            # LACUNAS: perguntas que a RAM E o banco NÃO responderam (escalaram pra web).
            # É o sinal de "maior ponto de dúvida do app" que a pesquisa proativa do idle
            # usa para saber o que buscar e trazer pronto pra próxima vez. `chave` é a
            # forma normalizada (agrupa 'o que é X?' e 'X, o que é'); `n` acumula.
            c.execute(
                """CREATE TABLE IF NOT EXISTS lacunas
                   (chave TEXT PRIMARY KEY, termos TEXT, n INTEGER DEFAULT 1,
                    visto_em TEXT, pesquisado_em TEXT)"""
            )
            # AGENDAMENTOS: a "responsabilidade contínua" dos agentes (lembrete/alarme/
            # timer, watcher "me avise quando", briefing diário). Persistente de propósito
            # — o SchedulerService lê esta tabela e sobrevive a restart do servidor (a RAM
            # da sessão morre com a conexão e não serviria para um alarme de amanhã).
            #   tipo         : 'lembrete' | 'watcher' | 'briefing'
            #   proximo_disparo: ISO — quando disparar (lembrete) ou checar (watcher)
            #   recorrencia  : NULL (único) | 'diario' | 'semanal:<0-6>' | 'intervalo:<segs>'
            #   payload      : JSON extra (watcher: termos+condicao)
            #   status       : 'ativo' | 'pendente_entrega' | 'concluido' | 'cancelado'
            c.execute(
                """CREATE TABLE IF NOT EXISTS agendamentos
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT, mensagem TEXT,
                    proximo_disparo TEXT, recorrencia TEXT, payload TEXT,
                    status TEXT DEFAULT 'ativo', criado_em TEXT, conversa_id TEXT)"""
            )
            # Comandos com a palavra-mestre que NEM o parser rápido NEM o roteador LLM
            # reconheceram. É a lista de "melhorias a revisar": mostra que comandos o
            # usuário tentou e o app não cobre — matéria-prima para ampliar os agentes.
            # `chave` normalizada agrupa tentativas repetidas (UPSERT incrementa `n`).
            c.execute(
                """CREATE TABLE IF NOT EXISTS mestre_nao_reconhecido
                   (chave TEXT PRIMARY KEY, comando TEXT, n INTEGER DEFAULT 1, visto_em TEXT)"""
            )
            # AUDITORIA (#27): trilha das AÇÕES que os agentes executaram (lembrete criado,
            # item na lista, watcher satisfeito, briefing entregue...). É o que responde
            # "o que você fez hoje?" — transparência e confiança. Só ações com efeito;
            # leituras (listar/ler/status) não entram.
            c.execute(
                """CREATE TABLE IF NOT EXISTS auditoria
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT,
                    acao TEXT, detalhe TEXT)"""
            )
            # ATALHO DE INTENÇÃO FREQUENTE (#2): conta as intenções-mestre por forma
            # normalizada (agrupa repetições idênticas: 'diagnóstico', 'o que tem na lista
            # de compras'...). Quando `n` cruza o limiar, o app OFERECE um atalho — `sugerido`
            # garante que a oferta acontece UMA vez só (não vira nag).
            c.execute(
                """CREATE TABLE IF NOT EXISTS mestre_frequencia
                   (assinatura TEXT PRIMARY KEY, exemplo TEXT, n INTEGER DEFAULT 1,
                    sugerido INTEGER DEFAULT 0, visto_em TEXT)"""
            )
            # ATALHOS nomeados (#2): apelido -> comando-mestre completo. "mestre, atalho X"
            # grava o último comando sob o nome X; depois "mestre, X" expande de volta.
            c.execute(
                """CREATE TABLE IF NOT EXISTS mestre_atalhos
                   (nome TEXT PRIMARY KEY, comando TEXT, criado_em TEXT)"""
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

    def save_chat(self, pergunta: str, resposta: str, conversa_id: Optional[str] = None) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO chat_history (data_hora, pergunta, resposta, conversa_id) "
                    "VALUES (?, ?, ?, ?)",
                    (datetime.now().isoformat(), pergunta, resposta, conversa_id),
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

    def save_lacuna(self, chave: str, termos: str) -> None:
        """Registra (ou incrementa) uma pergunta que a RAM E o banco não responderam.

        UPSERT: a mesma dúvida recorrente sobe o contador `n` em vez de virar N linhas —
        assim a pesquisa proativa prioriza o que MAIS falta, não o que falta há mais tempo."""
        if not chave:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO lacunas (chave, termos, n, visto_em)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(chave) DO UPDATE SET n = n + 1, visto_em = excluded.visto_em""",
                    (chave, termos, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao gravar lacuna", exc)

    def get_lacunas(self, limit: int = 20, nao_pesquisadas_ha_dias: int = 7) -> list[dict]:
        """As maiores dúvidas por resolver, mais frequentes primeiro.

        Pula lacunas pesquisadas há menos de `nao_pesquisadas_ha_dias` (não re-pesquisa
        o que acabou de ser trazido). Ordena por n desc, depois recência."""
        try:
            corte = (datetime.now() - timedelta(days=nao_pesquisadas_ha_dias)).isoformat()
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT termos, n FROM lacunas
                       WHERE pesquisado_em IS NULL OR pesquisado_em < ?
                       ORDER BY n DESC, visto_em DESC LIMIT ?""",
                    (corte, limit),
                ).fetchall()
            return [{"termos": t, "n": n} for t, n in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler lacunas", exc)
            return []

    def marcar_lacuna_pesquisada(self, chave: str) -> None:
        """Carimba que a lacuna foi pesquisada — sai da fila por `nao_pesquisadas_ha_dias`."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE lacunas SET pesquisado_em = ? WHERE chave = ?",
                    (datetime.now().isoformat(), chave),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao marcar lacuna", exc)

    # ---- Agendamentos (SchedulerService: lembretes/alarmes, watchers, briefing) ----
    def criar_agendamento(
        self, tipo: str, mensagem: str, proximo_disparo: str,
        recorrencia: Optional[str] = None, payload: Optional[str] = None,
        conversa_id: Optional[str] = None,
    ) -> Optional[int]:
        """Insere um agendamento ATIVO e devolve o id (para o usuário poder cancelar)."""
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    """INSERT INTO agendamentos
                       (tipo, mensagem, proximo_disparo, recorrencia, payload, status, criado_em, conversa_id)
                       VALUES (?, ?, ?, ?, ?, 'ativo', ?, ?)""",
                    (tipo, mensagem, proximo_disparo, recorrencia, payload,
                     datetime.now().isoformat(), conversa_id),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao criar agendamento", exc)
            return None

    def get_agendamentos_vencidos(self, agora_iso: str) -> list[dict]:
        """Agendamentos ATIVOS cujo horário já chegou — o scheduler dispara estes."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT id, tipo, mensagem, proximo_disparo, recorrencia, payload, conversa_id
                       FROM agendamentos
                       WHERE status = 'ativo' AND proximo_disparo <= ?
                       ORDER BY proximo_disparo ASC""",
                    (agora_iso,),
                ).fetchall()
            return [
                {"id": i, "tipo": t, "mensagem": m, "proximo_disparo": p,
                 "recorrencia": r, "payload": pl, "conversa_id": c}
                for i, t, m, p, r, pl, c in rows
            ]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler agendamentos vencidos", exc)
            return []

    def get_agendamentos_pendentes(self) -> list[dict]:
        """Disparos que ocorreram sem ninguém conectado — entregues na próxima conexão."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT id, tipo, mensagem, proximo_disparo, recorrencia, payload, conversa_id
                       FROM agendamentos WHERE status = 'pendente_entrega'
                       ORDER BY proximo_disparo ASC""",
                ).fetchall()
            return [
                {"id": i, "tipo": t, "mensagem": m, "proximo_disparo": p,
                 "recorrencia": r, "payload": pl, "conversa_id": c}
                for i, t, m, p, r, pl, c in rows
            ]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler agendamentos pendentes", exc)
            return []

    def listar_agendamentos(self, tipos: Optional[tuple] = None) -> list[dict]:
        """Agendamentos ATIVOS (para 'liste meus lembretes'). Filtra por tipo se dado."""
        try:
            with self._conn() as conn:
                if tipos:
                    marks = ",".join("?" * len(tipos))
                    rows = conn.execute(
                        f"""SELECT id, tipo, mensagem, proximo_disparo, recorrencia FROM agendamentos
                            WHERE status = 'ativo' AND tipo IN ({marks})
                            ORDER BY proximo_disparo ASC""",
                        tuple(tipos),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, tipo, mensagem, proximo_disparo, recorrencia FROM agendamentos
                           WHERE status = 'ativo' ORDER BY proximo_disparo ASC""",
                    ).fetchall()
            return [
                {"id": i, "tipo": t, "mensagem": m, "proximo_disparo": p, "recorrencia": r}
                for i, t, m, p, r in rows
            ]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao listar agendamentos", exc)
            return []

    def atualizar_agendamento(
        self, ag_id: int, *, status: Optional[str] = None, proximo_disparo: Optional[str] = None,
    ) -> None:
        """Reprograma (próximo disparo da recorrência) ou muda o status de um agendamento."""
        campos, valores = [], []
        if status is not None:
            campos.append("status = ?")
            valores.append(status)
        if proximo_disparo is not None:
            campos.append("proximo_disparo = ?")
            valores.append(proximo_disparo)
        if not campos:
            return
        valores.append(ag_id)
        try:
            with self._conn() as conn:
                conn.execute(
                    f"UPDATE agendamentos SET {', '.join(campos)} WHERE id = ?", valores
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao atualizar agendamento", exc)

    def cancelar_agendamento(self, ag_id: int) -> bool:
        """Marca como cancelado. True se um agendamento ATIVO foi de fato cancelado."""
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE agendamentos SET status = 'cancelado' WHERE id = ? AND status = 'ativo'",
                    (ag_id,),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao cancelar agendamento", exc)
            return False

    def registrar_comando_desconhecido(self, comando: str) -> None:
        """Guarda um comando com palavra-mestre que nada reconheceu (para revisão/evolução)."""
        chave = textutils.normaliza(comando)[:120]
        if not chave:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO mestre_nao_reconhecido (chave, comando, n, visto_em)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(chave) DO UPDATE SET n = n + 1, visto_em = excluded.visto_em""",
                    (chave, comando, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao registrar comando desconhecido", exc)

    def get_comandos_desconhecidos(self, limit: int = 50) -> list[dict]:
        """Comandos de agente que o app ainda não cobre, mais tentados primeiro."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT comando, n, visto_em FROM mestre_nao_reconhecido "
                    "ORDER BY n DESC, visto_em DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [{"comando": c, "n": n, "visto_em": v} for c, n, v in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler comandos desconhecidos", exc)
            return []

    # ---- Auditoria (#27): trilha de ações dos agentes ----
    def registrar_auditoria(self, acao: str, detalhe: str) -> None:
        """Registra UMA ação com efeito (não leituras). Best-effort: falhar aqui nunca
        pode derrubar a ação em si."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO auditoria (data_hora, acao, detalhe) VALUES (?, ?, ?)",
                    (datetime.now().isoformat(), acao, detalhe[:300]),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao registrar auditoria", exc)

    def get_auditoria(self, desde_iso: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Ações registradas, mais recentes primeiro. `desde_iso` filtra por instante
        (ex.: início do dia para 'o que você fez hoje')."""
        try:
            with self._conn() as conn:
                if desde_iso:
                    rows = conn.execute(
                        "SELECT data_hora, acao, detalhe FROM auditoria "
                        "WHERE data_hora >= ? ORDER BY id DESC LIMIT ?",
                        (desde_iso, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT data_hora, acao, detalhe FROM auditoria "
                        "ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            return [{"t": t, "acao": a, "detalhe": d} for t, a, d in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler auditoria", exc)
            return []

    # ---- Atalho de intenção frequente (#2) ----
    def registrar_frequencia(self, assinatura: str, exemplo: str) -> tuple[int, bool]:
        """Conta +1 esta intenção-mestre. Devolve (n_total, ja_sugerido). Best-effort:
        em erro devolve (0, True) — 0 nunca cruza o limiar, então não sugere nada."""
        chave = (assinatura or "").strip()[:120]
        if not chave:
            return (0, True)
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO mestre_frequencia (assinatura, exemplo, n, sugerido, visto_em)
                       VALUES (?, ?, 1, 0, ?)
                       ON CONFLICT(assinatura) DO UPDATE SET n = n + 1, visto_em = excluded.visto_em""",
                    (chave, exemplo, datetime.now().isoformat()),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT n, sugerido FROM mestre_frequencia WHERE assinatura = ?", (chave,)
                ).fetchone()
            return (int(row[0]), bool(row[1])) if row else (0, True)
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao registrar frequência", exc)
            return (0, True)

    def marcar_sugerido(self, assinatura: str) -> None:
        """Marca que já ofereci um atalho para esta intenção (não oferecer de novo)."""
        chave = (assinatura or "").strip()[:120]
        if not chave:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE mestre_frequencia SET sugerido = 1 WHERE assinatura = ?", (chave,)
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao marcar sugestão", exc)

    def salvar_atalho(self, nome: str, comando: str) -> None:
        """Grava/atualiza um atalho nomeado: apelido -> comando-mestre completo."""
        chave = (nome or "").strip().lower()[:60]
        if not chave or not comando:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO mestre_atalhos (nome, comando, criado_em) VALUES (?, ?, ?)
                       ON CONFLICT(nome) DO UPDATE SET comando = excluded.comando,
                       criado_em = excluded.criado_em""",
                    (chave, comando, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao salvar atalho", exc)

    def listar_atalhos(self) -> dict:
        """Todos os atalhos (nome -> comando), para o Agent carregar em memória."""
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT nome, comando FROM mestre_atalhos").fetchall()
            return {nome: comando for nome, comando in rows}
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao listar atalhos", exc)
            return {}

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

    # Chave de conversa: usa conversa_id quando existe; para turnos legados (NULL),
    # agrupa por DIA (substr da data) — assim o histórico antigo não vira uma lista
    # infinita de fragmentos soltos.
    _CID = "COALESCE(conversa_id, substr(data_hora,1,10))"

    def get_conversations(self, limit: int = 100) -> list[dict]:
        """Histórico agrupado em CONVERSAS (não turnos soltos). Uma entrada por
        conversa: id, título (1ª pergunta), instante final e nº de turnos."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"""SELECT {self._CID} AS cid, MAX(data_hora) AS fim, COUNT(*) AS n
                        FROM chat_history GROUP BY cid ORDER BY fim DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                out = []
                for cid, fim, n in rows:
                    tr = conn.execute(
                        f"""SELECT pergunta FROM chat_history
                            WHERE {self._CID} = ? ORDER BY id ASC LIMIT 1""",
                        (cid,),
                    ).fetchone()
                    titulo = (tr[0] if tr and tr[0] else "") or "Conversa"
                    out.append({"id": cid, "titulo": titulo, "fim": fim, "n": n})
                return out
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao listar conversas", exc)
            return []

    def get_conversation(self, cid: str, limit: int = 1000) -> list[dict]:
        """Todos os turnos (pergunta/resposta) de UMA conversa, em ordem cronológica —
        para reabrir o chat e continuar de onde parou."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"""SELECT pergunta, resposta, data_hora FROM chat_history
                        WHERE {self._CID} = ? ORDER BY id ASC LIMIT ?""",
                    (cid, limit),
                ).fetchall()
            return [{"q": q, "a": a, "t": t} for q, a, t in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler conversa", exc)
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
