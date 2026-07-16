"""
Mente Digital — ponto de entrada FastAPI.

Só faz a montagem (wiring) e expõe rotas. Toda a lógica vive nos módulos:
config, telemetry, llm, audio, rag, agent, ws.

    python -m mente_digital        (ou)   uvicorn mente_digital.main:app
"""
from __future__ import annotations

import asyncio
import gc
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# KMP/tokenizers ANTES de qualquer import pesado de ML
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from agent import Agent, EtlProcessor  # noqa: E402
from audio import SttService, TtsService  # noqa: E402
from config import settings  # noqa: E402
from llm import LlamaManager  # noqa: E402
from rag import EmbeddingProvider, VectorStore, WebSearcher  # noqa: E402
from state import AppContext, SessionMemory  # noqa: E402
from telemetry import db, telemetry  # noqa: E402
from ws import LiveSession  # noqa: E402


async def _boot(ctx: AppContext) -> None:
    """Carrega modelos sem bloquear o startup do servidor."""
    # GPU: em background (inclui warm-up).
    ctx.track_task(ctx.llama.load())
    # CPU: Whisper e Piper em threads.
    await asyncio.to_thread(ctx.stt.load)
    await asyncio.to_thread(ctx.tts.load)
    # RAG: embeddings (singleton) -> abre/sincroniza o VectorDB.
    await asyncio.to_thread(ctx.vectorstore.load_embeddings)
    await ctx.vectorstore.open()
    ctx.track_task(ctx.vectorstore.sync())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    await asyncio.to_thread(db.init)

    ctx = AppContext(settings=settings, memory=SessionMemory(settings))
    ctx.llama = LlamaManager()
    ctx.stt = SttService()
    ctx.tts = TtsService()
    # Embeddings singleton compartilhado: o VectorStore o carrega no boot e o
    # WebSearcher reusa a MESMA instância para rankear os trechos do deep-fetch
    # (RAG efêmero) — sem carregar um segundo modelo nem gastar VRAM extra.
    embeddings = EmbeddingProvider()
    ctx.web = WebSearcher(embeddings)
    ctx.vectorstore = VectorStore(embeddings)
    ctx.agent = Agent(ctx)
    ctx.etl = EtlProcessor(ctx)
    app.state.ctx = ctx

    await _boot(ctx)
    telemetry.track("SERVER", "Mente Digital online.")
    try:
        yield
    finally:
        ctx.llama.shutdown()
        gc.collect()
        telemetry.track("SERVER", "Encerrado.")


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


def get_ctx(request: Request = None, websocket: WebSocket = None) -> AppContext:
    target = request or websocket
    return target.app.state.ctx


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})


@app.get("/api/historico")
async def obter_historico(request: Request):
    # Agora vem do SQLite (persistente entre reinícios), não só da RAM.
    historico = await asyncio.to_thread(db.get_history, 200)
    return JSONResponse(content=historico)


@app.get("/api/conversas")
async def listar_conversas(request: Request):
    """Histórico agrupado em CONVERSAS (não turnos soltos) — o que o sidebar lista."""
    conversas = await asyncio.to_thread(db.get_conversations, 100)
    return JSONResponse(content=conversas)


@app.get("/api/conversa/{cid}")
async def obter_conversa(cid: str, request: Request):
    """Todos os turnos de uma conversa, para reabrir e continuar o chat."""
    turnos = await asyncio.to_thread(db.get_conversation, cid, 1000)
    return JSONResponse(content=turnos)


@app.get("/api/metrics")
async def obter_metricas(request: Request):
    metricas = await asyncio.to_thread(db.metrics)
    ctx = get_ctx(request)
    metricas["sessao"] = {
        "chat_history_ram": len(ctx.memory.chat_history),
        "conhecimento_sessao": len(ctx.memory.conhecimento_sessao),
        "fila_etl": len(ctx.memory.fila_etl),
        "llm_pronto": ctx.llama.ready,
        "stt_pronto": ctx.stt.ready,
        "tts_pronto": ctx.tts.ready,
        "vectordb_pronto": ctx.vectorstore.ready,
    }
    return JSONResponse(content=metricas)


@app.post("/api/nota/texto")
async def receber_nota_texto(request: Request):
    ctx = get_ctx(request)
    data = await request.json()
    texto = (data.get("texto") or "").strip()
    if not texto:
        return {"status": "vazio"}
    nome = f"Nota_Manual_{int(time.time())}.md"
    caminho = os.path.join(settings.caminho_obsidian, nome)

    def _save() -> None:
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(f"# Nota Rápida\n\n{texto}")

    try:
        await asyncio.to_thread(_save)
        ctx.track_task(ctx.vectorstore.sync())
        return {"status": "ok", "arquivo": nome}
    except Exception as exc:
        telemetry.error("NOTA", "Erro ao salvar nota manual", exc)
        return JSONResponse(content={"status": "erro"}, status_code=500)


@app.websocket("/ws/chat_live")
async def websocket_endpoint(websocket: WebSocket):
    ctx = get_ctx(websocket=websocket)
    await LiveSession(ctx, websocket).run()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.host, port=settings.port, log_level="error", reload=False)
