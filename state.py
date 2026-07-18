"""
Estado compartilhado da aplicação.

No monólito isso vivia solto em `app.state.*`. Aqui vira um container explícito,
injetado nos serviços — mais fácil de testar e sem coleções que crescem sem fim
(cada uma tem `maxlen`, evitando creep de RAM em sessões longas).
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator, Coroutine, Deque, Optional, Set, Tuple

from config import Settings

if TYPE_CHECKING:  # evita imports circulares em runtime
    from agent import Agent, EtlProcessor
    from audio import SttService, TtsService
    from llm import LlamaManager
    from rag import VectorStore, WebSearcher
    from scheduler import SchedulerService


class SessionMemory:
    """Memória volátil da sessão, toda limitada por tamanho."""

    def __init__(self, settings: Settings):
        # (pergunta, resposta) — contexto recente para o prompt
        self.chat_history: Deque[Tuple[str, str]] = deque(maxlen=settings.max_chat_history)
        # (tema, dados) — RAM de pre-fetch + web ("Memória Fresca da Sessão")
        self.conhecimento_sessao: Deque[Tuple[str, str]] = deque(maxlen=settings.max_session_knowledge)
        # (tema, dados) — fila drenada no end_session (ETL idle)
        self.fila_etl: Deque[Tuple[str, str]] = deque(maxlen=settings.max_etl_queue)
        # Conversa ATUAL: todo turno gravado carrega este id, e o histórico agrupa por
        # ele (ver telemetry.get_conversations). None = ainda não definida pelo cliente.
        self.conversa_id: Optional[str] = None
        # MODO CONFIDENCIAL (#5): quando True, o turno NÃO é persistido (sem dump, sem
        # SQLite, sem fila de ETL) — vive só na RAM desta sessão e morre com ela. O
        # follow-up ainda funciona (chat_history/conhecimento_sessao seguem em memória).
        self.confidencial: bool = False
        # DESFAZER (#8): as ações que REVERTEM a última mutação da sessão (lista de
        # `tools.Decisao`), guardadas na RAM. "mestre, desfaça" as executa e limpa este
        # campo (consumo único — não se desfaz o desfazer). None = nada a desfazer.
        # Vive só na sessão, então não persiste; um restart zera o histórico de undo.
        self.ultima_reversivel: Optional[list] = None
        # CORTA-E-CORRIGE (#9): as ações FORWARD (originais) da última mutação, para
        # "mestre, corrige para X" REFAZER com o valor certo (desfaz via ultima_reversivel
        # e reexecuta com o item corrigido). Anda junto com ultima_reversivel.
        self.ultima_acao: Optional[list] = None
        # COFRE DE CONFIRMAÇÃO (#25): a `tools.Decisao` destrutiva que está esperando um
        # "mestre, confirma" para rodar. None = nada pendente. Vive só na RAM da sessão.
        self.confirmacao_pendente: Optional[object] = None
        # ATALHO DE INTENÇÃO FREQUENTE (#2): o texto do último comando-mestre RESOLVIDO —
        # é o que "mestre, atalho X" grava sob o apelido X. None = nada a encurtar ainda.
        self.ultimo_comando_mestre: Optional[str] = None

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

    def nova_conversa(self, conversa_id: str) -> None:
        """Começa uma conversa do zero: novo id e contexto limpo (sem herdar o chat
        anterior). O ETL da conversa que estava aberta é disparado à parte (end_session)."""
        self.conversa_id = conversa_id
        self.chat_history.clear()
        self.conhecimento_sessao.clear()
        self.confidencial = False   # chat novo volta ao modo normal (público)
        self.ultima_reversivel = None   # não se desfaz ação de outra conversa
        self.ultima_acao = None
        self.confirmacao_pendente = None
        self.ultimo_comando_mestre = None

    def carregar_conversa(self, conversa_id: str, turnos: list[Tuple[str, str]]) -> None:
        """Reabre uma conversa existente: define o id e recarrega o histórico recente
        (últimos turnos) na RAM, para que o modelo continue com o contexto certo."""
        self.conversa_id = conversa_id
        self.chat_history.clear()
        for q, a in turnos[-self.chat_history.maxlen:]:
            self.chat_history.append((q, a))
        self.conhecimento_sessao.clear()
        self.ultima_reversivel = None   # reabrir conversa não herda undo pendente
        self.ultima_acao = None
        self.confirmacao_pendente = None
        self.ultimo_comando_mestre = None


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
    # NÃO existe `memory` aqui, e isso é deliberado: a memória de sessão é POR
    # CONEXÃO (LiveSession.memory) e viaja como parâmetro até quem precisa dela.
    # Antes era um SessionMemory único no AppContext, compartilhado por TODA conexão:
    # o cliente manda `set_conversa` no onopen e reconecta sozinho com backoff, então
    # bastava o celular estar aberto (ou o WS piscar) para o último a conectar
    # sobrescrever `conversa_id` — e os turnos seguintes eram gravados no SQLite
    # dentro da conversa ERRADA (corrupção persistente, não só de RAM). O
    # `chat_history` global ainda vazava o contexto de uma conversa no prompt da outra.
    # Sessões vivas, para o /api/metrics agregar (não é dono do estado, só observa).
    sessoes: Set["object"] = field(default_factory=set, repr=False)
    # Sinaliza que NÃO há inferência interativa rodando -> ETL idle pode usar a GPU.
    # Começa "setado" (livre). Manipulado SÓ pelo context manager `interativo()`.
    interactive_idle: asyncio.Event = field(default_factory=asyncio.Event)
    # Quantos pipelines interativos estão em voo. Ver `interativo()`.
    _interativos: int = field(default=0, repr=False)
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
    scheduler: "SchedulerService" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.interactive_idle.set()  # livre por padrão

    @contextlib.asynccontextmanager
    async def interativo(self) -> AsyncIterator[None]:
        """Marca "há inferência interativa em voo" — com CONTADOR, não booleano.

        O par clear()/set() solto tinha um furo que aparecia no fluxo mais comum do
        modo voz, o barge-in: `_cancel_pipeline` cancela a task A e cria a B na linha
        seguinte, sem esperar A morrer. Mas A demora de propósito — o `finally` do
        `LlamaManager.stream` só retorna quando a thread da GPU parou de fato. Quando
        A finalmente desenrolava, o `set()` dela DESFAZIA o `clear()` de B: o ETL era
        avisado de "GPU livre" com B decodificando, e entrava na frente da resposta
        do usuário. Com o contador, só o ÚLTIMO a sair libera.

        Mutação de int a partir do event loop é atômica -> não precisa de lock. O
        `finally` roda também no CancelledError (barge-in), então o contador não vaza.
        """
        self._interativos += 1
        self.interactive_idle.clear()
        try:
            yield
        finally:
            self._interativos = max(0, self._interativos - 1)
            if self._interativos == 0:
                self.interactive_idle.set()

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
