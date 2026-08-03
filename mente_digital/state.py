"""
Estado compartilhado da aplicação.

No monólito isso vivia solto em `app.state.*`. Aqui vira um container explícito,
injetado nos serviços — mais fácil de testar e sem coleções que crescem sem fim
(cada uma tem `maxlen`, evitando creep de RAM em sessões longas).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator, Coroutine, Deque, List, Optional, Set, Tuple

from mente_digital.config import Settings
from mente_digital.telemetry import telemetry

if TYPE_CHECKING:  # evita imports circulares em runtime
    from mente_digital.agent import Agent, EtlProcessor
    from mente_digital.audio import SttService, TtsService
    from mente_digital.llm import LlamaManager
    from mente_digital.rag import VectorStore, WebSearcher
    from mente_digital.scheduler import SchedulerService


# ESTE TURNO VAI SER OUVIDO? (2026-07-29, medido no teste guiado)
#
# O `_falar` sintetiza DENTRO do laço de tokens (`await synth_base64`), então o TTS
# está no caminho crítico: num turno DIGITADO, o texto para na tela enquanto o XTTS
# gera um áudio que ninguém vai ouvir. Medido em 19 turnos reais: `tts_total` ficou
# em ~95% do relógio (36,7 s totais com 35,2 s de TTS no pior caso).
#
# Por que ContextVar e não um parâmetro: a fala converge para dois primitivos, mas
# é ACIONADA de 40+ lugares (quase todo comando-mestre chama `_emitir_falado`).
# Enfiar um argumento por essa árvore toda seria uma mudança espalhada e fácil de
# esquecer num ramo novo. E por que não um atributo do Agent: ele é um SINGLETON
# em `ctx.agent`, compartilhado por todas as sessões — duas conversas simultâneas
# (uma por voz, outra digitada) corromperiam o estado uma da outra.
#
# ContextVar resolve os dois: cada turno já roda no seu próprio `asyncio.Task`
# (`ws._start_pipeline`), e `create_task` copia o contexto — o `set()` feito dentro
# do pipeline vive e morre naquele turno. O default `True` é deliberado: push
# proativo do scheduler (alarme, briefing) roda FORA de turno e deve falar sempre.
turno_falado: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "turno_falado", default=True)


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
        # MODO ECONÔMICO (#30): quando ligado, a busca local IGNORA o gate de
        # relevância e aceita QUALQUER átomo válido como contexto — responde do
        # vault em vez de escalar pra web (economiza chamada web + latência). É o
        # trade-off OPT-IN inverso do gate: menos precisão (reabre o risco do
        # "Cache Hit falso") por menos web. Meta-comando de sessão, como o confidencial.
        self.economico: bool = False
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
        # SRS (#43): sessão de revisão EM ANDAMENTO. dict {"pendentes": [cards], "atual":
        # card, "revelado": bool} ou None. Vive só na RAM (os cards persistem no SQLite; o
        # que é volátil é a fila da revisão atual). Como o cofre de confirmação, um comando
        # não-relacionado a abandona.
        self.revisao: Optional[dict] = None
        # TUTOR SOCRÁTICO (#44): modo de sessão. Quando True, as respostas de conhecimento
        # viram perguntas guiadas (via verbosidade.aplicar_tutor) em vez de resposta pronta.
        self.tutor: bool = False
        # "FONTE?" (painel 2026-07): proveniência da ÚLTIMA resposta de conhecimento —
        # itens "nota:<caminho>", "web:<domínio>" ("web:" = web sem domínio rastreado)
        # e "memoria". Zerada no INÍCIO de cada turno de conhecimento (barge-in no meio
        # nunca deixa fonte da resposta errada) e nas trocas de conversa. Só RAM — o
        # que de graça respeita o modo confidencial.
        self.ultimas_fontes: list = []

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
        self.economico = False      # chat novo volta ao modo normal (com gate)
        self.ultima_reversivel = None   # não se desfaz ação de outra conversa
        self.ultima_acao = None
        self.confirmacao_pendente = None
        self.ultimo_comando_mestre = None
        self.revisao = None             # revisão em andamento não atravessa conversas
        self.tutor = False              # modo tutor não atravessa conversas
        self.ultimas_fontes = []        # fonte da resposta de OUTRA conversa não vale aqui

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
        self.revisao = None
        self.tutor = False
        self.economico = False
        self.ultimas_fontes = []


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
    # Fase 3 (OCR): serviços do projeto que foram descarregados da VRAM para o OCR
    # rodar em outro processo/venv — guardados para RESTAURAR depois (nenhum deles
    # auto-carrega: STT devolveria "" e o TTS ficaria mudo em silêncio).
    _vram_liberada: Set[str] = field(default_factory=set, repr=False)
    # priv-02 (painel 2026-07-24): conversas em MODO SIGILOSO, por conversa_id. A
    # SessionMemory é por CONEXÃO, então um wifi piscando + reconexão automática
    # derrubava o sigilo EM SILÊNCIO (a memory nova nasce confidencial=False). O id
    # fica neste set RAM-only (nada persiste — coerente com a própria promessa) e o
    # `set_conversa` do ws restaura o modo na reconexão.
    sigilosas: Set[str] = field(default_factory=set, repr=False)
    # Sinaliza que NÃO há inferência interativa rodando -> ETL idle pode usar a GPU.
    # Começa "setado" (livre). Manipulado SÓ pelo context manager `interativo()`.
    interactive_idle: asyncio.Event = field(default_factory=asyncio.Event)
    # Quantos pipelines interativos estão em voo. Ver `interativo()`.
    _interativos: int = field(default=0, repr=False)
    # Referências fortes das tasks de background (ver track_task). Vive tanto quanto
    # o app, então tasks disparadas dentro de uma sessão sobrevivem ao fim dela.
    _bg_tasks: Set["asyncio.Task"] = field(default_factory=set, repr=False)
    # Só as tarefas que o `_boot` despachou, com o rótulo que a tela de boot mostra.
    # Ver `track_boot_task` para o porquê de ser lista de PERMISSÃO.
    _rotulo_boot: dict = field(default_factory=dict, repr=False)
    # #38: escrita no vault via ferramenta (nota/lista/captura) DURANTE a conversa não
    # reindexa na hora — o `sync` re-embeda os chunks E reconstrói a MALHA sobre ~13k
    # átomos na GPU serializada, o que congelava o PRÓXIMO turno (~46s: o STT em cuda e
    # o decode disputavam a GPU). Marcamos o vault "sujo" e o `EtlProcessor.run_idle`
    # sincroniza no idle pós-conversa, quando a GPU está livre (o mesmo padrão do ETL).
    # Tradeoff aceito: a nota entra no ÍNDICE só no próximo idle — no disco/Obsidian ela
    # já está na hora. É um booleano no event loop -> mutação atômica, sem lock.
    vault_pendente_sync: bool = field(default=False, repr=False)
    # Debounce do idle de conhecimento (2026-07-23): ao encerrar a conversa, os itens
    # drenados NÃO vão pro ETL na hora — ficam aqui e um timer espera `idle_grace_seconds`
    # SEM nova atividade. Abrir outra conversa ou mandar um turno re-adia (cancela o timer;
    # os itens permanecem). Assim o ETL não pousa na GPU a 100% em cima do STT/decode do
    # próximo turno. Ver agendar_idle_conhecimento / adiar_idle_conhecimento / _idle_apos_grace.
    _idle_itens: list = field(default_factory=list, repr=False)
    _idle_timer: Optional["asyncio.Task"] = field(default=None, repr=False)

    # Serviços (preenchidos no lifespan)
    llama: "LlamaManager" = None          # type: ignore[assignment]
    stt: "SttService" = None              # type: ignore[assignment]
    tts: "TtsService" = None              # type: ignore[assignment]
    vectorstore: "VectorStore" = None     # type: ignore[assignment]
    web: "WebSearcher" = None             # type: ignore[assignment]
    agent: "Agent" = None                 # type: ignore[assignment]
    etl: "EtlProcessor" = None            # type: ignore[assignment]
    scheduler: "SchedulerService" = None  # type: ignore[assignment]
    # WATTÍMETRO: o acumulador de energia por dia. Vive no ctx (e não em
    # `app.state`, como o registro de aparelhos) porque quem o alimenta é o tick do
    # scheduler, que só enxerga o ctx.
    consumo: object = None

    # MODO ECONOMIA (2026-08-02): os modelos estão soltos e a máquina livre — o
    # servidor segue de pé atendendo /api, mas LLM/Whisper/voz/embeddings saíram da
    # VRAM. Vive AQUI, e não em `app.state`, porque quem decide dormir sozinho é o
    # scheduler (que só enxerga o ctx) e quem responde "como você está?" é a rota
    # /api/energia: dois donos do mesmo booleano é como um deles fica desatualizado.
    descansando: bool = False
    # O ETL do idle está rodando? Marcado por `EtlProcessor.run_idle` (ponto único —
    # ele é disparado de três lugares: fim de conversa, rota /api/idle e testes). O
    # watcher de economia lê isto para não descarregar o modelo POR CIMA de uma
    # atomização em curso: soltar a VRAM debaixo do ETL não economiza nada, só perde
    # o trabalho que ele já tinha feito.
    idle_em_andamento: bool = False
    # Relógio (monotônico) do último turno INTERATIVO — o que o watcher de economia
    # usa para decidir que ninguém está usando.
    #
    # ⚠ Por que atividade e não "não há sessão": todo o resto do trabalho de fundo
    # (ingestão, OCR, consolidação) usa `ctx.sessoes` vazio como sinal de ócio, e
    # isso funciona porque são jobs que só rodam com o app FECHADO. O standby é o
    # oposto: ele existe justamente para o app aberto o dia inteiro, onde a sessão
    # nunca fecha. Com a régua da sessão ele jamais dispararia.
    ultima_interacao: float = field(default_factory=time.monotonic)
    # #29: orçamento de tokens de fundo, recalibrado pelo scheduler a cada tick a
    # partir da VRAM livre. None = sem leitura (sem CUDA) -> o fundo usa o max_tokens
    # configurado normalmente.
    orcamento_fundo: Optional[int] = None
    # #36 Diapasão: perfil de COMO o usuário prefere ser respondido. Carregado no
    # boot, refinado pelo idle, lido no hot-path (injetado na instrução de resposta).
    perfil_conversa: Optional[str] = None

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
        # Interação nova = adia o idle de conhecimento pendente (debounce). Sem isto, o ETL
        # agendado ao fim da conversa ANTERIOR pousaria na GPU durante ESTE turno.
        self.adiar_idle_conhecimento()
        self.marcar_uso()
        try:
            yield
        finally:
            self._interativos = max(0, self._interativos - 1)
            if self._interativos == 0:
                self.interactive_idle.set()
            # Também na SAÍDA: o relógio do standby conta do FIM do turno. Marcar só na
            # entrada faria uma resposta longa (síntese de tema, map-reduce) envelhecer
            # enquanto ainda estava sendo gerada.
            self.marcar_uso()

    async def aguardar_ocio(self, timeout: float) -> bool:
        """Espera o turno em voo terminar. False = estourou o tempo e ainda há um.

        Existe por causa do botão de MODO ECONOMIA, que agora existe no celular —
        e é apertado por quem está longe do PC, sem ver o que acontece nele.
        Descarregar os modelos no meio de uma resposta não a derruba (o pipeline
        é fail-soft), faz pior: ela sai DEGRADADA e calada. Medido em 2026-08-02,
        o `liberar_vram` no meio de um turno levou o embedding junto e a busca de
        figuras estourou `'NoneType' object has no attribute 'embed_query'` — o
        pára-quedas segurou ("seguindo só com texto") e a pessoa recebeu uma
        resposta pior sem nada dizendo que foi por isso.

        O watcher automático já vetava esse caso; o botão manual, não."""
        try:
            await asyncio.wait_for(self.interactive_idle.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def marcar_uso(self) -> None:
        """"Alguém está usando isto agora" — rearma o relógio do modo economia.

        Chamado de todo caminho que significa uso real: turno interativo (acima),
        religar pela rota de energia e abertura de conexão. Não é chamado por
        trabalho de FUNDO: o ETL/crawler rodando não é motivo para segurar a VRAM
        depois que ele terminar."""
        self.ultima_interacao = time.monotonic()

    def segundos_sem_uso(self, agora: Optional[float] = None) -> float:
        """Há quanto tempo ninguém interage. `agora` injetado para o teste não
        depender do relógio (mesmo padrão de `agenda.parse_quando`)."""
        return max(0.0, (agora if agora is not None else time.monotonic()) - self.ultima_interacao)

    def agendar_idle_conhecimento(self, itens: list) -> None:
        """Enfileira os itens do fim de conversa e (re)arma o timer de carência (debounce).

        Chamado pelo `_finalizar_sessao` no lugar de disparar o ETL na hora. O idle só roda
        após `idle_grace_seconds` SEM nova atividade — reabrir/conversar adia (ver
        `adiar_idle_conhecimento`). Se a carência for 0, roda de imediato (comportamento
        antigo). Os itens ACUMULAM entre encerramentos até o idle efetivamente rodar."""
        if itens:
            self._idle_itens.extend(itens)
        grace = self.settings.idle_grace_seconds
        if grace <= 0:                       # sem carência: comportamento antigo (imediato)
            self._disparar_idle_agora()
            return
        self._rearmar_idle_timer(grace)

    def adiar_idle_conhecimento(self) -> None:
        """Nova atividade (turno interativo OU abertura de conversa): adia o idle pendente.

        Cancela o timer de carência SEM perder os itens bufferados — eles serão re-agendados
        quando a sessão encerrar de novo (`agendar_idle_conhecimento`) ou pelo idle por
        inatividade. É o que impede o ETL de atropelar o próximo turno do usuário."""
        if self._idle_timer is not None and not self._idle_timer.done():
            self._idle_timer.cancel()
        self._idle_timer = None

    def _rearmar_idle_timer(self, grace: float) -> None:
        if self._idle_timer is not None and not self._idle_timer.done():
            self._idle_timer.cancel()
        self._idle_timer = self.track_task(self._idle_apos_grace(grace))

    async def _idle_apos_grace(self, grace: float) -> None:
        """Dorme a carência e então dispara o ETL — a menos que uma interação esteja em voo
        (re-arma) ou o timer tenha sido cancelado por atividade nova (adiar)."""
        try:
            await asyncio.sleep(grace)
        except asyncio.CancelledError:       # adiado por atividade nova: itens ficam pro próximo
            return
        # Decode interativo em voo? Não atropela: re-arma e tenta de novo após a carência.
        if not self.interactive_idle.is_set():
            self._rearmar_idle_timer(grace)
            return
        self._disparar_idle_agora()

    def _disparar_idle_agora(self) -> None:
        """Drena o buffer e roda o ETL idle (fora do timer). Nada pendente = no-op.

        GATE (2026-07-23): NÃO consolida enquanto houver sessão conectada — a atomização/
        indexação/malha/unload não são preemptíveis e atropelavam a próxima pergunta no meio
        da conversa. Com sessão viva, re-arma e espera a DESCONEXÃO (os itens ficam bufferados,
        nada se perde); ao desconectar, `_finalizar_sessao` re-agenda e aí `ctx.sessoes` está
        vazio -> dispara. É o ponto ÚNICO onde o ETL parte, então cobre a carência e o atalho
        de grace<=0. Desligável por `idle_adiar_com_sessao_viva`."""
        if self.settings.idle_adiar_com_sessao_viva and self.sessoes:
            self._rearmar_idle_timer(self.settings.idle_grace_seconds or 5.0)
            return
        itens = self._idle_itens
        self._idle_itens = []
        self._idle_timer = None
        if self.etl is not None:
            self.track_task(self.etl.run_idle(itens))

    def track_boot_task(self, coro: Coroutine, rotulo: str) -> "asyncio.Task":
        """`track_task` + marca a tarefa como sendo DO BOOT.

        A marcação explícita substituiu uma heurística que se mostrou errada em
        produção (2026-08-02): `tarefas_de_fundo` contava toda task retida menos
        uma lista de laços perpétuos, e isso era veneno — o app cria trabalho de
        fundo o tempo TODO em runtime (a passada de consolidação do scheduler, o
        ETL do idle). Resultado: a tela de boot do app nativo ficava presa em 80%
        esperando uma pendência que nunca acabava, porque a fila nunca esvazia.

        Lista de PERMISSÃO é a forma certa aqui: "o boot terminou?" é uma pergunta
        sobre um conjunto FECHADO e conhecido de tarefas, e nenhum trabalho criado
        depois pode entrar nela por acidente."""
        tarefa = self.track_task(coro)
        self._rotulo_boot[tarefa] = rotulo
        return tarefa

    def tarefas_de_fundo(self) -> List[str]:
        """Rótulos do trabalho de fundo AINDA em voo — sem os laços perpétuos.

        Existe para a tela de boot do app nativo (app.py). O `_boot` do main.py
        despacha trabalho que NÃO segura a porta: `llama.load`, `_malha_e_sync`
        (malha de conceitos + sync do Chroma) e o `preparar_ram` do XTTS. A porta
        abre antes deles terminarem, então quem só olhasse "os serviços estão
        `ready`?" anunciaria "tudo pronto" com três jobs disputando GPU e disco.

        Isso não é teórico: medido em 2026-08-02, a primeira pergunta feita logo
        após o boot decodificou a **44,8 tok/s** contra os 93–113 tok/s das dez
        amostras anteriores desta máquina, e a busca no vault levou 889 ms contra
        um p50 de 242 ms. `lock_wait_ms` era 0 — não era fila do executor de GPU,
        era disputa de recurso bruto com o boot.

        SÓ conta o que `track_boot_task` marcou. Não é genérico de propósito: uma
        tentativa anterior varria o `_bg_tasks` inteiro excluindo laços perpétuos, e
        o trabalho de runtime (consolidação, ETL do idle) entrava na conta e travava
        a tela de boot em 80% para sempre. Ver `track_boot_task`.

        Efeito colateral bom: a lista se limpa sozinha. Task concluída sai do dict,
        então isto não cresce num processo que fica dias no ar."""
        pendentes: List[str] = []
        for task in list(self._rotulo_boot):
            if task.done():
                self._rotulo_boot.pop(task, None)
                continue
            pendentes.append(self._rotulo_boot[task])
        return sorted(set(pendentes))

    def _provedor_de_embeddings(self):
        """O `EmbeddingProvider` de dentro do VectorStore, ou None.

        ⚠ BUG CORRIGIDO em 2026-08-02: isto era um `getattr(vectorstore,
        "embeddings", None)` inline, mas o `VectorStore.__init__` guarda o
        provedor em `self._embeddings`, COM underscore (rag.py:708). O getattr
        devolvia None e o `liberar_vram` pulava o embedding CALADO — desde que a
        função nasceu, inclusive no caminho do OCR para o qual ela foi escrita.
        O sintoma era invisível: `liberados` vinha sem "embeddings" e ninguém
        comparava com o que deveria estar lá.

        O nome público (`embeddings`, sem underscore) fica como primeira opção
        porque os fakes duck-typed dos testes usam essa forma."""
        loja = getattr(self, "vectorstore", None)
        if loja is None:
            return None
        return getattr(loja, "embeddings", None) or getattr(loja, "_embeddings", None)

    async def liberar_vram(self) -> Set[str]:
        """Descarrega TUDO do projeto que ocupa VRAM — Fase 3 (OCR).

        Por que o LLM não basta: o OCR sobe ~3 GB num processo/venv SEPARADO, e do
        lado do app ainda ficariam o XTTS (~2 GB), o Whisper em cuda (~1,5 GB) e o
        e5 (~0,5 GB). Numa 3080 de 10 GB, misturar os dois lados é o caminho para
        OOM (ou pior: o device-side assert de 2026-07-23). Aqui a GPU fica limpa.

        Devolve o conjunto do que foi descarregado, para `restaurar_vram` religar
        exatamente isso — nenhum destes serviços auto-carrega (o STT devolveria ""
        e o TTS ficaria mudo, ambos em silêncio). Duck-typed e fail-soft: serviço
        sem `unload` (fake de teste, engine Piper que é CPU) é simplesmente pulado.
        """
        liberados: Set[str] = set()
        llama = getattr(self, "llama", None)
        if llama is not None and getattr(llama, "ready", False):
            await llama.unload()
            liberados.add("llama")     # religa sozinho (ensure_loaded) — não restauramos
        for nome, alvo in (("stt", getattr(self, "stt", None)),
                           ("tts", getattr(self, "tts", None)),
                           ("embeddings", self._provedor_de_embeddings())):
            if alvo is None or not hasattr(alvo, "unload") or not getattr(alvo, "ready", True):
                continue
            try:
                await asyncio.to_thread(alvo.unload)
                liberados.add(nome)
            except Exception as exc:
                telemetry.error("VRAM", f"Falha ao descarregar {nome}", exc)
        # ACUMULA, não sobrescreve. ⚠ Este `|=` era um `=`, e o defeito só aparecia
        # no SEGUNDO descarregamento seguido — que o modo economia tornou comum
        # (2026-08-02, visto no celular): o watcher dormiu e guardou
        # {embeddings, stt}; algo religou só o embedding; o botão "Modo economia"
        # descarregou de novo e a lista virou {embeddings}, PERDENDO o stt. No
        # religar, `restaurar_vram` trouxe só o embedding e o Whisper ficou fora
        # para sempre — sem erro, sem log, com a voz simplesmente não funcionando.
        # É a degradação silenciosa que o docstring acima existe para evitar.
        # Serviço que voltou por outro caminho é religado à toa; `load` é
        # idempotente, e pagar um load a mais é infinitamente melhor que perder um.
        self._vram_liberada |= liberados - {"llama"}
        if liberados:
            telemetry.track("VRAM", f"VRAM liberada para o OCR: {', '.join(sorted(liberados))}")
        return liberados

    async def restaurar_vram(self) -> None:
        """Religa o que `liberar_vram` descarregou (menos o LLM, que já é lazy).
        Sem isto a voz voltaria MUDA depois de um OCR — o pior tipo de defeito:
        silencioso. Cada `load` é síncrono, então vai por to_thread."""
        pendentes, self._vram_liberada = set(self._vram_liberada), set()
        for nome in sorted(pendentes):
            alvo = (self._provedor_de_embeddings()
                    if nome == "embeddings" else getattr(self, nome, None))
            if alvo is None:
                continue
            try:
                await asyncio.to_thread(alvo.load)
            except Exception as exc:
                telemetry.error("VRAM", f"Falha ao restaurar {nome} após o OCR", exc)
        if pendentes:
            telemetry.track("VRAM", f"Serviços restaurados: {', '.join(sorted(pendentes))}")

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
        task.add_done_callback(self._task_done)
        return task

    def marcar_vault_sujo(self) -> None:
        """#38: uma escrita no vault (nota/lista/captura) ficou pendente de reindexação.
        Em vez de sincronizar na hora — o que disputaria a GPU serializada com o STT/decode
        do próximo turno e o congelava por ~46s — só marcamos. O `EtlProcessor.run_idle`
        faz o `sync` no idle pós-conversa, com a GPU livre (ver `vault_pendente_sync`)."""
        self.vault_pendente_sync = True

    def _task_done(self, task: "asyncio.Task") -> None:
        """Remove do set E torna a exceção VISÍVEL (painel 2026-07): sem isto, uma
        falha em trabalho de fundo (ETL, sync do índice, entrega de lembrete, reload
        do LLM) morria como aviso de GC do asyncio — violando o "erros nunca são
        engolidos" do projeto. CancelledError é fluxo normal (barge-in/shutdown),
        não erro. Import local: state é importado por todo mundo e o telemetry não
        pode virar dependência de topo aqui só por causa do callback."""
        self._bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            from mente_digital.telemetry import telemetry

            telemetry.error("TASK", f"Task de fundo falhou: {task.get_coro()!r}", exc)

    async def drenar_tasks(self, timeout: float = 10.0) -> None:
        """Espera as tasks de fundo terminarem (até `timeout`) e cancela as
        retardatárias — chamado no shutdown ANTES de derrubar a GPU, para um ETL
        quase pronto CONCLUIR a atomização em vez de ser rasgado com escrita em voo
        (por isso espera-PRIMEIRO-cancela-depois, não o contrário)."""
        pendentes = {t for t in self._bg_tasks if not t.done()}
        if not pendentes:
            return
        _prontas, restantes = await asyncio.wait(pendentes, timeout=timeout)
        for t in restantes:
            t.cancel()
        if restantes:
            await asyncio.gather(*restantes, return_exceptions=True)
