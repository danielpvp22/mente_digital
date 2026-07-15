"""
Estado compartilhado da aplicação.

No monólito isso vivia solto em `app.state.*`. Aqui vira um container explícito,
injetado nos serviços — mais fácil de testar e sem coleções que crescem sem fim
(cada uma tem `maxlen`, evitando creep de RAM em sessões longas).
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Coroutine, Deque, Optional, Set, Tuple

from config import Settings

if TYPE_CHECKING:  # evita imports circulares em runtime
    from agent import Agent, EtlProcessor
    from audio import SttService, TtsService
    from llm import LlamaManager
    from rag import VectorStore, WebSearcher


class SessionMemory:
    """Memória volátil da sessão, toda limitada por tamanho."""

    def __init__(self, settings: Settings):
        # (pergunta, resposta) — contexto recente para o prompt
        self.chat_history: Deque[Tuple[str, str]] = deque(maxlen=settings.max_chat_history)
        # (tema, dados) — RAM de pre-fetch + web ("Memória Fresca da Sessão")
        self.conhecimento_sessao: Deque[Tuple[str, str]] = deque(maxlen=settings.max_session_knowledge)
        # (tema, dados) — fila drenada no end_session (ETL idle)
        self.fila_etl: Deque[Tuple[str, str]] = deque(maxlen=settings.max_etl_queue)

    def registrar_turno(self, pergunta: str, resposta: str) -> None:
        self.chat_history.append((pergunta, resposta))

    def lembrar(self, tema: str, dados: str) -> None:
        self.conhecimento_sessao.append((tema, dados))

    def enfileirar_etl(self, tema: str, dados: str) -> None:
        self.fila_etl.append((tema, dados))

    def drenar_etl(self) -> list[Tuple[str, str]]:
        itens = list(self.fila_etl)
        self.fila_etl.clear()
        return itens


class LruCache:
    """Cache LRU simples e thread-agnóstico (usado pela busca web)."""

    def __init__(self, maxsize: int):
        self.maxsize = maxsize
        self._data: "OrderedDict[str, str]" = OrderedDict()

    def get(self, key: str) -> Optional[str]:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: str, value: str) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


@dataclass
class AppContext:
    """Amarra configuração, serviços e memória. Guardado em app.state.ctx."""

    settings: Settings
    memory: SessionMemory
    # Sinaliza que NÃO há inferência interativa rodando -> ETL idle pode usar a GPU.
    # Começa "setado" (livre). O pipeline limpa ao entrar e seta ao sair.
    interactive_idle: asyncio.Event = field(default_factory=asyncio.Event)
    # Referências fortes das tasks de background (ver track_task). Vive tanto quanto
    # o app, então tasks disparadas dentro de uma sessão sobrevivem ao fim dela.
    _bg_tasks: Set["asyncio.Task"] = field(default_factory=set, repr=False)

    # Serviços (preenchidos no lifespan)
    llama: "LlamaManager" = None          # type: ignore[assignment]
    stt: "SttService" = None              # type: ignore[assignment]
    tts: "TtsService" = None              # type: ignore[assignment]
    vectorstore: "VectorStore" = None     # type: ignore[assignment]
    web: "WebSearcher" = None             # type: ignore[assignment]
    agent: "Agent" = None                 # type: ignore[assignment]
    etl: "EtlProcessor" = None            # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.interactive_idle.set()  # livre por padrão

    def track_task(self, coro: Coroutine) -> "asyncio.Task":
        """Cria uma task de background SEGURANDO uma referência forte a ela.

        O event loop mantém só uma referência FRACA às tasks. Sem guardar uma
        referência forte, o GC pode coletar a task no meio da execução e a
        corrotina morre em silêncio (footgun documentado do asyncio). Isso era um
        risco real em `agent._prefetch`, no ETL idle (`etl.run_idle`) e nas syncs
        do VectorDB — trabalho que sumia sem log. Aqui a task fica retida no set
        até terminar, quando o done-callback a remove.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task
