"""
Telemetria + persistência.

- TelemetryStream: motor de logs coloridos no terminal (track/warn/error).
  Agora é thread-safe (lock no write) porque tokens chegam de threads da GPU.
- Database: SQLite para log de ETL, histórico persistente (sobrevive a restart)
  e métricas. Métodos são síncronos; chame-os via asyncio.to_thread no código async.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Optional

from mente_digital import textutils
from mente_digital.config import settings


def _reconfig_utf8(stream: object) -> None:
    """Best-effort: força utf-8/replace no stream de log.

    Sem isso, um char fora do cp1252 (átomo vietnamita do vault, terminal Windows,
    ou stdout redirecionado p/ arquivo) faz o `write` estourar UnicodeEncodeError e
    DERRUBAR o pipeline no meio do turno (bug real). Guardado porque nem todo stream
    tem `reconfigure` (pytest capture, Python antigo) — nesses casos o _safe_write
    abaixo é a rede de segurança no ponto de escrita.
    """
    with contextlib.suppress(Exception):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]


_reconfig_utf8(sys.stdout)
_reconfig_utf8(sys.stderr)


class TelemetryStream:
    _C = {
        "reset": "\033[0m", "blue": "\033[94m", "cyan": "\033[96m",
        "yellow": "\033[93m", "red": "\033[91m", "gray": "\033[90m",
    }

    def __init__(self) -> None:
        self.start_time = time.perf_counter()
        self.last_time = self.start_time
        self._lock = threading.Lock()
        self._arquivo: Optional[object] = None
        self._arquivo_tentado = False

    def _sink(self) -> Optional[object]:
        """O arquivo de log, aberto na PRIMEIRA escrita (None = desligado/indisponível).

        Preguiçoso de propósito: abrir no import criaria o arquivo em todo processo
        que apenas importe o módulo — pytest, scripts de manutenção, o CLI. E uma
        tentativa só: se o disco recusou, não insiste a cada linha.
        """
        if self._arquivo_tentado:
            return self._arquivo
        self._arquivo_tentado = True
        if settings.log_arquivo_habilitado:
            with contextlib.suppress(OSError, ValueError):
                caminho = Path(settings.log_arquivo)
                caminho.parent.mkdir(parents=True, exist_ok=True)
                self._arquivo = caminho.open("a", encoding="utf-8", errors="replace")
        return self._arquivo

    @staticmethod
    def _fmt(t: float) -> str:
        return f"{t:.4f}s"

    @staticmethod
    def _safe_write(stream: object, text: str) -> None:
        """Escreve sem NUNCA propagar UnicodeEncodeError.

        Rede de segurança no ponto de escrita para o caso de o stream não ter sido
        reconfigurado p/ utf-8 (pytest capture, stdout cp1252 já aberto): re-sanitiza
        contra o encoding do próprio stream e escreve a versão com `replace`. Logging
        é instrumentação — não pode ter poder de quebrar o pipeline.
        """
        try:
            stream.write(text)  # type: ignore[attr-defined]
        except UnicodeEncodeError:
            enc = getattr(stream, "encoding", None) or "ascii"
            stream.write(text.encode(enc, "replace").decode(enc))  # type: ignore[attr-defined]

    def track(self, module: str, message: str, level: str = "INFO") -> None:
        now = time.perf_counter()
        with self._lock:
            total = now - self.start_time
            delta = now - self.last_time
            self.last_time = now
            c = self._C
            lvl = c["blue"] if level == "INFO" else c["yellow"] if level == "WARN" else c["red"]
            self._safe_write(
                sys.stdout,
                f"{c['gray']}[{self._fmt(total)}]{c['reset']} "
                f"{c['cyan']}(+{self._fmt(delta)}){c['reset']} "
                f"{lvl}[{module}]{c['reset']} {message}\n",
            )
            sys.stdout.flush()
            # A MESMA linha em arquivo, sem cor: o console some ao fechar o terminal,
            # e é justamente o comportamento de fundo que a análise depois precisa.
            sink = self._sink()
            if sink is not None:
                with contextlib.suppress(Exception):
                    sink.write(f"[{self._fmt(total)}] (+{self._fmt(delta)}) "
                               f"[{level}][{module}] {message}\n")
                    sink.flush()

    def warn(self, module: str, message: str) -> None:
        self.track(module, message, "WARN")

    def error(self, module: str, message: str, exc: Optional[BaseException] = None) -> None:
        self.track(module, message, "ERROR")
        if exc is not None:
            with self._lock:
                # Formata o traceback p/ string e escreve via _safe_write: a mensagem
                # da exceção pode carregar conteúdo do vault (char não-cp1252), então
                # o próprio dump de erro é um vetor de UnicodeEncodeError.
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                self._safe_write(sys.stderr, self._C["red"] + tb + self._C["reset"] + "\n")
                sys.stderr.flush()


telemetry = TelemetryStream()


class LatencyTracker:
    """Mede a latência por resposta, QUEBRADA POR ESTÁGIO (F4).

    Marca o 1º instante de cada tipo de msg que sai pelo `send` E conta os tokens para
    derivar o tok/s do DECODE (descontando o prefill, que já está no TTFT). O clock é
    injetável para teste determinístico. `stt_ms` é preenchido de FORA (a transcrição
    roda no ws.py, antes do pipeline) — por isso não é medido aqui.

    Morava no agent.py; veio para cá na modularização porque é instrumentação,
    não orquestração — a companhia natural é o save_latency do Database abaixo.
    """

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self.t0 = clock()
        self.ttft: float | None = None
        self.ttfa: float | None = None
        self.n_tokens = 0
        self._t_ultimo_token: float | None = None
        self.stt_ms: int | None = None   # transcrição (voz), setado pelo ws.py
        # WATERFALL (consultoria TTFT #1) — os estágios que vivem ANTES do 1º token,
        # preenchidos de fora como o stt_ms: sem eles, TTFT/TTFA dizem QUANTO demorou,
        # mas não ONDE — e toda discussão de otimização vira chute.
        self.vad_ms: int | None = None       # janela de silêncio do endpoint (voz)
        self.extrator_ms: int | None = None  # interpretação (gate/LLM ± fase-b overlap)
        self.busca_ms: int | None = None     # estágio Banco (embedding + HNSW + gate)
        # INSTRUMENTAÇÃO ADITIVA (2026-07): campos best-effort preenchidos DE FORA —
        # o produtor (llm.stream, dentro da thread do worker/executor) e a síntese TTS
        # (_falar). Default None = "não medido" (fake de teste, caminho de fundo), então
        # quem não preenche nunca quebra. São o que reparte o TTFT caixa-preta da rota
        # 'banco' (lock-wait vs reload frio vs prefill) e separa o decode do TTS/contenção.
        self.lock_wait_ms: float | None = None    # espera pelo _inference_lock (GPU ocupada)
        self.prefill_ms: float | None = None      # do lock ao 1º token (prefill do prompt)
        self.decode_tok_s_gpu: float | None = None  # tok/s PRODUTOR (dentro do worker; imune ao event loop/TTS)
        self.reload_frio: int | None = None       # 1 se este stream religou o modelo (unload do idle)
        self.vram_peak_mb: float | None = None    # pico de VRAM alocada no decode (torch)
        self.tts_synth_ms_total: float | None = None  # soma da síntese de todas as frases do turno
        self.tts_synth_ms_max: float | None = None    # maior síntese de frase (o pior caso do TTFA/gap)
        self.tts_frases: int | None = None            # nº de frases sintetizadas
        # obs-01 (painel 2026-07-24): o VEREDITO do retrieval por turno. A qualidade
        # da recuperação era INVISÍVEL em produção — o trace só tinha latência, e o
        # único diagnóstico (RAG_DEBUG) fica desligado. Preenchidos pelo pipeline;
        # None = o estágio Banco nem rodou (rota mestre/tools/time-sensitive).
        self.melhor_dist: float | None = None   # menor distância no estágio Banco
        self.relevante: int | None = None       # 1 = passou no gate (léxico OU confiante)
        self.sentinela: int | None = None       # 1 = Banco relevante que contribuiu NADA (proxy: decode morreu no sentinela)
        self.escalou_web: int | None = None     # 1 = cascata local falhou e escalou pra web
        # MODO TRACE: timeline de marcos (nome, ms desde t0). Só materializa quando mark
        # é chamado; fica vazia no caminho comum (custo zero se ninguém instrumenta).
        self.events: list = []

    def mark(self, nome: str) -> None:
        """Anexa um marco (nome, ms desde t0) à timeline do TRACE. USA O MESMO relógio
        do tracker (self._clock) — coerente com ttft/ttfa. Baratíssimo (um append) e
        best-effort: chamado só quando há um tracker real, então não precisa de guarda."""
        self.events.append((nome, round((self._clock() - self.t0) * 1000, 1)))

    def registrar_synth(self, ms: float) -> None:
        """Acumula o tempo de síntese TTS de UMA frase: total, máximo e contagem. A
        síntese XTTS inline era 100% cega — isto a mede sem tocar o fluxo (o _falar
        cronometra cada synth_base64). None inicial vira 0 no 1º registro."""
        self.tts_frases = (self.tts_frases or 0) + 1
        self.tts_synth_ms_total = (self.tts_synth_ms_total or 0.0) + ms
        if self.tts_synth_ms_max is None or ms > self.tts_synth_ms_max:
            self.tts_synth_ms_max = ms

    def note(self, msg: dict) -> None:
        tipo = msg.get("tipo")
        if tipo == "token":
            agora = self._clock()
            if self.ttft is None:
                self.ttft = agora - self.t0
            self.n_tokens += 1
            self._t_ultimo_token = agora
        elif tipo == "audio" and self.ttfa is None:
            self.ttfa = self._clock() - self.t0

    def total(self) -> float:
        return self._clock() - self.t0

    def decode_tok_s(self) -> float | None:
        """tok/s do DECODE: (n-1) / (último token − 1º token). Desconta o prefill —
        senão um prompt RAG longo faria o modelo 'parecer lento'. None com < 2 tokens
        (sem janela de decode medível). Espelha eval/ab_modelos._gerar."""
        if self.n_tokens < 2 or self._t_ultimo_token is None or self.ttft is None:
            return None
        dur = self._t_ultimo_token - (self.t0 + self.ttft)
        return round((self.n_tokens - 1) / dur, 1) if dur > 0 else None

    @staticmethod
    def _ms(seg: float | None) -> int | None:
        return round(seg * 1000) if seg is not None else None


class Database:
    """SQLite fino. Uma conexão por operação (simples e seguro entre threads)."""

    def __init__(self, path: str) -> None:
        self.path = path

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Uma conexão POR OPERAÇÃO, agora com vida determinística (painel 2026-07).

        O `with sqlite3.connect(...)` clássico faz commit/rollback mas NÃO fecha —
        o close ficava por conta do refcount do CPython. O `with conn:` interno
        preserva EXATAMENTE a semântica transacional que os 44 call sites assumem
        (`with self._conn() as conn:` continua idêntico para quem chama); o finally
        fecha de fato. O `timeout=10` do connect JÁ é o busy_timeout do SQLite.
        `synchronous=NORMAL` é por-conexão (não persiste no arquivo), por isso vive
        aqui: é o par canônico do WAL — fsync no checkpoint, seguro em disco local.
        """
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        # WAL uma vez (fica gravado NO ARQUIVO): leitor nunca mais bloqueia em
        # escrita — save_chat/save_latency do turno vs tick do scheduler vs ETL.
        # Fora do bloco transacional: mudar journal_mode dentro de transação falha.
        # Backup do .db passa a exigir os sidecars -wal/-shm juntos (ou um
        # `PRAGMA wal_checkpoint(TRUNCATE)` antes de copiar).
        raw = sqlite3.connect(self.path, timeout=10)
        try:
            raw.execute("PRAGMA journal_mode=WAL")
        finally:
            raw.close()
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
            # F4 — timing por estágio: colunas extras (migração idempotente p/ bancos
            # antigos, como o conversa_id acima). stt_ms=transcrição, decode_tok_s=tok/s
            # do decode, n_tokens=tokens gerados na resposta.
            lat_cols = [r[1] for r in c.execute("PRAGMA table_info(metricas_latencia)").fetchall()]
            # vad/extrator/busca: o waterfall da consultoria TTFT (#1) — mesma migração
            # idempotente dos demais (banco antigo ganha as colunas, linhas velhas = NULL).
            # As colunas lock_wait/prefill/decode_tok_s_gpu/reload_frio/vram_peak e as
            # tts_synth_* são a instrumentação aditiva de 2026-07 — mesma migração
            # idempotente (banco antigo ganha as colunas, linhas velhas = NULL).
            for _col, _tipo in (("stt_ms", "INTEGER"), ("decode_tok_s", "REAL"),
                                ("n_tokens", "INTEGER"), ("vad_ms", "INTEGER"),
                                ("extrator_ms", "INTEGER"), ("busca_ms", "INTEGER"),
                                ("lock_wait_ms", "REAL"), ("prefill_ms", "REAL"),
                                ("decode_tok_s_gpu", "REAL"), ("reload_frio", "INTEGER"),
                                ("vram_peak_mb", "REAL"), ("tts_synth_ms_total", "REAL"),
                                ("tts_synth_ms_max", "REAL"), ("tts_frases", "INTEGER"),
                                # obs-01: o veredito do retrieval por turno (painel 2026-07-24)
                                ("melhor_dist", "REAL"), ("relevante", "INTEGER"),
                                ("sentinela", "INTEGER"), ("escalou_web", "INTEGER")):
                if _col not in lat_cols:
                    c.execute(f"ALTER TABLE metricas_latencia ADD COLUMN {_col} {_tipo}")
            # Métricas do ciclo do conhecimento (painel 2026-07): retrato DIÁRIO da
            # base auto-colhida — total de chunks, composição por origem e quantos
            # ainda carregam #conhecimento_novo. Gravado no idle (1x/dia).
            c.execute(
                """CREATE TABLE IF NOT EXISTS base_snapshot
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT,
                    total INTEGER, por_origem TEXT, novos INTEGER)"""
            )
            # rot-03 (painel 2026-07-24): decisões CRUAS do roteador LLM. Antes só a
            # rota agregada sobrevivia — cada erro real de roteamento era evidência
            # perdida, e o eval seguia com 9 casos. Cada linha é um caso candidato;
            # a taxa de parse_ok em produção é o critério do constrained-decoding.
            # Colheita acadêmica (Fase 4): URLs de PDF já vistas — dedup DURÁVEL
            # (o cache de URL do WebSearcher é só de RAM, e o crawler roda todo dia).
            c.execute(
                """CREATE TABLE IF NOT EXISTS academico_pdfs
                   (url TEXT PRIMARY KEY, data_hora TEXT)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS router_log
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT, origem TEXT,
                    texto TEXT, bruto TEXT, tool TEXT, parse_ok INTEGER)"""
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
            # TEMAS QUENTES (#4): o ESPELHO da lacuna. Lacuna = o que a RAM E o banco NÃO
            # responderam (buraco). Tema quente = o que o usuário mais REUSA do banco (o
            # estágio Banco respondeu de fato) — interesse recorrente. A pesquisa proativa
            # do idle re-pesquisa estes na web para trazer NOVIDADE (dedup por átomo filtra
            # o já-sabido), então a base amadurece nos temas favoritos, não só nos buracos.
            # Mesmo shape da lacuna; `chave` normalizada agrupa; `n` conta os reusos.
            c.execute(
                """CREATE TABLE IF NOT EXISTS temas_quentes
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
            # SRS (#43): cards de repetição espaçada. `frente`/`verso` da carta (a última
            # troca marcada), `proxima_revisao` (ISO) e `estagio` (caixa de Leitner). A
            # revisão é sob demanda, mas o CRONOGRAMA é persistente (sobrevive a restart).
            c.execute(
                """CREATE TABLE IF NOT EXISTS srs_cards
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, frente TEXT, verso TEXT,
                    proxima_revisao TEXT, estagio INTEGER DEFAULT 0, criado_em TEXT)"""
            )
            # GATILHOS CONDICIONAIS INTERNOS (#11): "quando <evento interno>, faça <ação>".
            #   evento : nome do evento do app (v1: 'lista_add')
            #   filtro : substring que o contexto do evento precisa conter ('' = qualquer)
            #   acao   : JSON de uma lista de Decisões [{"tool":..,"args":..}] a executar
            #   descricao: frase legível (para "quais gatilhos tenho")
            c.execute(
                """CREATE TABLE IF NOT EXISTS gatilhos
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, evento TEXT, filtro TEXT,
                    acao TEXT, descricao TEXT, criado_em TEXT)"""
            )
            # HÁBITOS (#37): uma linha por (hábito, DIA cumprido). UNIQUE evita contar duas
            # vezes o mesmo dia; a sequência (streak) é derivada das datas. Fluxo independente.
            c.execute(
                """CREATE TABLE IF NOT EXISTS habitos
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, data TEXT,
                    criado_em TEXT, UNIQUE(nome, data))"""
            )
            # ROTINAS COMPOSTAS (#10): macro NOMEADA -> comando composto salvo. "rotina manhã"
            # expande para o `comando` e o fluxo normal (parse_composto) executa os passos.
            c.execute(
                """CREATE TABLE IF NOT EXISTS rotinas
                   (nome TEXT PRIMARY KEY, comando TEXT, criado_em TEXT)"""
            )
            # DETECTOR DE CONTRADIÇÃO (#24): pares de átomos que o idle marcou como
            # afirmando fatos incompatíveis. UNIQUE(fonte_a, fonte_b) evita re-gravar o
            # mesmo par a cada varredura. `resolvido` fica para futura curadoria.
            c.execute(
                """CREATE TABLE IF NOT EXISTS contradicoes
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, fonte_a TEXT, fonte_b TEXT,
                    resumo TEXT, data TEXT, resolvido INTEGER DEFAULT 0,
                    UNIQUE(fonte_a, fonte_b))"""
            )
            # DIAPASÃO (#36): perfil de COMO o usuário prefere ser respondido. Uma linha
            # (chave fixa 'conversa'); o idle refina, o hot-path lê. Diretriz de estilo.
            c.execute(
                """CREATE TABLE IF NOT EXISTS perfil_conversa
                   (chave TEXT PRIMARY KEY, valor TEXT, atualizado_em TEXT)"""
            )
            conn.commit()

    def salvar_perfil(self, valor: str, chave: str = "conversa") -> None:
        """Grava/atualiza o perfil de conversa (#36)."""
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO perfil_conversa (chave, valor, atualizado_em) VALUES (?, ?, ?)
                       ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor,
                       atualizado_em = excluded.atualizado_em""",
                    (chave, valor, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao salvar perfil de conversa", exc)

    def ler_perfil(self, chave: str = "conversa") -> Optional[str]:
        """O perfil de conversa guardado, ou None (#36)."""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT valor FROM perfil_conversa WHERE chave = ?", (chave,)
                ).fetchone()
            return row[0] if row else None
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler perfil de conversa", exc)
            return None

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

    # -- métricas do ciclo do conhecimento (painel 2026-07) ---------------------
    def salvar_snapshot_base(self, total: int, por_origem: dict, novos: int) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO base_snapshot (data_hora, total, por_origem, novos) "
                    "VALUES (?, ?, ?, ?)",
                    (datetime.now().isoformat(), total,
                     json.dumps(por_origem, ensure_ascii=False), novos),
                )
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao gravar snapshot da base", exc)

    def snapshot_base_hoje(self) -> bool:
        """True se já há snapshot de HOJE (throttle de 1x/dia — o scan da coleção
        inteira é barato no idle, mas não precisa rodar a cada ciclo)."""
        try:
            with self._conn() as conn:
                hoje = datetime.now().date().isoformat()
                row = conn.execute(
                    "SELECT 1 FROM base_snapshot WHERE data_hora >= ? LIMIT 1", (hoje,)
                ).fetchone()
                return row is not None
        except Exception:
            return False

    def metricas_base(self) -> dict:
        """Bloco 'base' do /api/metrics: último snapshot + eventos do ciclo
        (DEDUP/PROMOCAO/PURGA_ORFAOS) dos últimos 7 dias. Exposto ATRÁS do gate de
        acesso (#7) — a composição da base revela o que o dono estuda e quando."""
        out: dict = {"snapshot": None, "ciclo_7d": {}}
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT data_hora, total, por_origem, novos FROM base_snapshot "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    try:
                        origem = json.loads(row[2] or "{}")
                    except ValueError:
                        origem = {}
                    out["snapshot"] = {
                        "data": row[0], "total": row[1], "por_origem": origem, "novos": row[3],
                    }
                desde = (datetime.now() - timedelta(days=7)).isoformat()
                out["ciclo_7d"] = dict(conn.execute(
                    "SELECT tipo_acao, COUNT(*) FROM log_etl "
                    "WHERE data_hora >= ? AND tipo_acao IN ('DEDUP','PROMOCAO','PURGA_ORFAOS') "
                    "GROUP BY tipo_acao",
                    (desde,),
                ).fetchall())
        except Exception as exc:
            telemetry.error("SQLITE", "Erro nas métricas da base", exc)
        return out

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

    # -- SRS (#43): repetição espaçada -----------------------------------------
    def srs_criar_card(self, frente: str, verso: str, proxima_revisao: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO srs_cards (frente, verso, proxima_revisao, estagio, criado_em) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (frente, verso, proxima_revisao, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao criar card SRS", exc)

    def srs_vencidos(self, agora_iso: str, limite: int) -> list:
        """Cards com revisão vencida (proxima_revisao <= agora), do mais atrasado ao menos."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, frente, verso, proxima_revisao, estagio FROM srs_cards "
                    "WHERE proxima_revisao <= ? ORDER BY proxima_revisao LIMIT ?",
                    (agora_iso, limite),
                ).fetchall()
            return [
                {"id": r[0], "frente": r[1], "verso": r[2], "proxima_revisao": r[3], "estagio": r[4]}
                for r in rows
            ]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao buscar cards SRS vencidos", exc)
            return []

    def srs_contar_vencidos(self, agora_iso: str) -> int:
        try:
            with self._conn() as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM srs_cards WHERE proxima_revisao <= ?", (agora_iso,)
                ).fetchone()[0]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao contar cards SRS", exc)
            return 0

    def srs_reagendar(self, card_id: int, estagio: int, proxima_revisao: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE srs_cards SET estagio = ?, proxima_revisao = ? WHERE id = ?",
                    (estagio, proxima_revisao, card_id),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao reagendar card SRS", exc)

    # -- Gatilhos condicionais internos (#11) ----------------------------------
    def gatilho_criar(self, evento: str, filtro: str, acao_json: str, descricao: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO gatilhos (evento, filtro, acao, descricao, criado_em) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (evento, filtro, acao_json, descricao, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao criar gatilho", exc)

    def gatilhos_por_evento(self, evento: str) -> list:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, filtro, acao, descricao FROM gatilhos WHERE evento = ?",
                    (evento,),
                ).fetchall()
            return [{"id": r[0], "filtro": r[1], "acao": r[2], "descricao": r[3]} for r in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler gatilhos", exc)
            return []

    def gatilhos_listar(self) -> list:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, descricao FROM gatilhos ORDER BY id"
                ).fetchall()
            return [{"id": r[0], "descricao": r[1]} for r in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao listar gatilhos", exc)
            return []

    def gatilho_remover(self, gatilho_id: int) -> bool:
        try:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM gatilhos WHERE id = ?", (gatilho_id,))
                conn.commit()
                return cur.rowcount > 0
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao remover gatilho", exc)
            return False

    # -- Hábitos (#37) ---------------------------------------------------------
    def habito_marcar(self, nome: str, data_iso: str) -> None:
        """Registra o hábito num DIA. UNIQUE(nome, data) -> marcar duas vezes o mesmo dia
        é inócuo (INSERT OR IGNORE)."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO habitos (nome, data, criado_em) VALUES (?, ?, ?)",
                    (nome, data_iso, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao marcar hábito", exc)

    def habito_datas(self, nome: str) -> list:
        """Todas as datas (ISO) em que o hábito foi cumprido."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT data FROM habitos WHERE nome = ? ORDER BY data", (nome,)
                ).fetchall()
            return [r[0] for r in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler hábito", exc)
            return []

    def habitos_nomes(self) -> list:
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT DISTINCT nome FROM habitos ORDER BY nome").fetchall()
            return [r[0] for r in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao listar hábitos", exc)
            return []

    # -- Rotinas compostas (#10) -----------------------------------------------
    def rotina_salvar(self, nome: str, comando: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO rotinas (nome, comando, criado_em) VALUES (?, ?, ?)",
                    (nome, comando, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao salvar rotina", exc)

    def rotina_get(self, nome: str) -> Optional[str]:
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT comando FROM rotinas WHERE nome = ?", (nome,)).fetchone()
            return row[0] if row else None
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler rotina", exc)
            return None

    def rotinas_listar(self) -> list:
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT nome, comando FROM rotinas ORDER BY nome").fetchall()
            return [{"nome": r[0], "comando": r[1]} for r in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao listar rotinas", exc)
            return []

    def rotina_remover(self, nome: str) -> bool:
        try:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM rotinas WHERE nome = ?", (nome,))
                conn.commit()
                return cur.rowcount > 0
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao remover rotina", exc)
            return False

    def pdf_academico_visto(self, url: str) -> bool:
        """True se este PDF já foi colhido (ou tentado) — Fase 4. Em erro devolve
        True: na dúvida NÃO baixar de novo é o lado seguro (evita loop de download)."""
        try:
            with self._conn() as conn:
                return conn.execute(
                    "SELECT 1 FROM academico_pdfs WHERE url = ?", (url,)
                ).fetchone() is not None
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao checar PDF acadêmico", exc)
            return True

    def marcar_pdf_academico(self, url: str) -> None:
        """Carimba a URL como vista (idempotente)."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO academico_pdfs (url, data_hora) VALUES (?, ?)",
                    (url, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao marcar PDF acadêmico", exc)

    def save_router_log(self, origem: str, texto: str, bruto: str,
                        tool: Optional[str], parse_ok: bool) -> None:
        """rot-03: log best-effort de cada decisão do roteador LLM (o flywheel que
        alimenta o eval de roteamento com casos REAIS). Truncado: o valor está no
        par pergunta→decisão, não em páginas de texto."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO router_log (data_hora, origem, texto, bruto, tool, parse_ok) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(), origem, texto[:500], (bruto or "")[:800],
                     tool, int(parse_ok)),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao gravar router_log", exc)

    def save_latency(
        self,
        rota: str,
        ttft_ms: Optional[int],
        ttfa_ms: Optional[int],
        total_ms: Optional[int],
        stt_ms: Optional[int] = None,
        decode_tok_s: Optional[float] = None,
        n_tokens: Optional[int] = None,
        vad_ms: Optional[int] = None,
        extrator_ms: Optional[int] = None,
        busca_ms: Optional[int] = None,
        lock_wait_ms: Optional[float] = None,
        prefill_ms: Optional[float] = None,
        decode_tok_s_gpu: Optional[float] = None,
        reload_frio: Optional[int] = None,
        vram_peak_mb: Optional[float] = None,
        tts_synth_ms_total: Optional[float] = None,
        tts_synth_ms_max: Optional[float] = None,
        tts_frases: Optional[int] = None,
        melhor_dist: Optional[float] = None,
        relevante: Optional[int] = None,
        sentinela: Optional[int] = None,
        escalou_web: Optional[int] = None,
    ) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO metricas_latencia "
                    "(data_hora, rota, ttft_ms, ttfa_ms, total_ms, stt_ms, decode_tok_s, "
                    "n_tokens, vad_ms, extrator_ms, busca_ms, lock_wait_ms, prefill_ms, "
                    "decode_tok_s_gpu, reload_frio, vram_peak_mb, tts_synth_ms_total, "
                    "tts_synth_ms_max, tts_frases, melhor_dist, relevante, sentinela, "
                    "escalou_web) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?)",
                    (datetime.now().isoformat(), rota, ttft_ms, ttfa_ms, total_ms,
                     stt_ms, decode_tok_s, n_tokens, vad_ms, extrator_ms, busca_ms,
                     lock_wait_ms, prefill_ms, decode_tok_s_gpu, reload_frio, vram_peak_mb,
                     tts_synth_ms_total, tts_synth_ms_max, tts_frases,
                     melhor_dist, relevante, sentinela, escalou_web),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao gravar latência", exc)

    # FILTRO DE OUTLIER (instrumentação 2026-07): uma resposta com TTFT ou tok/s acima
    # dos tetos de sanidade é artefato de relógio-de-parede (idle/reload frio) e envenena
    # p95/AVG — o id272 (ttft=346800ms, tok_s=2142) sozinho distorcia tudo. Aqui a linha
    # é EXCLUÍDA da agregação (mas permanece no banco). NULL nunca é filtrado: turno
    # digitado sem tok/s, linha antiga sem a coluna — tratados como "dentro".
    @staticmethod
    def _clausula_saneamento() -> str:
        return ("(ttft_ms IS NULL OR ttft_ms <= ?) "
                "AND (decode_tok_s IS NULL OR decode_tok_s <= ?)")

    def _contar_outliers(self, conn: sqlite3.Connection) -> int:
        """Quantas linhas o saneamento descarta (para logar o que ficou de fora)."""
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM metricas_latencia "
                "WHERE ttft_ms > ? OR decode_tok_s > ?",
                (settings.latencia_ttft_teto_ms, settings.latencia_tok_s_teto),
            ).fetchone()[0]
        except Exception:
            return 0

    @staticmethod
    def _percentil(ordenados: list, frac: float):
        """Percentil por posto-mais-próximo sobre uma lista JÁ ordenada. Sem dependência
        nova de propósito (numpy até existe no projeto, mas isto roda no caminho do
        /api/metrics — 8 sorts de <=200 itens não justificam importar nada)."""
        if not ordenados:
            return None
        return ordenados[min(len(ordenados) - 1, int(round(frac * (len(ordenados) - 1))))]

    def latencia_percentis(self, ultimas: int = 200) -> dict:
        """WATERFALL (consultoria TTFT #1): p50/p95 POR ESTÁGIO das últimas N respostas.

        Média esconde cauda — e é a cauda que o usuário sente. NULLs ficam fora POR
        estágio (turno digitado não tem vad/stt; rota web não tem busca do Banco;
        linhas antigas não têm as colunas novas), por isso cada estágio reporta o
        próprio `n`. É o que arbitra as apostas da consultoria: se vad+stt dominam,
        otimizar decode não muda o que o usuário ouve."""
        campos = ("vad_ms", "stt_ms", "extrator_ms", "busca_ms",
                  "ttft_ms", "ttfa_ms", "total_ms", "decode_tok_s")
        out: dict = {"amostras": 0, "estagios": {}}
        try:
            with self._conn() as conn:
                filtrados = self._contar_outliers(conn)
                if filtrados:
                    telemetry.track(
                        "LATENCIA",
                        f"Percentis: {filtrados} linha(s) fora dos tetos de sanidade "
                        f"(ttft>{settings.latencia_ttft_teto_ms}ms ou "
                        f"tok/s>{settings.latencia_tok_s_teto}) — excluída(s) da agregação.",
                    )
                rows = conn.execute(
                    f"SELECT {', '.join(campos)} FROM metricas_latencia "
                    f"WHERE {self._clausula_saneamento()} "
                    "ORDER BY id DESC LIMIT ?",
                    (settings.latencia_ttft_teto_ms, settings.latencia_tok_s_teto, ultimas),
                ).fetchall()
            out["amostras"] = len(rows)
            for i, campo in enumerate(campos):
                valores = sorted(r[i] for r in rows if r[i] is not None)
                if valores:
                    out["estagios"][campo] = {
                        "n": len(valores),
                        "p50": self._percentil(valores, 0.50),
                        "p95": self._percentil(valores, 0.95),
                    }
        except Exception as exc:
            telemetry.error("SQLITE", "Erro nos percentis de latência", exc)
        return out

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

    # ---- Temas quentes (#4): o que o usuário mais REUSA do banco -> re-pesquisa ----
    def save_tema_quente(self, chave: str, termos: str) -> None:
        """Registra (ou incrementa) um tema que o usuário REUSOU do vault (o estágio Banco
        respondeu de fato). UPSERT: o mesmo interesse recorrente sobe `n` em vez de virar N
        linhas — a re-pesquisa proativa prioriza o favorito mais consultado."""
        if not chave:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO temas_quentes (chave, termos, n, visto_em)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(chave) DO UPDATE SET n = n + 1, visto_em = excluded.visto_em""",
                    (chave, termos, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao gravar tema quente", exc)

    def get_temas_quentes(
        self, limit: int = 10, min_reuso: int = 3, nao_pesquisadas_ha_dias: int = 14
    ) -> list[dict]:
        """Os temas MAIS REUSADOS ainda não re-pesquisados (ou fora do cooldown), do mais
        reusado ao menos. `min_reuso` separa o interesse recorrente da curiosidade de uma
        vez só; `nao_pesquisadas_ha_dias` é o cooldown (não refresca o mesmo favorito à toa)."""
        try:
            corte = (datetime.now() - timedelta(days=nao_pesquisadas_ha_dias)).isoformat()
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT termos, n FROM temas_quentes
                       WHERE n >= ? AND (pesquisado_em IS NULL OR pesquisado_em < ?)
                       ORDER BY n DESC, visto_em DESC LIMIT ?""",
                    (min_reuso, corte, limit),
                ).fetchall()
            return [{"termos": t, "n": n} for t, n in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler temas quentes", exc)
            return []

    def marcar_tema_pesquisado(self, chave: str) -> None:
        """Carimba que o tema quente foi re-pesquisado — entra no cooldown de N dias."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE temas_quentes SET pesquisado_em = ? WHERE chave = ?",
                    (datetime.now().isoformat(), chave),
                )
                conn.commit()
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao marcar tema quente", exc)

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
        payload: Optional[str] = None,
    ) -> None:
        """Reprograma (próximo disparo da recorrência), muda o status, e/ou atualiza o
        payload (ex.: fase do pomodoro #19) de um agendamento."""
        campos, valores = [], []
        if status is not None:
            campos.append("status = ?")
            valores.append(status)
        if proximo_disparo is not None:
            campos.append("proximo_disparo = ?")
            valores.append(proximo_disparo)
        if payload is not None:
            campos.append("payload = ?")
            valores.append(payload)
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

    def registrar_contradicao(self, fonte_a: str, fonte_b: str, resumo: str) -> bool:
        """Grava um par contraditório (#24). O par é ordenado para (A,B) e (B,A) não
        virarem dois registros; UNIQUE ignora repetição. Devolve True se gravou novo."""
        a, b = sorted([str(fonte_a or ""), str(fonte_b or "")])
        if not a or not b or a == b:
            return False
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO contradicoes (fonte_a, fonte_b, resumo, data)
                       VALUES (?, ?, ?, ?)""",
                    (a, b, resumo, datetime.now().isoformat()),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao registrar contradição", exc)
            return False

    def contradicoes_abertas(self, limite: int = 5) -> list[dict]:
        """Contradições ainda não resolvidas, mais recentes primeiro (#24)."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT fonte_a, fonte_b, resumo FROM contradicoes "
                    "WHERE resolvido = 0 ORDER BY id DESC LIMIT ?",
                    (limite,),
                ).fetchall()
            return [{"a": a, "b": b, "resumo": r} for a, b, r in rows]
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao ler contradições", exc)
            return []

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
                # Mesmo saneamento dos percentis: a média não pode ser puxada pelo
                # artefato wall-clock (id272). Linhas com NULL passam (não filtradas).
                lat = conn.execute(
                    "SELECT COUNT(*), AVG(ttft_ms), AVG(ttfa_ms), AVG(total_ms), "
                    "AVG(stt_ms), AVG(decode_tok_s) FROM metricas_latencia "
                    f"WHERE {self._clausula_saneamento()}",
                    (settings.latencia_ttft_teto_ms, settings.latencia_tok_s_teto),
                ).fetchone()
                latencia = {
                    "amostras": lat[0] or 0,
                    "ttft_ms_medio": round(lat[1]) if lat[1] is not None else None,
                    "ttfa_ms_medio": round(lat[2]) if lat[2] is not None else None,
                    "total_ms_medio": round(lat[3]) if lat[3] is not None else None,
                    "stt_ms_medio": round(lat[4]) if lat[4] is not None else None,
                    "decode_tok_s_medio": round(lat[5], 1) if lat[5] is not None else None,
                }
            return {
                "total_conversas": total_chat,
                "total_etl": total_etl,
                "etl_por_status": por_status,
                "ultimos_etl": ultimos_etl,
                "latencia": latencia,
                # Waterfall (consultoria #1): p50/p95 por estágio — a média acima fica
                # por compatibilidade, mas é o percentil que orienta otimização.
                "waterfall": self.latencia_percentis(),
            }
        except Exception as exc:
            telemetry.error("SQLITE", "Erro ao calcular métricas", exc)
            return {}


db = Database(settings.db_telemetria)
