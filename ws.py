"""
Sessão WebSocket ao vivo (texto + voz com VAD/Barge-in).

Encapsula a máquina de estados que estava solta no endpoint do monólito:
- VAD por RMS no servidor (início/fim de fala).
- Barge-in (interrupção) cancelando o pipeline em andamento.
- fim de sessão (end_session OU disconnect) disparando o ETL idle uma única vez.
- roteamento de mensagens de texto.

Robustez: a leitura usa timeout, então o silêncio é detectado mesmo que o browser
pare de enviar pacotes; e os erros são logados via telemetry (não mais `except: pass`).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import List, Optional

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

import mestre
import tools
from agent import append_chat_dump
from otimizador import e_backchannel
from config import settings
from state import AppContext, SessionMemory
from telemetry import db, telemetry

_RECV_TIMEOUT = 0.5  # s — granularidade da checagem de silêncio


def janela_endpoint(dur_fala: float) -> float:
    """Janela de silêncio que encerra a fala (endpointing adaptativo, consultoria #3).

    Fala CURTA (comando, pergunta seca) encerra com a janela curta — o 1,2s fixo era
    pago por TODO turno de voz, e é maior que o próprio TTFT do RAG. Fala longa mantém
    a margem cheia: pausa de respiração no meio de um raciocínio não pode decapitar a
    frase. Pura (settings entram por leitura) para testar sem WebSocket."""
    if not settings.vad_adaptativo:
        return settings.vad_silence_seconds
    if dur_fala <= settings.vad_fala_curta_seconds:
        return settings.vad_silence_curta_seconds
    return settings.vad_silence_seconds


class LiveSession:
    def __init__(self, ctx: AppContext, websocket: WebSocket) -> None:
        self.ctx = ctx
        self.ws = websocket
        self.audio_buffer: List["np.ndarray"] = []
        self.is_recording = False
        self.last_audio_time = time.time()
        self.last_activity = time.time()   # qualquer interação: rearma o timer de idle
        # Endpointing adaptativo (#3): início da fala corrente (mede a duração p/ a
        # janela) e o instante/janela do ÚLTIMO endpoint — retomada logo após um corte
        # CURTO é o sinal de corte-precoce que calibra os defaults (logado abaixo).
        self._fala_inicio = 0.0
        self._fim_fala_ts: Optional[float] = None
        self._janela_usada: Optional[float] = None
        # F3 — wake-word "mestre" (tipo Alexa): com `mestre_wake` ligado, a sessão começa
        # DORMENTE e só processa fala normal após a palavra-mestre ACORDAR; volta a dormir
        # após `mestre_sleep_seconds` de silêncio. Desligado = sempre ativa (hoje).
        self.dormente = settings.mestre_wake
        self.pipeline_task: Optional[asyncio.Task] = None
        self._finalizada = False  # guarda: idle roda UMA vez (end_session OU disconnect)
        # A memória é DESTA conexão. Antes era um SessionMemory único no AppContext,
        # compartilhado por todas: o cliente reconecta sozinho (backoff) e reenvia
        # `set_conversa` no onopen, então o último a conectar sobrescrevia o
        # `conversa_id` de todos — e os turnos iam pro SQLite na conversa errada.
        self.memory = SessionMemory(ctx.settings)

    # -- envio seguro (falha esperada durante barge-in/disconnect) --------------
    async def safe_send(self, data: dict) -> bool:
        try:
            await self.ws.send_json(data)
            return True
        except Exception:
            return False

    def _cancel_pipeline(self) -> None:
        if self.pipeline_task and not self.pipeline_task.done():
            self.pipeline_task.cancel()

    def _start_pipeline(
        self, texto: str, stt_ms: Optional[int] = None, vad_ms: Optional[int] = None
    ) -> None:
        self._cancel_pipeline()
        self.pipeline_task = asyncio.create_task(
            self.ctx.agent.pipeline_resposta(
                texto, self.safe_send, self.memory, stt_ms=stt_ms, vad_ms=vad_ms
            )
        )

    async def _dump_pergunta(self, texto: str) -> None:
        """Grava a pergunta no dump — MENOS quando é efêmera OU é comando de agente.

        O dump é a matéria-prima que o idle atomiza em Zettelkasten permanente. Um
        turno sobre cotação/clima ali vira nota eterna sobre um dado que expira em
        horas (medido: 3 dos átomos-lixo entraram por aqui). O par IA é guardado pelo
        mesmo critério, no pipeline. O turno em si não se perde: vai pro SQLite.

        COMANDO-MESTRE: o lado IA já é gateado (não vira conhecimento), mas o lado do
        USUÁRIO vazava — "mestre, adiciona leite" e "mestre, modo sigiloso" viravam os
        átomos "## Leite" e "## Modo Sigiloso" no idle (medido no teste real). Um comando
        de agente NUNCA é conhecimento: se a msg começa pela palavra-mestre, fora do dump.
        """
        if tools.e_efemero(texto) or self.memory.confidencial:
            return
        # Backchannel ("ok"/"aham"/"tchau") não é conhecimento: fora do dump que o idle
        # atomiza — senão vira átomo-lixo. Espelha o gate do Agent._pipeline.
        if settings.ignorar_backchannel and e_backchannel(texto):
            return
        if settings.palavra_mestre_habilitada and mestre.separar(texto, settings.palavra_mestre) is not None:
            return
        await append_chat_dump("User", texto)

    # -- loop principal ---------------------------------------------------------
    async def run(self) -> None:
        await self.ws.accept()
        self.ctx.sessoes.add(self)
        # Entrega já os avisos que dispararam enquanto ninguém estava conectado
        # (lembrete de ontem, watcher satisfeito de madrugada). Em background: não
        # atrasa o handshake, e o loop do scheduler também os reentregaria no tick.
        if self.ctx.scheduler is not None:
            self.ctx.track_task(self.ctx.scheduler.entregar_pendentes())
        # Abertura do live é sinal de uso: religa o modelo se o idle o descarregou,
        # para a 1ª pergunta não pagar o reload em cima da latência normal.
        if not self.ctx.llama.ready:
            self.ctx.track_task(self.ctx.llama.ensure_loaded())
            await self.safe_send(
                {"tipo": "status", "texto": "Modelo religando..."}
            )
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(self.ws.receive(), timeout=_RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._check_silence()
                    self._check_inatividade()
                    self._check_sono()
                    continue

                if msg.get("type") == "websocket.disconnect":
                    break

                if "bytes" in msg and msg["bytes"] is not None:
                    self._on_audio(msg["bytes"])
                elif "text" in msg and msg["text"] is not None:
                    await self._on_text(msg["text"])

                await self._check_silence()
                self._check_inatividade()
                self._check_sono()
        except WebSocketDisconnect:
            telemetry.track("WS", "Cliente desconectou.")
        except Exception as exc:
            telemetry.error("WS", "Erro no loop do WebSocket", exc)
        finally:
            self.ctx.sessoes.discard(self)
            self._cancel_pipeline()
            # Rede de segurança: se o cliente caiu SEM mandar end_session, a conversa
            # ainda é atomizada e a fila ETL sintetizada (senão o histórico se perdia).
            self._finalizar_sessao()

    def _finalizar_sessao(self) -> None:
        """Dispara o ETL idle (end_session explícito ou disconnect). A guarda
        `_finalizada` evita rodar duas vezes seguidas (ex.: end_session + disconnect),
        mas uma NOVA fala/mensagem reabre a sessão (`_marcar_ativa`), então o usuário
        pode encerrar → idle → reabrir → encerrar de novo na mesma conexão.
        Retido no ctx (não na sessão): o idle sobrevive à desconexão do WS."""
        if self._finalizada:
            return
        self._finalizada = True
        itens = self.memory.drenar_etl()   # sempre esvazia a fila (limpa a RAM da sessão)
        # PRIVACIDADE (#34): sessão confidencial NÃO atomiza nada ao encerrar. Os guards
        # por-turno já mantêm o turno sigiloso fora do dump/fila/SQLite, mas o disconnect
        # era o furo — run_idle atomizaria o dump acumulado + resultados web em átomos
        # PERMANENTES no vault (medido: ~40 átomos vazaram no teste real). Dreno a fila e
        # DESCARTO; conhecimento legítimo anterior (se houver) é re-atomizado no próximo
        # idle não-confidencial (deferido, nunca perdido). Cobre disconnect, end_session e
        # inatividade — todos passam por aqui. Tradeoff: o modelo não descarrega neste idle.
        if self.memory.confidencial:
            telemetry.track("SERVER", "Sessão confidencial — idle de conhecimento pulado (nada atomizado).")
            return
        telemetry.track("SERVER", "Sessão encerrada. Iniciando processamento IDLE...")
        self.ctx.track_task(self.ctx.etl.run_idle(itens))

    def _marcar_ativa(self) -> None:
        """Nova mensagem/fala do usuário = conversa ABERTA: sai do idle e rearma o
        gatilho, para que um próximo end_session (ou disconnect) volte a consolidar o
        conhecimento acumulado a partir daqui. Também rearma o timer de inatividade."""
        self._finalizada = False
        self.last_activity = time.time()

    def _check_inatividade(self) -> None:
        """Chat ABERTO mas parado há `idle_inatividade_seconds` -> entra em idle
        (consolida conhecimento + libera a GPU). Diferente do disconnect: a conexão
        segue viva, e uma nova mensagem rearma via `_marcar_ativa`.

        Não dispara se um pipeline está em voo (o usuário espera uma resposta) nem se o
        idle já rodou (`_finalizada`). O `run_idle` do ETL cede a GPU à interação e, no
        fim, descarrega o modelo — que a próxima mensagem religa sob demanda."""
        if self._finalizada:
            return
        if self.pipeline_task and not self.pipeline_task.done():
            return
        if time.time() - self.last_activity < settings.idle_inatividade_seconds:
            return
        telemetry.track("SERVER", "Inatividade detectada — entrando em idle.")
        self._finalizar_sessao()

    async def _deve_processar(self, texto: str) -> bool:
        """F3 (wake-word): decide se esta fala/texto deve ser PROCESSADA, atualizando o
        estado dormente/ativo. Sem `mestre_wake`, SEMPRE processa (comportamento atual).
        Com ele: DORMENTE só acorda/processa se a fala for a palavra-mestre — senão ignora
        (é assim que a voz de outra pessoa por perto não dispara nada); ATIVO processa e
        rearma o timer de sono. A palavra "mestre" SOZINHA acorda e só confirma ('Ouvindo…'),
        sem virar um comando vazio que o fluxo-mestre recusaria."""
        if not settings.mestre_wake:
            return True
        comando = (mestre.separar(texto, settings.palavra_mestre)
                   if settings.palavra_mestre_habilitada else None)
        if self.dormente:
            if comando is None:
                return False                       # dormindo e não chamaram "mestre"
            self.dormente = False
            await self.safe_send({"tipo": "navegar", "acao": "ativar_live"})
            telemetry.track("WS", "Palavra-mestre acordou o live.")
        self.last_activity = time.time()
        if comando == "":                          # só "mestre": acorda e espera o comando
            await self.safe_send({"tipo": "status", "texto": "Ouvindo…"})
            return False
        return True

    def _check_sono(self) -> None:
        """F3: ATIVO e parado há `mestre_sleep_seconds` -> volta a DORMIR (só a palavra-
        mestre reacorda). Diferente do idle de 90s (`_check_inatividade`), que consolida
        conhecimento e libera a GPU — dormir só PARA de responder a fala comum, mantendo a
        conexão viva. Não dorme com uma resposta em voo (o usuário está esperando)."""
        if not settings.mestre_wake or self.dormente:
            return
        if self.pipeline_task and not self.pipeline_task.done():
            return
        if time.time() - self.last_activity < settings.mestre_sleep_seconds:
            return
        self.dormente = True
        telemetry.track("WS", "Live dormiu (silêncio) — diga 'mestre' para acordar.")
        self.ctx.track_task(self.safe_send({"tipo": "navegar", "acao": "dormir_live"}))

    # -- áudio (VAD servidor) ---------------------------------------------------
    def _on_audio(self, raw: bytes) -> None:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(pcm ** 2))) if pcm.size else 0.0
        if rms > settings.vad_rms_threshold:
            if not self.is_recording:
                agora = time.time()
                self._fala_inicio = agora   # duração da fala alimenta a janela adaptativa
                # TAXA DE CORTE-PRECOCE (#3): retomar a fala logo depois de um endpoint
                # CURTO sugere que cortamos um enunciado que ia continuar. É este log que
                # decide se vad_silence_curta aperta ou relaxa — medir antes de cravar.
                if (
                    self._fim_fala_ts is not None
                    and self._janela_usada is not None
                    and self._janela_usada < settings.vad_silence_seconds
                    and agora - self._fim_fala_ts < settings.vad_silence_seconds
                ):
                    telemetry.warn(
                        "VAD",
                        f"Retomada {round((agora - self._fim_fala_ts) * 1000)}ms após "
                        "endpoint curto — possível corte precoce (calibre MENTE_VAD_SILENCE_CURTA_SECONDS).",
                    )
            # Religa o LLM no PRIMEIRO frame de fala (painel 2026-07): o reload de
            # 1-2s pós-idle corre em paralelo com a fala + VAD + STT, em vez de ser
            # pago serialmente no TTFT da primeira resposta. Só na TRANSIÇÃO
            # silêncio→fala (não por frame: `ready` fica False durante todo o load e
            # cada frame criaria mais uma task na fila do _load_lock) e só ACORDADO
            # (dormente = voz de fundo não deve reancorar a VRAM que o idle liberou).
            if not self.is_recording and not self.dormente and not self.ctx.llama.ready:
                self.ctx.track_task(self.ctx.llama.ensure_loaded())
            self.is_recording = True
            self.last_audio_time = time.time()
        if self.is_recording:
            self.audio_buffer.append(pcm)

    async def _check_silence(self) -> None:
        if not self.is_recording:
            return
        # Endpointing adaptativo (#3): a janela depende da DURAÇÃO da fala até o último
        # frame com voz — comando curto encerra mais cedo, raciocínio longo mantém margem.
        janela = janela_endpoint(self.last_audio_time - self._fala_inicio)
        if time.time() - self.last_audio_time <= janela:
            return
        self.is_recording = False
        self._fim_fala_ts = time.time()
        self._janela_usada = janela          # p/ o detector de corte-precoce (_on_audio)
        buffer, self.audio_buffer = self.audio_buffer, []
        if len(buffer) < settings.vad_min_frames:
            return
        final_audio = np.concatenate(buffer)
        _t_stt = time.perf_counter()
        texto = await self.ctx.stt.transcribe(final_audio)
        stt_ms = round((time.perf_counter() - _t_stt) * 1000)
        if len(texto) < 3:
            return
        if not await self._deve_processar(texto):
            return   # F3: dormente e não foi "mestre" -> ignora (voz de fundo/de outros)
        self._marcar_ativa()
        await self.safe_send({"tipo": "transcricao", "texto": texto})
        await self._dump_pergunta(texto)
        # vad_ms = a janela de silêncio PAGA neste turno (waterfall #1): é o pedaço do
        # TTFA que mora antes do STT e que o modo adaptativo existe para encolher.
        self._start_pipeline(texto, stt_ms=stt_ms, vad_ms=round(janela * 1000))

    # -- texto / controle -------------------------------------------------------
    async def _on_text(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            telemetry.warn("WS", f"Payload de texto inválido: {exc}")
            return

        tipo = payload.get("tipo")
        if tipo == "barge_in":
            self.is_recording = False
            self.audio_buffer = []
            self._cancel_pipeline()

        elif tipo == "ack_proativo":
            # ACK de aplicação (painel 2026-07): o front CONFIRMOU que exibiu o push.
            # Só agora o scheduler conclui/reprograma — send "ok" em TCP meio-aberto
            # não prova nada. Não é atividade do usuário: não chama _marcar_ativa.
            ack_id = payload.get("id")
            if ack_id is not None and self.ctx.scheduler is not None:
                await self.ctx.scheduler.confirmar_entrega(str(ack_id))

        elif tipo == "end_session":
            self._finalizar_sessao()

        elif tipo == "set_conversa":
            # Reassocia o id da conversa atual (ex.: reconexão do WS) sem mexer no contexto.
            cid = (payload.get("id") or "").strip()
            if cid:
                self.memory.conversa_id = cid

        elif tipo == "nova_conversa":
            # "Novo chat": id novo e contexto limpo. A conversa anterior já foi encerrada
            # (end_session) pelo próprio front antes de trocar.
            cid = (payload.get("id") or "").strip()
            if cid:
                self._cancel_pipeline()
                self.memory.nova_conversa(cid)
                self._marcar_ativa()
                if not self.ctx.llama.ready:   # novo chat = uso: religa cedo
                    self.ctx.track_task(self.ctx.llama.ensure_loaded())
                telemetry.track("WS", f"Nova conversa: {cid}")

        elif tipo == "carregar_conversa":
            # Reabre uma conversa do histórico: recarrega os turnos na RAM p/ continuar.
            cid = (payload.get("id") or "").strip()
            if cid:
                self._cancel_pipeline()
                turnos_raw = await asyncio.to_thread(db.get_conversation, cid, settings.max_chat_history)
                turnos = [(t["q"], t["a"]) for t in turnos_raw]
                self.memory.carregar_conversa(cid, turnos)
                telemetry.track("WS", f"Conversa reaberta: {cid} ({len(turnos)} turnos)")

        elif tipo == "texto":
            texto = (payload.get("payload") or "").strip()
            if not texto:
                return
            if not await self._deve_processar(texto):
                return   # F3: dormente e não foi "mestre"
            self._marcar_ativa()
            # Digitou = uso: religa o modelo JÁ (mesmo racional do nova_conversa),
            # em paralelo com o dump — em vez de dentro do stream(), serial.
            if not self.ctx.llama.ready:
                self.ctx.track_task(self.ctx.llama.ensure_loaded())
            await self.safe_send({"tipo": "transcricao", "texto": texto})
            await self._dump_pergunta(texto)
            # Turno DIGITADO fica sem o estilo falado DE PROPÓSITO (sem stt_ms ->
            # origem_voz=False no pipeline): quem digitou está LENDO a resposta na
            # tela — listas/markdown ajudam ali; o áudio que o live toca é bônus.
            # Só a fala transcrita (_check_silence) ativa o registro de conversa.
            self._start_pipeline(texto)
