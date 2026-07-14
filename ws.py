"""
Sessão WebSocket ao vivo (texto + voz com VAD/Barge-in).

Encapsula a máquina de estados que estava solta no endpoint do monólito:
- VAD por RMS no servidor (início/fim de fala).
- Barge-in (interrupção) cancelando o pipeline em andamento.
- end_session disparando o ETL idle.
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

from agent import append_chat_dump
from config import settings
from state import AppContext
from telemetry import telemetry

_RECV_TIMEOUT = 0.5  # s — granularidade da checagem de silêncio


class LiveSession:
    def __init__(self, ctx: AppContext, websocket: WebSocket) -> None:
        self.ctx = ctx
        self.ws = websocket
        self.audio_buffer: List["np.ndarray"] = []
        self.is_recording = False
        self.last_audio_time = time.time()
        self.pipeline_task: Optional[asyncio.Task] = None

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
            self.ctx.agent.pipeline_resposta(texto, self.safe_send)
        )

    # -- loop principal ---------------------------------------------------------
    async def run(self) -> None:
        await self.ws.accept()
        if not self.ctx.llama.ready:
            await self.safe_send(
                {"tipo": "status", "texto": "Modelo ainda carregando ou indisponível."}
            )
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(self.ws.receive(), timeout=_RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._check_silence()
                    continue

                if msg.get("type") == "websocket.disconnect":
                    break

                if "bytes" in msg and msg["bytes"] is not None:
                    self._on_audio(msg["bytes"])
                elif "text" in msg and msg["text"] is not None:
                    await self._on_text(msg["text"])

                await self._check_silence()
        except WebSocketDisconnect:
            telemetry.track("WS", "Cliente desconectou.")
        except Exception as exc:
            telemetry.error("WS", "Erro no loop do WebSocket", exc)
        finally:
            self._cancel_pipeline()

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
        await self.safe_send({"tipo": "transcricao", "texto": texto})
        await append_chat_dump("User", texto)
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
            telemetry.track("SERVER", "Sessão encerrada. Iniciando processamento IDLE...")
            itens = self.ctx.memory.drenar_etl()
            asyncio.create_task(self.ctx.etl.run_idle(itens))

        elif tipo == "texto":
            texto = (payload.get("payload") or "").strip()
            if not texto:
                return
            await self.safe_send({"tipo": "transcricao", "texto": texto})
            await append_chat_dump("User", texto)
            self._start_pipeline(texto)
