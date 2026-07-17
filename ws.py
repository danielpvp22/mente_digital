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

import tools
from agent import append_chat_dump
from config import settings
from state import AppContext, SessionMemory
from telemetry import db, telemetry

_RECV_TIMEOUT = 0.5  # s — granularidade da checagem de silêncio


class LiveSession:
    def __init__(self, ctx: AppContext, websocket: WebSocket) -> None:
        self.ctx = ctx
        self.ws = websocket
        self.audio_buffer: List["np.ndarray"] = []
        self.is_recording = False
        self.last_audio_time = time.time()
        self.last_activity = time.time()   # qualquer interação: rearma o timer de idle
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

    def _start_pipeline(self, texto: str) -> None:
        self._cancel_pipeline()
        self.pipeline_task = asyncio.create_task(
            self.ctx.agent.pipeline_resposta(texto, self.safe_send, self.memory)
        )

    async def _dump_pergunta(self, texto: str) -> None:
        """Grava a pergunta no dump — MENOS quando é efêmera.

        O dump é a matéria-prima que o idle atomiza em Zettelkasten permanente. Um
        turno sobre cotação/clima ali vira nota eterna sobre um dado que expira em
        horas (medido: 3 dos átomos-lixo entraram por aqui). O par IA é guardado pelo
        mesmo critério, no pipeline. O turno em si não se perde: vai pro SQLite.
        """
        if tools.e_efemero(texto):
            return
        await append_chat_dump("User", texto)

    # -- loop principal ---------------------------------------------------------
    async def run(self) -> None:
        await self.ws.accept()
        self.ctx.sessoes.add(self)
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
                    continue

                if msg.get("type") == "websocket.disconnect":
                    break

                if "bytes" in msg and msg["bytes"] is not None:
                    self._on_audio(msg["bytes"])
                elif "text" in msg and msg["text"] is not None:
                    await self._on_text(msg["text"])

                await self._check_silence()
                self._check_inatividade()
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
        telemetry.track("SERVER", "Sessão encerrada. Iniciando processamento IDLE...")
        itens = self.memory.drenar_etl()
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

    # -- áudio (VAD servidor) ---------------------------------------------------
    def _on_audio(self, raw: bytes) -> None:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(pcm ** 2))) if pcm.size else 0.0
        if rms > settings.vad_rms_threshold:
            self.is_recording = True
            self.last_audio_time = time.time()
        if self.is_recording:
            self.audio_buffer.append(pcm)

    async def _check_silence(self) -> None:
        if not self.is_recording:
            return
        if time.time() - self.last_audio_time <= settings.vad_silence_seconds:
            return
        self.is_recording = False
        buffer, self.audio_buffer = self.audio_buffer, []
        if len(buffer) < settings.vad_min_frames:
            return
        final_audio = np.concatenate(buffer)
        texto = await self.ctx.stt.transcribe(final_audio)
        if len(texto) < 3:
            return
        self._marcar_ativa()
        await self.safe_send({"tipo": "transcricao", "texto": texto})
        await self._dump_pergunta(texto)
        self._start_pipeline(texto)

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
            self._marcar_ativa()
            await self.safe_send({"tipo": "transcricao", "texto": texto})
            await self._dump_pergunta(texto)
            self._start_pipeline(texto)
