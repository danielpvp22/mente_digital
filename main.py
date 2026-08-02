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
from datetime import datetime, timedelta
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
from mente_digital import energia  # noqa: E402
from mente_digital.llm import LlamaManager  # noqa: E402
from mente_digital.rag import EmbeddingProvider, VectorStore, WebSearcher  # noqa: E402
from mente_digital import rede  # noqa: E402
from mente_digital.scheduler import SchedulerService  # noqa: E402
from mente_digital.state import AppContext  # noqa: E402
from mente_digital import vault_filtros  # noqa: E402
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


def _preimportar_arvores() -> None:
    """Importa as árvores pesadas do Whisper e do embedding na MESMA thread.

    É o que torna segura a paralelização abaixo. O bug de 2026-07-29 (#75): duas
    threads importando árvores que se cruzam (`transformers` -> `torch.utils.checkpoint`
    -> `sympy` -> `mpmath`) fizeram o CPython estourar `KeyError:
    'mpmath.functions.orthogonal'` — módulo visto pela metade pela outra thread. O app
    subia "saudável" e SEM RAG NENHUM, com um único WARN de pista.

    Régua que ficou: nunca despachar import pesado em background antes de um await que
    importe a mesma árvore. Aqui os imports acontecem UMA vez, single-thread; depois
    disso `sys.modules` está inteiro e os dois `load` só carregam PESO em paralelo.
    Falha de import não é fatal — o `load` de cada serviço tem o próprio pára-quedas."""
    for mod in ("faster_whisper", "sentence_transformers"):
        try:
            __import__(mod)
        except Exception as exc:
            telemetry.warn("BOOT", f"pré-import de {mod} falhou (segue): {exc}")


async def _malha_e_sync(ctx: AppContext) -> None:
    """O trabalho de RAG que não precisa segurar o boot — na ordem de sempre.

    Sequencial, nunca em tasks irmãos: os dois leem/escrevem o mesmo SQLite do Chroma
    (escritor único), e soltá-los soltos os faz brigar — medido em 2026-07-30, a malha
    ficou 79 s sem montar e o app atendeu sem índice de conceitos.

    O SYNC VEM PRIMEIRO desde 2026-08-02. Antes era malha→sync, e o `sync` reconstrói
    a malha no fim sempre que mexe no índice: a primeira construção era jogada fora
    segundos depois, toda vez que houvesse algo a indexar — 5,53 s medidos num boot
    real. Como o app escreve no próprio vault (o ETL do idle colhe átomos), "algo a
    indexar" é o caso NORMAL aqui, não a exceção.

    Invertida, a malha é construída UMA vez: o `sync` a refaz se mexeu em algo, e só
    quando ele não mexeu (ou falhou) é que a construímos por fora. O custo da inversão
    é a malha ficar pronta alguns segundos mais tarde — e o `search` já degrada com
    graça sem ela, mantendo o aterramento léxico e perdendo só a expansão por
    conceito, que é exatamente o motivo de ela estar fora do caminho crítico."""
    if not await ctx.vectorstore.sync():
        await ctx.vectorstore.reconstruir_malha()


async def _boot(ctx: AppContext) -> None:
    """Carrega modelos sem bloquear o startup do servidor."""
    # cuDNN: com o XTTS (torch, cuDNN 9) ligado, o faster-whisper (ctranslate2, cuDNN 8)
    # NÃO pode carregar o cuDNN primeiro — senão o torch não acha 'cudnnGetLibConfig'
    # (erro 127) e o XTTS crasha o processo ao carregar. Pré-inicializar o cuDNN do torch
    # antes do Whisper fixa a ordem das DLLs. Só quando o XTTS está ativo.
    if settings.tts_engine == "xtts":
        await asyncio.to_thread(_preinit_cudnn)
    # GPU: em background (inclui warm-up).
    ctx.track_boot_task(ctx.llama.load(), "Modelo na GPU")
    # CPU: Whisper e Piper em threads. Com `boot_paralelo`, o Whisper (2,50 s medidos)
    # carrega JUNTO com os embeddings (7,55 s) — os dois são CPU e independentes, então
    # o boot paga o maior em vez da soma. A ordem em relação ao _preinit_cudnn acima é
    # preservada: o cuDNN do torch já foi fixado antes de o ctranslate2 tocar o dele.
    # O XTTS custa ~17s de boot e ~1,4 GB de VRAM, e desde o portão de fala
    # (`state.turno_falado`) uma sessão só de TEXTO nunca o usa. Quem o acorda é o
    # microfone abrindo (`ws._on_audio`), com o `_falar` esperando o que faltar. O
    # Piper continua no boot: é CPU, leve, e não disputa VRAM com o LLM.
    preguicoso = settings.tts_carga_preguicosa and settings.tts_engine == "xtts"
    if settings.boot_paralelo:
        await asyncio.to_thread(_preimportar_arvores)
        # O XTTS SAI NA FRENTE. Perfilado em 2026-08-02: ele é a tarefa mais LONGA
        # do boot (11,9 s de CPU com o torch já importado) e era despachada por
        # ÚLTIMO — então o "pronto" esperava por ela sozinha, depois de todo o resto
        # ter acabado. Malha e sync, que pareciam o gargalo, custam 2,49 s + 4,12 s e
        # terminam bem antes; e eles NÃO podem sair na frente, porque `open()` recusa
        # sem embeddings (rag.py:807), `reconstruir_malha` sem store (rag.py:875) e
        # `sync` idem (rag.py:898) — corrente dura. Quem podia adiantar era o XTTS,
        # que não depende de nada disso.
        #
        # ⚠ É a MESMA ordem que causou o bug de 2026-07-29 (`KeyError:
        # 'mpmath.functions.orthogonal'` — app "saudável" e SEM RAG NENHUM, com um
        # WARN de pista). A diferença que a torna segura está uma linha acima: o
        # `_preimportar_arvores` JÁ rodou, então `transformers/torch/sympy/mpmath`
        # estão inteiros em `sys.modules` e as duas threads não disputam import.
        # É literalmente a régua escrita no docstring daquela função.
        # Como a falha era SILENCIOSA, o teste é o log: `[MALHA] Índice de conceitos`
        # tem de aparecer com ~16.475 conceitos.
        if preguicoso and settings.tts_preparar_ram_no_boot:
            telemetry.track("XTTS", "Pré-montando em RAM (a VRAM só na 1ª voz).")
            ctx.track_boot_task(asyncio.to_thread(ctx.tts.preparar_ram), "Voz para a RAM")
        await asyncio.gather(
            asyncio.to_thread(ctx.stt.load),
            asyncio.to_thread(ctx.vectorstore.load_embeddings),
        )
    else:
        await asyncio.to_thread(ctx.stt.load)
    if not preguicoso:
        await asyncio.to_thread(ctx.tts.load)
    # RAG: embeddings (singleton, já carregados acima no caminho paralelo) -> abre o
    # VectorDB. A MALHA (3,09 s) sai do caminho crítico: sem ela o `search` mantém o
    # aterramento léxico original e só a expansão por conceito não acontece — a
    # degradação graciosa que o próprio `search` já tratava. O `_reconstruir_malha` tem
    # lock, então esta montagem e a do fim do `sync` não se cruzam.
    if not settings.boot_paralelo:
        await asyncio.to_thread(ctx.vectorstore.load_embeddings)
    await ctx.vectorstore.open(com_malha=not settings.boot_malha_background)
    if settings.boot_malha_background:
        # MALHA e SYNC no MESMO task, na ordem de sempre. Despachá-los como dois tasks
        # irmãos MEDIU MAL (2026-07-30): a malha ficou 79 s sem montar e o app rodou sem
        # índice de conceitos, porque os dois disputam o MESMO SQLite do Chroma — que é
        # de escritor único. Antes disso não acontecia por acidente de ordem: a malha
        # rodava AWAITADA, então terminava antes de o sync começar. Encadeando, os 3,09 s
        # saem do caminho crítico E a ordem relativa é a de sempre.
        ctx.track_boot_task(_malha_e_sync(ctx), "Malha e índice")
    else:
        ctx.track_boot_task(ctx.vectorstore.sync(), "Índice do vault")
    # XTTS EM RAM, DEPOIS DOS EMBEDDINGS — a ordem é o conserto de um bug real (medido
    # em 2026-07-29): despachado ANTES, o `preparar_ram` importava torch+coqui numa
    # thread enquanto o `load_embeddings` importava sentence-transformers noutra, e as
    # duas árvores se cruzam (transformers -> torch.utils.checkpoint -> sympy -> mpmath).
    # O CPython estourou `KeyError: 'mpmath.functions.orthogonal'` — módulo visto pela
    # metade por outra thread. Como o load_embeddings é AWAITADO, depois dele a árvore
    # pesada já está inteira em `sys.modules` e o import do XTTS não disputa nada.
    # O sintoma era traiçoeiro: fail-soft, app "saudável" e SEM RAG, só com um WARN.
    if preguicoso and settings.tts_preparar_ram_no_boot and not settings.boot_paralelo:
        # Em segundo plano: monta o modelo em RAM (as 19,3s de CPU medidas) para que o
        # microfone só pague o ~1s do device. Sem isto a 1ª fala espera o load inteiro.
        # Com `boot_paralelo` ele JÁ foi despachado lá em cima, na frente de tudo —
        # aqui é só o caminho serial, que não tem o pré-import a proteger a ordem.
        telemetry.track("XTTS", "Pré-montando em RAM (a VRAM só na 1ª voz).")
        ctx.track_boot_task(asyncio.to_thread(ctx.tts.preparar_ram), "Voz para a RAM")
    elif preguicoso and not settings.tts_preparar_ram_no_boot:
        telemetry.track("XTTS", "Carga preguiçosa: só sobe quando houver voz.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A PORTA PRIMEIRO, os modelos depois. O uvicorn só faz o bind DEPOIS deste
    # lifespan, então sem esta linha uma segunda instância gasta ~45 s carregando
    # tudo, escreve "Mente Digital online" e só então descobre que a porta é de
    # outro — deixando um zumbi com ~4,7 GB de VRAM presos. Ver rede.py.
    if rede.porta_em_uso(settings.host, settings.port):
        telemetry.error(
            "BOOT",
            f"Porta {settings.host}:{settings.port} já está em uso — quase sempre é "
            f"outra instância do Mente Digital já rodando. Feche-a antes de subir "
            f"esta (nenhum modelo foi carregado).",
        )
        raise RuntimeError(f"porta {settings.host}:{settings.port} ocupada")
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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """A marca do app na aba do navegador — o MESMO `.ico` da barra de tarefas.

    Sem gate, como a rota `/`: um ícone não revela nada que a SPA servida ali do
    lado já não revele. O arquivo é gerado sob demanda em `dados/marca/` (não é
    versionado — ver marca.py); a geração custa ~40 ms e só acontece uma vez.
    """
    from fastapi.responses import FileResponse

    from mente_digital import marca

    alvo = await asyncio.to_thread(marca.caminho_ico, BASE_DIR)
    if not alvo.exists():                       # geração falhou; segue sem ícone
        raise HTTPException(status_code=404, detail="não encontrado")
    return FileResponse(alvo, media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/health")
async def health(request: Request):
    """Prontidão de cada serviço — a ÚNICA /api sem gate de acesso, de propósito.

    Revela estritamente menos que a rota `/`, que já serve a SPA inteira sem gate
    nenhum: aqui só saem booleanos, sem caminho, contagem, config ou conteúdo. Em
    troca ela funciona de onde o gate não deixaria — a tela de boot do app nativo
    apontada para um servidor remoto, e o healthcheck do container (que bate de
    fora do loopback e hoje só passa por acidente de `/` não ser gateada).

    `pronto` NÃO exige tudo: sem STT/voz o app responde por texto normalmente, e
    com o XTTS preguiçoso a voz só sobe quando o microfone abre — esperá-la seria
    esperar para sempre. O que segura a porta é o que impede PERGUNTAR."""
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:                       # lifespan ainda montando o container
        return JSONResponse(content={"pronto": False, "servicos": {}})
    preguicoso = settings.tts_carga_preguicosa and settings.tts_engine == "xtts"
    # O boot despacha trabalho que NÃO segura a porta (malha, sync do Chroma, XTTS
    # para a RAM). Sem reportá-lo, um cliente concluiria "está pronto" enquanto três
    # jobs disputam GPU e disco — e a primeira pergunta pagaria a conta. Ver
    # AppContext.tarefas_de_fundo.
    fundo = ctx.tarefas_de_fundo()
    servicos = {
        "servidor": True,
        "llm": ctx.llama.ready,
        "vault": ctx.vectorstore.ready,
        "stt": ctx.stt.ready,
        "voz": ctx.tts.ready or preguicoso,
        "porta": True,                    # se esta resposta chegou, a porta atende
        "fundo": not fundo,
    }
    return JSONResponse(content={
        "pronto": all(servicos.values()),
        "servicos": servicos,
        "tarefas_de_fundo": fundo,
        "motor_voz": settings.tts_engine,
        "voz_preguicosa": preguicoso,
        # MODO ECONOMIA, dito com todas as letras. O app do celular deduzia standby de
        # `llm == false`, e isso é AMBÍGUO no caso mais comum de todos: durante o boot
        # normal o LLM também está em false, e o app mandava um `ligar` por cima de um
        # carregamento já em curso. Aqui a diferença entre "ainda não subiu" e "soltei
        # de propósito" é um campo, não uma inferência.
        "descansando": ctx.descansando,
    })


@app.post("/api/energia", dependencies=[Depends(exigir_acesso)])
async def energia_endpoint(request: Request):
    """Liga/desliga os modelos sem fechar o app — o botão de energia da janela.

    O app foi feito para ficar ABERTO o dia inteiro, e entre conversas os modelos
    seguram ~5 GB de VRAM e ~7 GB de RAM à toa. `liberar_vram` já sabia soltar
    tudo (nasceu para dar lugar ao OCR); aqui vira interruptor, com a medição
    antes/depois para o dono conferir em vez de acreditar.

    ⚠ Por que DESLIGADO é desligado, e não "dorme e acorda sozinho": o docstring
    de `liberar_vram` é explícito — nenhum destes serviços auto-carrega. O LLM
    voltaria sozinho (o `ws.py` chama `ensure_loaded` a cada mensagem), mas o
    embedding NÃO: o RAG passaria a responder sem contexto, o STT devolveria ""
    e o TTS ficaria mudo — os três em SILÊNCIO. Degradação silenciosa é o defeito
    que este projeto mais combate, então religar é ato explícito. O front chama
    "ligar" antes de mandar a mensagem quando está descansando."""
    ctx = get_ctx(request)
    corpo = await request.json() if await request.body() else {}
    acao = (corpo.get("acao") or "estado").strip().lower()

    if acao == "desligar":
        antes = energia.medir()
        liberados = await ctx.liberar_vram()
        await asyncio.to_thread(energia.enxugar)
        depois = energia.medir()
        ctx.descansando = True
        telemetry.track("ENERGIA", f"Descansando a pedido do dono ({', '.join(sorted(liberados)) or 'nada a soltar'}).")
        # As OUTRAS cascas precisam saber. Com a janela do PC e o celular abertos ao
        # mesmo tempo, quem não clicou continuaria mostrando "Ligado" e, pior, pularia
        # o religar-antes-de-enviar — a resposta viria com o RAG cego, sem aviso.
        await _avisar_energia(ctx, "descansando", depois)
        return JSONResponse(content={
            "estado": "descansando", "liberados": sorted(liberados),
            "antes": antes, "depois": depois, "liberou": energia.delta(antes, depois),
        })

    if acao == "ligar":
        antes = energia.medir()
        await ctx.restaurar_vram()      # STT, TTS e embeddings — o LLM é lazy
        await ctx.llama.ensure_loaded()
        ctx.descansando = False
        # Religar É uso: sem isto o watcher de economia veria "20 min sem turno" e
        # mandaria dormir logo depois de o celular ter acabado de acordar o PC.
        ctx.marcar_uso()
        telemetry.track("ENERGIA", "Religado a pedido do dono.")
        depois = energia.medir()
        await _avisar_energia(ctx, "ligado", depois)
        return JSONResponse(content={
            "estado": "ligado", "antes": antes, "depois": depois,
        })

    return JSONResponse(content={
        "estado": "descansando" if ctx.descansando else "ligado",
        "depois": energia.medir(),
    })


async def _avisar_energia(ctx: AppContext, estado: str, medida: dict) -> None:
    """Empurra a mudança de energia às sessões vivas, via scheduler (dono do push).
    Fail-soft: sem scheduler (teste, container mínimo) o app segue — o chip só não
    atualiza sozinho na outra casca."""
    sched = getattr(ctx, "scheduler", None)
    if sched is None:
        return
    try:
        await sched.avisar_energia(estado, medida)
    except Exception as exc:                        # noqa: BLE001 - aviso, nunca fatal
        telemetry.warn("ENERGIA", f"Não consegui avisar as sessões: {exc}")


@app.post("/api/idle", dependencies=[Depends(exigir_acesso)])
async def idle_endpoint(request: Request):
    """Dispara e INTERROMPE a consolidação de fundo, sob comando da casca.

    Existe porque o gatilho novo é a inatividade do DONO (teclado e mouse parados
    na máquina inteira — ver `ocioso.py`), não a inatividade do chat, que o
    `ws._check_inatividade` já cobre. Quem observa isso é o app.py; aqui fica só
    o braço que executa.

    ⚠ O contrato do "parar" é `liberar o que o IDLE ocupou -- o app segue pronto`
    (decisão do dono, 2026-08-02), e NÃO "desligar tudo". Por isso guardamos se o
    LLM já estava carregado ANTES de o idle começar: se estava, ele fica; se o
    idle é que o subiu, ele volta a sair. Sem essa memória, parar o idle deixaria
    a máquina em estado diferente do que estava, e a próxima pergunta pagaria os
    7,3 s de religamento que ele não pediu."""
    ctx = get_ctx(request)
    corpo = await request.json() if await request.body() else {}
    acao = (corpo.get("acao") or "estado").strip().lower()
    estado = request.app.state

    tarefa = getattr(estado, "idle_task", None)
    rodando = tarefa is not None and not tarefa.done()

    if acao == "rodar":
        if rodando:
            return JSONResponse(content={"estado": "rodando", "ja_estava": True})
        estado.idle_llm_estava_pronto = ctx.llama.ready
        estado.idle_task = ctx.track_task(ctx.etl.run_idle([]))
        telemetry.track("IDLE", "Consolidação disparada pela ociosidade do dono.")
        return JSONResponse(content={"estado": "rodando", "ja_estava": False})

    if acao == "parar":
        if not rodando:
            return JSONResponse(content={"estado": "parado", "ja_estava": True})
        tarefa.cancel()
        # Devolve SÓ o que o idle pegou. `ensure_loaded`/`unload` são idempotentes,
        # então reafirmar o estado anterior é seguro mesmo se nada tiver mudado.
        if not getattr(estado, "idle_llm_estava_pronto", True) and ctx.llama.ready:
            await ctx.llama.unload()
            await asyncio.to_thread(energia.enxugar)
        telemetry.track("IDLE", "Consolidação interrompida — o dono voltou ao teclado.")
        return JSONResponse(content={"estado": "parado", "ja_estava": False,
                                     "medida": energia.medir()})

    return JSONResponse(content={"estado": "rodando" if rodando else "parado",
                                 "medida": energia.medir()})


@app.get("/api/imagem/{caminho:path}", dependencies=[Depends(exigir_acesso)])
async def imagem_do_vault(caminho: str):
    """Serve uma figura do vault para o chat (Fase 5b). SÓ imagem, SÓ dentro do
    vault: o guard de traversal vive em `_dentro_do_vault` (compartilhado com as
    rotas de nota), e a allowlist de extensão abaixo impede servir .md/.db por
    esta rota. O gate de acesso é o mesmo das demais /api — e como <img> não manda
    header, o token vai por query string, o mesmo tradeoff já aceito no WebSocket."""
    from fastapi.responses import FileResponse

    alvo = _dentro_do_vault(caminho)
    if alvo.suffix.lower() not in {".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg"}:
        raise HTTPException(status_code=404, detail="não encontrado")
    return FileResponse(alvo, headers={"Cache-Control": "public, max-age=86400"})


def _dentro_do_vault(caminho: str) -> Path:
    """Resolve `caminho` DENTRO do vault ou levanta 404. Guard único.

    `resolve()` + `is_relative_to` fecham path traversal: um `..%2f..` resolveria
    para fora e vazaria arquivo do disco. Mesma regra da rota de imagem — extraída
    para cá quando o modo avançado passou a ler notas, para não existirem duas
    implementações do guard (uma delas destinada a envelhecer errado)."""
    raiz = Path(settings.caminho_obsidian).resolve()
    alvo = (raiz / caminho).resolve()
    if not alvo.is_relative_to(raiz) or not alvo.is_file():
        raise HTTPException(status_code=404, detail="não encontrado")
    return alvo


@app.get("/api/nota", dependencies=[Depends(exigir_acesso)])
async def ler_nota(caminho: str, request: Request):
    """Texto cru de uma nota do vault — o que o modo avançado abre ao clicar numa
    fonte. Só `.md`: as demais extensões saem pela rota de imagem, que tem a
    allowlist própria, ou não saem."""
    alvo = _dentro_do_vault(caminho)
    if alvo.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="não encontrado")
    texto = await asyncio.to_thread(alvo.read_text, encoding="utf-8", errors="replace")
    return JSONResponse(content={
        "caminho": caminho, "nome": alvo.name, "texto": texto,
        "bytes": alvo.stat().st_size,
    })


# Varredura do vault para os FILTROS do navegador (pasta/origem/data). Medido no
# vault do dono: 24.838 notas, 0,16 s para o rglob e 1,6 s lendo o frontmatter de
# todas. Rápido para uma passada, caro para cada tecla digitada — daí o cache com
# TTL. Não é invalidação por conteúdo de propósito: o painel é ferramenta de
# navegação, e ver uma nota nascida há 90 s só no próximo minuto é irrelevante
# perto de varrer o disco a cada requisição.
_VAULT_TTL_SEGUNDOS = 120.0
_vault_cache: dict = {"t": 0.0, "itens": [], "por_caminho": {}}


def _varrer_vault() -> dict:
    """(bloqueante) Caminho, mtime e cabeçalho de cada nota. Vai em to_thread."""
    agora = time.time()
    if _vault_cache["itens"] and agora - _vault_cache["t"] < _VAULT_TTL_SEGUNDOS:
        return _vault_cache
    raiz = Path(settings.caminho_obsidian)
    itens = []
    for p in raiz.rglob("*.md"):
        try:
            rel = str(p.relative_to(raiz)).replace("\\", "/")
            mtime = p.stat().st_mtime
        except OSError:                      # sumiu entre o rglob e o stat
            continue
        meta = vault_filtros.descrever(rel, vault_filtros.ler_cabecalho(p))
        meta.update({"caminho": rel, "nome": p.stem, "mtime": mtime})
        itens.append(meta)
    itens.sort(key=lambda m: m["mtime"], reverse=True)
    _vault_cache.update({"t": agora, "itens": itens,
                         "por_caminho": {m["caminho"]: m for m in itens}})
    return _vault_cache


def _desde(dias: int) -> str:
    """A data ISO de corte, ou "" quando não há filtro de data."""
    if dias <= 0:
        return ""
    return (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")


@app.get("/api/vault/facetas", dependencies=[Depends(exigir_acesso)])
async def facetas_do_vault(request: Request):
    """As opções dos filtros, contadas a partir dos DADOS.

    A tela não traz lista fixa: uma pasta nova aparece sozinha, e uma família sem
    nota nenhuma não é oferecida numa lista que devolveria zero resultados."""
    cache = await asyncio.to_thread(_varrer_vault)
    return JSONResponse(content=vault_filtros.contar_facetas(cache["itens"]))


def _rel_do_vault(source: str) -> str:
    """O `source` do índice reduzido ao caminho relativo que a tela usa."""
    bruto = (source or "").replace("\\", "/")
    raiz = str(Path(settings.caminho_obsidian)).replace("\\", "/").rstrip("/") + "/"
    return bruto[len(raiz):] if bruto.startswith(raiz) else bruto


def _chaves_da_malha(caminho: str) -> tuple[str, ...]:
    """As grafias sob as quais uma nota pode estar na malha.

    ⚠ MEDIDO no vault do dono (2026-08-02), porque eu tinha suposto errado: o
    `source` gravado no índice (rag.py:273) é o caminho **ABSOLUTO com barras
    normais** — `D:/projetos/.../Cerebro_Digital/Conhecimento_Novo/nota.md`. Mas
    a tela trafega as duas formas: o modo "recentes" devolve caminho RELATIVO
    (montado do `rglob`) e o semântico devolve o `source` cru, absoluto. As duas
    chegam aqui, e as duas têm de achar a nota.

    Procurar só a forma recebida devolvia `nos: 0` — nem a semente — e a tela
    dizia "esta nota não tem vizinhos fortes". Falha SILENCIOSA e convincente:
    a mensagem é plausível, então ninguém desconfia do índice."""
    bruto = caminho.strip().strip('"')
    formas: list[str] = []

    def _juntar(valor: str) -> None:
        for s in (valor, valor.replace("\\", "/"), valor.replace("/", "\\")):
            if s and s not in formas:
                formas.append(s)

    _juntar(bruto)
    try:
        raiz = Path(settings.caminho_obsidian)
        alvo = Path(bruto)
        _juntar(str(alvo.relative_to(raiz)) if alvo.is_absolute() else str(raiz / bruto))
    except (ValueError, OSError):          # fora do vault, ou caminho inválido
        pass
    return tuple(formas)


@app.get("/api/vault/grafo", dependencies=[Depends(exigir_acesso)])
async def grafo_da_nota(caminho: str, saltos: int = 1, max_nos: int = 40,
                        request: Request = None):
    """A vizinhança de uma nota na malha de conceitos — o grafo do modo avançado.

    LOCAL, não global: 24.850 notas não formam desenho legível em renderizador
    nenhum. A pergunta que esta tela responde é "com o que ESTA nota conversa?".

    O corte por IDF é `settings.malha_idf_min` (4.0), o MESMO que a expansão de
    contexto usa (rag.py:1616) — e pela mesma razão, escrita no docstring da
    `MalhaIndex`: [[Python]] está em 101 átomos, [[DuckDB]] em 34. Sem o corte, o
    primeiro salto traria todo mundo que menciona um conceito-hub e o desenho
    seria idêntico para qualquer semente."""
    ctx = get_ctx(request)
    malha = getattr(ctx.vectorstore, "malha", None)
    if malha is None or malha.n_atomos == 0:
        # Distinto de "nota sem vizinhos": a malha some com o processo e é
        # remontada em segundo plano no boot. A tela precisa poder dizer
        # "ainda montando" em vez de "esta nota não conversa com nada".
        return JSONResponse(content={"nos": [], "arestas": [], "malha_pronta": False,
                                     "truncado": False, "alcancados": 0,
                                     "arestas_totais": 0})

    saltos = max(1, min(int(saltos or 1), 2))
    max_nos = max(2, min(int(max_nos or 40), 120))
    # Teto de arestas proporcional ao de nós. Num tema denso os vizinhos são todos
    # ligados entre si (~700 arestas para 39 nós, medido em "fotossíntese") e o
    # desenho vira um disco sólido. 2,5× dá trama sem virar mancha.
    max_arestas = int(max_nos * 2.5)
    for chave in _chaves_da_malha(caminho):
        g = await asyncio.to_thread(malha.vizinhanca, chave, saltos, max_nos,
                                    settings.malha_idf_min, max_arestas)
        if g["nos"]:
            break
    else:                                   # nenhuma grafia bateu
        g = {"nos": [], "arestas": [], "truncado": False, "alcancados": 0,
             "arestas_totais": 0}

    cache = await asyncio.to_thread(_varrer_vault)
    for no in g["nos"]:
        # O id vem na grafia do ÍNDICE (absoluta); o cache é indexado pelo caminho
        # RELATIVO, que é também o que a tela reenvia ao clicar num vizinho.
        rel = _rel_do_vault(no["id"])
        meta = cache["por_caminho"].get(rel) or {}
        no.update({"caminho": rel,
                   "nome": meta.get("nome") or Path(rel).stem,
                   "familia": meta.get("familia", "")})
    g["malha_pronta"] = True
    return JSONResponse(content=g)


@app.get("/api/notas", dependencies=[Depends(exigir_acesso)])
async def buscar_notas(q: str = "", k: int = 30, pasta: str = "", origem: str = "",
                       dias: int = 0, request: Request = None):
    """Busca no vault para o navegador do modo avançado.

    Usa a MESMA recuperação vetorial do assistente (`VectorStore.recuperar`) em vez
    de varrer os ~24.700 arquivos: além de ser rápido, é honesto — o que aparece
    aqui é o que o RAG realmente enxerga, então a tela serve para DEPURAR a busca,
    não só para navegar. Sem `q`, devolve as notas modificadas mais recentemente,
    que é o "o que mudou por aqui" útil na abertura do painel.

    `pasta`/`origem`/`dias` filtram (ver vault_filtros.py). O filtro é aplicado
    DEPOIS da recuperação vetorial, não dentro dela, e isso é deliberado: o
    `where` do Chroma não alcança `origem` nem `colhido_em` (o índice guarda só
    `source`/`mtime` — rag.py:273), e mover esses campos para o índice custaria
    reindexar 24 mil notas. Com filtro ativo a busca pede MAIS candidatos ao
    vetorial para não devolver três resultados por causa do corte."""
    k = max(1, min(int(k or 30), 100))
    corte = _desde(int(dias or 0))
    filtrando = bool(pasta or origem or corte)

    if not q.strip():
        cache = await asyncio.to_thread(_varrer_vault)
        itens = [m for m in cache["itens"] if vault_filtros.passa(m, pasta, origem, corte)]
        return JSONResponse(content={
            "modo": "recentes", "total_filtrado": len(itens),
            "itens": [{"caminho": m["caminho"], "nome": m["nome"], "trecho": "",
                       "dist": None, "familia": m["familia"],
                       "colhido_em": m["colhido_em"]} for m in itens[:k]],
        })

    # O container só é preciso DAQUI para baixo: listar arquivos por data não
    # depende do vetorial, e pedi-lo lá em cima acoplava o modo "recentes" a um
    # serviço que ele não usa.
    ctx = get_ctx(request)
    # Sobre-busca só quando há filtro: sem ele, pedir 4× seria pagar HNSW à toa.
    docs = await ctx.vectorstore.recuperar(q, k=min(k * 4, 400) if filtrando else k) or []
    cache = await asyncio.to_thread(_varrer_vault) if filtrando else None
    itens = []
    for doc, dist in docs:
        origem_doc = (getattr(doc, "metadata", {}) or {}).get("source", "")
        caminho = origem_doc.replace("\\", "/")
        meta = (cache["por_caminho"].get(caminho) if cache else None) or {}
        if filtrando and not vault_filtros.passa(meta, pasta, origem, corte):
            continue
        itens.append({
            "caminho": caminho,
            "nome": Path(origem_doc).stem if origem_doc else "(sem origem)",
            "trecho": (getattr(doc, "page_content", "") or "")[:280],
            "dist": round(float(dist), 4),
            "familia": meta.get("familia", ""),
            "colhido_em": meta.get("colhido_em", ""),
        })
        if len(itens) >= k:
            break
    return JSONResponse(content={"itens": itens, "modo": "semantica",
                                 "candidatos": len(docs)})


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
