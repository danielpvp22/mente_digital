"""
Mente Digital — ponto de entrada FastAPI.

Só faz a montagem (wiring) e expõe rotas. Toda a lógica vive nos módulos:
config, telemetry, llm, audio, rag, agent, ws.

    python main.py        (ou)   uvicorn main:app
"""
from __future__ import annotations

import asyncio
import gc
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# KMP/tokenizers ANTES de qualquer import pesado de ML
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from mente_digital import acesso  # noqa: E402
from mente_digital.agent import Agent, EtlProcessor  # noqa: E402
from mente_digital.audio import SttService, build_tts  # noqa: E402
from mente_digital.config import BASE_DIR, settings  # noqa: E402
from mente_digital.llm import LlamaManager  # noqa: E402
from mente_digital.rag import EmbeddingProvider, VectorStore, WebSearcher  # noqa: E402
from mente_digital.scheduler import SchedulerService  # noqa: E402
from mente_digital.state import AppContext  # noqa: E402
from mente_digital.telemetry import db, telemetry  # noqa: E402
from mente_digital.ws import LiveSession  # noqa: E402


def _preinit_cudnn() -> None:
    """Força o cuDNN do torch (9.x) a carregar AGORA. Sem efeito se não houver CUDA/torch."""
    try:
        import torch

        if torch.cuda.is_available():
            _ = torch.zeros(1, device="cuda")
            torch.backends.cudnn.version()   # carrega a lib cuDNN do torch no processo
    except Exception as exc:
        telemetry.warn("BOOT", f"pré-init do cuDNN falhou (segue): {exc}")


async def _boot(ctx: AppContext) -> None:
    """Carrega modelos sem bloquear o startup do servidor."""
    # cuDNN: com o XTTS (torch, cuDNN 9) ligado, o faster-whisper (ctranslate2, cuDNN 8)
    # NÃO pode carregar o cuDNN primeiro — senão o torch não acha 'cudnnGetLibConfig'
    # (erro 127) e o XTTS crasha o processo ao carregar. Pré-inicializar o cuDNN do torch
    # antes do Whisper fixa a ordem das DLLs. Só quando o XTTS está ativo.
    if settings.tts_engine == "xtts":
        await asyncio.to_thread(_preinit_cudnn)
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

    # Sem memória de sessão aqui: ela é POR CONEXÃO (LiveSession.memory). O AppContext
    # é container de SERVIÇOS, que são compartilháveis; estado de conversa não é.
    ctx = AppContext(settings=settings)
    ctx.llama = LlamaManager()
    ctx.stt = SttService()
    ctx.tts = build_tts()   # Piper (default) ou XTTS-v2 conforme MENTE_TTS_ENGINE
    # Embeddings singleton compartilhado: o VectorStore o carrega no boot e o
    # WebSearcher reusa a MESMA instância para rankear os trechos do deep-fetch
    # (RAG efêmero) — sem carregar um segundo modelo nem gastar VRAM extra.
    embeddings = EmbeddingProvider()
    ctx.web = WebSearcher(embeddings)
    ctx.vectorstore = VectorStore(embeddings)
    ctx.agent = Agent(ctx)
    ctx.etl = EtlProcessor(ctx)
    ctx.scheduler = SchedulerService(ctx)
    # #36 Diapasão: carrega o perfil de conversa persistido (o idle o refina depois).
    ctx.perfil_conversa = db.ler_perfil()
    app.state.ctx = ctx

    await _boot(ctx)
    # Agendador (lembretes/alarmes/watchers/briefing): loop de background retido no ctx,
    # então sobrevive a qualquer conexão individual. Lê a tabela `agendamentos` (persistente).
    if settings.scheduler_enabled:
        ctx.track_task(ctx.scheduler.run_forever())
    telemetry.track("SERVER", "Mente Digital online.")
    try:
        yield
    finally:
        ctx.scheduler.parar()
        # Drena as tasks de fundo ANTES do shutdown da GPU (painel 2026-07): o ETL
        # precisa do modelo para terminar, e llama.shutdown() derrubaria o executor
        # com um decode em voo. parar() veio antes para o run_forever poder sair.
        await ctx.drenar_tasks(timeout=10.0)
        ctx.llama.shutdown()
        gc.collect()
        telemetry.track("SERVER", "Encerrado.")


app = FastAPI(lifespan=lifespan)
# templates/ fica na RAIZ do repo (mesmo nível de main.py). Ancorado em BASE_DIR
# (derivado de config.py, não de main.py) em vez de relativo ao cwd — assim
# `python main.py` funciona de qualquer diretório (o único caminho do app que
# antes dependia do cwd).
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_ctx(request: Request = None, websocket: WebSocket = None) -> AppContext:
    target = request or websocket
    return target.app.state.ctx


async def exigir_acesso(request: Request) -> None:
    """Gate das rotas /api (painel #7): token via header/query OU loopback.
    A regra em si é pura e vive em acesso.py; aqui só se extrai host/token."""
    token = request.headers.get("x-mente-token") or request.query_params.get("token")
    host = request.client.host if request.client else None
    if not acesso.cliente_autorizado(host, token, settings.access_token):
        raise HTTPException(status_code=401, detail="não autorizado")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})


@app.get("/api/imagem/{caminho:path}", dependencies=[Depends(exigir_acesso)])
async def imagem_do_vault(caminho: str):
    """Serve uma figura do vault para o chat (Fase 5b). SÓ imagem, SÓ dentro do
    vault: `resolve()` + `is_relative_to` fecham path traversal (um `..%2f..` no
    caminho resolveria para fora e vazaria arquivo do disco), e a allowlist de
    extensão impede servir .md/.db por esta rota. O gate de acesso é o mesmo das
    demais /api — e como <img> não manda header, o token vai por query string,
    o mesmo tradeoff já aceito no WebSocket."""
    from fastapi.responses import FileResponse

    raiz = Path(settings.caminho_obsidian).resolve()
    alvo = (raiz / caminho).resolve()
    if not alvo.is_relative_to(raiz) or not alvo.is_file():
        raise HTTPException(status_code=404, detail="não encontrado")
    if alvo.suffix.lower() not in {".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg"}:
        raise HTTPException(status_code=404, detail="não encontrado")
    return FileResponse(alvo, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/historico", dependencies=[Depends(exigir_acesso)])
async def obter_historico(request: Request):
    # Agora vem do SQLite (persistente entre reinícios), não só da RAM.
    historico = await asyncio.to_thread(db.get_history, 200)
    return JSONResponse(content=historico)


@app.get("/api/conversas", dependencies=[Depends(exigir_acesso)])
async def listar_conversas(request: Request):
    """Histórico agrupado em CONVERSAS (não turnos soltos) — o que o sidebar lista."""
    conversas = await asyncio.to_thread(db.get_conversations, 100)
    return JSONResponse(content=conversas)


@app.get("/api/conversa/{cid}", dependencies=[Depends(exigir_acesso)])
async def obter_conversa(cid: str, request: Request):
    """Todos os turnos de uma conversa, para reabrir e continuar o chat."""
    turnos = await asyncio.to_thread(db.get_conversation, cid, 1000)
    return JSONResponse(content=turnos)


@app.get("/api/metrics", dependencies=[Depends(exigir_acesso)])
async def obter_metricas(request: Request):
    metricas = await asyncio.to_thread(db.metrics)
    # Ciclo do conhecimento (painel): snapshot da base + eventos DEDUP/PROMOCAO/PURGA.
    # Já nasce atrás do gate de acesso (#7) — a rota inteira exige token/loopback.
    metricas["base"] = await asyncio.to_thread(db.metricas_base)
    ctx = get_ctx(request)
    # A memória é por conexão, então aqui AGREGAMOS as sessões vivas em vez de ler
    # um estado global (que não existe mais — ver AppContext).
    sessoes = list(ctx.sessoes)
    metricas["sessao"] = {
        "conexoes": len(sessoes),
        "chat_history_ram": sum(len(s.memory.chat_history) for s in sessoes),
        "conhecimento_sessao": sum(len(s.memory.conhecimento_sessao) for s in sessoes),
        "fila_etl": sum(len(s.memory.fila_etl) for s in sessoes),
        "llm_pronto": ctx.llama.ready,
        "stt_pronto": ctx.stt.ready,
        "tts_pronto": ctx.tts.ready,
        "vectordb_pronto": ctx.vectorstore.ready,
    }
    return JSONResponse(content=metricas)


@app.post("/api/nota/texto", dependencies=[Depends(exigir_acesso)])
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
    # Gate ANTES do accept (painel #7): 1008 = policy violation. O browser não manda
    # header custom no handshake de WS, então o token vem por query (?token=...) —
    # tradeoff documentado; a URL do WS não é logada pelo uvicorn com log_level=error.
    token = websocket.query_params.get("token")
    host = websocket.client.host if websocket.client else None
    if not acesso.cliente_autorizado(host, token, settings.access_token):
        await websocket.close(code=1008)
        return
    if not acesso.origin_confere(websocket.headers.get("origin"), websocket.headers.get("host", "")):
        await websocket.close(code=1008)
        return
    await LiveSession(ctx, websocket).run()


if __name__ == "__main__":
    import uvicorn

    # TLS opcional (painel 2026-07): só passa os kwargs de SSL quando os DOIS
    # caminhos existem — senão o uvicorn sobe em HTTP como sempre. Isso destrava a
    # voz no celular (getUserMedia exige secure context fora de localhost).
    ssl_kwargs = {}
    if settings.ssl_cert and settings.ssl_key:
        if os.path.exists(settings.ssl_cert) and os.path.exists(settings.ssl_key):
            ssl_kwargs = {"ssl_certfile": settings.ssl_cert, "ssl_keyfile": settings.ssl_key}
            telemetry.track("SERVER", "TLS ligado (HTTPS/WSS).")
        else:
            telemetry.error(
                "SERVER",
                f"MENTE_SSL_CERT/KEY apontam para arquivo inexistente "
                f"({settings.ssl_cert!r}, {settings.ssl_key!r}) — subindo em HTTP.",
            )
    uvicorn.run(
        "main:app", host=settings.host, port=settings.port,
        log_level="error", reload=False, **ssl_kwargs,
    )
