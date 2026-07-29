"""
Núcleo de inferência (GPU / RTX 3080).

HARDENING DO inference_lock
---------------------------
No monólito, o barge-in cancelava o gerador async e liberava o `inference_lock`
IMEDIATAMENTE — mas a daemon thread continuava decodificando na GPU. Uma nova
inferência pegava o lock e começava enquanto a antiga ainda rodava: exatamente a
concorrência de VRAM que o lock deveria impedir.

Aqui a garantia passa a ser ESTRUTURAL, não cooperativa:

1. Um ThreadPoolExecutor de UMA thread ("gpu-infer"). Como só existe uma thread,
   dois `create_chat_completion` NUNCA se sobrepõem — o próximo job só começa
   quando o anterior retorna.
2. Um `stop_event` por requisição. No cancelamento (barge-in), ele é setado e o
   loop de decode quebra no próximo token (~1 token de latência), liberando a
   thread para o próximo job.
3. O `asyncio.Lock` continua existindo para preservar o contrato "uma stream
   lógica por vez" da arquitetura original, e o `finally` só solta a thread depois
   que o worker realmente terminou.

PREEMPÇÃO (prioridade da inferência interativa)
-----------------------------------------------
A serialização acima tem um preço: quem pega o lock o segura até o decode acabar.
O ETL idle sintetiza com `max_tokens_sintese=1600` — ~13s a 120 tok/s. Uma pergunta
que chegasse no meio esperava TUDO isso antes do primeiro token, porque o
`interactive_idle` do AppContext só evita COMEÇAR o próximo documento; ele não
interrompe o que já está decodificando. O pilar "o ETL cede a GPU para a inferência
interativa" era, na prática, falso.

A correção: um decode pode se declarar `preemptible=True`. O `stop_event` dele entra
num registro, e `preempt()` (chamado pelo pipeline interativo) o seta — o loop do
worker já checa esse evento a cada token, então o decode morre em ~1 token e solta
o lock. A stream preemptada levanta `InferenciaPreemptada`, o que OBRIGA o chamador
a decidir o que fazer com o trabalho perdido (o ETL devolve o item pra fila; nada
se perde em silêncio).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, AsyncIterator, Optional, Set

if TYPE_CHECKING:
    from mente_digital.telemetry import LatencyTracker

from mente_digital import prompts
from mente_digital.config import settings
from mente_digital.telemetry import telemetry

_SENTINEL = object()


def montar_system(system_prompt: str) -> str:
    """Composição FINAL do system prompt — ponto único: todo decode passa aqui.

    Ordem fixa: "/no_think" primeiro (diretiva do Qwen3), depois o preâmbulo comum
    (consultoria #10 — prefixo idêntico entre chamadas p/ reuso do KV do prefixo no
    llama.cpp), depois a tarefa. Como as flags não mudam durante o processo, o começo
    do prompt fica byte-idêntico entre extrator/roteador/resposta — a condição do
    reuso. Pura, para o A/B do bench testar a composição sem carregar modelo."""
    base = (
        f"{prompts.PREAMBULO_COMUM}\n{system_prompt}"
        if settings.prompt_preambulo_comum
        else system_prompt
    )
    return f"/no_think\n{base}" if settings.llm_no_think else base


def preparar_offline(caminho_modelo: str) -> str:
    """Alinha as flags de `<think>` ao modelo OFFLINE e devolve o caminho dele.

    HIGIENE e COMPORTAMENTO são coisas separadas, e confundi-las custou caro:

    - `llm_strip_think` (higiene) é ligado SEMPRE que há modelo offline. O `.env`
      o desliga por causa do modelo do SERVIDOR, que não raciocina; com um Qwen3
      que raciocina, o desligado despeja o bloco `<think>…</think>` DENTRO do
      átomo — há 8 notas assim na base, resquício de uma passada anterior.
    - `llm_no_think` (comportamento) é decisão de QUALIDADE, não de higiene, e
      fica com o dono em `atomizacao_pensar`. Pensar pode condensar melhor a
      página; e pensar consome o MESMO orçamento de saída que os átomos, que é a
      causa medida dos átomos truncados. Só um A/B decide — ver
      `eval/ab_atomizacao_think.py`.

    As flags valem por processo, e o processo offline serve UM modelo só — então
    alinhar aqui é seguro e não toca no servidor, que roda noutro processo.
    """
    if not caminho_modelo:
        return caminho_modelo
    settings.llm_strip_think = True
    settings.llm_no_think = not settings.atomizacao_pensar
    telemetry.track(
        "LLM",
        f"Modelo offline: strip de <think> LIGADO; raciocínio "
        f"{'LIGADO' if settings.atomizacao_pensar else 'desligado'}.")
    return caminho_modelo


def _ggml_type(kv: str) -> Optional[int]:
    """Constante `GGML_TYPE_*` do llama_cpp para o KV-cache, ou None (com aviso).

    O import mora AQUI, e não no topo de `_build_llama_kwargs`, porque só este ramo
    precisa dele: montar os kwargs tem de funcionar sem llama-cpp-python instalado
    — é o que o CI faz (a lib compila por minutos e fica fora de propósito) e é o
    que a docstring de lá promete ao chamar a função de "pura". Cada motivo de
    desistir tem seu próprio aviso: lib ausente e valor desconhecido são coisas
    diferentes, e um log que troca uma pela outra manda o diagnóstico para o lado
    errado.
    """
    try:
        import llama_cpp
    except ImportError as exc:
        telemetry.warn("VRAM", f"kv_cache_type={kv} exige llama-cpp-python ({exc}); usando f16.")
        return None

    tipo = getattr(llama_cpp, f"GGML_TYPE_{kv.upper()}", None)
    if tipo is None:
        telemetry.warn("VRAM", f"kv_cache_type={kv} desconhecido; usando f16.")
    return tipo


class InferenciaPreemptada(RuntimeError):
    """O decode foi abortado para ceder a GPU à inferência interativa.

    Não é erro: é o mecanismo funcionando. Quem pediu uma stream `preemptible`
    precisa tratar isto — o texto gerado até aqui está incompleto e deve ser
    descartado, e o trabalho, reagendado.
    """


class _WorkerError:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_ABRE_THINK = "<think>"
_FECHA_THINK = "</think>"


class _FiltroThink:
    """Remove o bloco `<think>…</think>` do INÍCIO do stream (Qwen3). Puro/testável.

    O Qwen3 abre toda resposta com esse bloco — VAZIO quando o prompt traz "/no_think",
    mas ainda presente. Sem remover, a tag vaza para o `SentenceChunker` e o TTS acaba
    **falando a marcação**. É o mesmo padrão do guard anti-sentinela: segura enquanto o
    buffer ainda PODE ser o começo de `<think>`, e decide assim que souber.

    Só age no PREFIXO: depois que o bloco fecha (ou que o 1º texto prova que não há
    bloco), vira passthrough e não custa mais nada por token.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._estado = "indeciso"          # indeciso -> dentro -> limpando -> passthrough

    def push(self, token: str) -> str:
        """Recebe um token e devolve o que pode ser emitido AGORA (pode ser '')."""
        if self._estado == "passthrough":
            return token
        if self._estado == "limpando":
            # O bloco fechou colado no fim do buffer: pula o espaço que vier DEPOIS,
            # senão a resposta começaria com as quebras de linha do <think>.
            limpo = token.lstrip()
            if not limpo:
                return ""
            self._estado = "passthrough"
            return limpo
        self._buf += token
        if self._estado == "indeciso":
            visto = self._buf.lstrip()
            if not visto:
                return ""                   # só espaço em branco até aqui
            if _ABRE_THINK.startswith(visto) and len(visto) < len(_ABRE_THINK):
                return ""                   # ainda pode virar "<think>": segura
            if visto.startswith(_ABRE_THINK):
                self._estado = "dentro"
            else:
                self._estado = "passthrough"        # não é bloco: solta tudo
                out, self._buf = self._buf, ""
                return out
        if self._estado == "dentro":
            fim = self._buf.find(_FECHA_THINK)
            if fim == -1:
                return ""                   # ainda dentro do bloco
            resto = self._buf[fim + len(_FECHA_THINK):].lstrip()
            self._buf = ""
            if resto:
                self._estado = "passthrough"
                return resto
            self._estado = "limpando"       # fechou colado: limpa o espaço seguinte
            return ""
        return ""

    def flush(self) -> str:
        """Fim do stream. Se ficou INDECISO (resposta curtíssima que era prefixo de
        '<think>'), devolve o retido — nunca engolir texto do usuário."""
        if self._estado == "indeciso":
            out, self._buf = self._buf, ""
            self._estado = "passthrough"
            return out
        return ""


class LlamaManager:
    def __init__(self, caminho_modelo: str = "") -> None:
        # Override do .gguf para as passadas OFFLINE (atomização, tradução,
        # varredura): com o servidor fechado a VRAM inteira está livre e cabe um
        # modelo maior, que lê melhor uma página e destila ideias completas. Vazio
        # = o modelo do servidor, escolhido por latência. Ver
        # settings.caminho_modelo_atomizacao.
        self._caminho_modelo = caminho_modelo
        self._model = None
        self._load_lock = asyncio.Lock()        # protege o lazy-load (era llm_manager_lock)
        self._inference_lock = asyncio.Lock()   # serializa streams (era inference_lock)
        # UMA thread => zero overlap de decode na GPU, mesmo durante cancelamentos.
        self._gpu_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-infer")
        self._ready = False
        # stop_events dos decodes de BAIXA prioridade em curso (ETL). Só o event loop
        # mexe aqui, então um set puro basta — sem lock.
        self._preemptiveis: Set[threading.Event] = set()

    @property
    def ready(self) -> bool:
        return self._ready

    def preempt(self) -> int:
        """Aborta os decodes preemptíveis em curso. Devolve quantos foram atingidos.

        Barato e idempotente: setar um Event já setado é no-op, e sem ETL rodando
        isto custa uma iteração sobre um set vazio — então o pipeline interativo pode
        chamar sempre, sem pagar nada no caminho comum.
        """
        atingidos = list(self._preemptiveis)
        for ev in atingidos:
            ev.set()
        if atingidos:
            telemetry.track("LLM", f"Preempção: {len(atingidos)} decode(s) de ETL cedendo a GPU.")
        return len(atingidos)

    @staticmethod
    def _reset_vram_peak() -> None:
        """Zera o contador de pico da VRAM antes de um decode instrumentado. TARDIO e
        best-effort: o módulo NÃO pode passar a exigir torch no import (CI leve). Sem
        CUDA/torch, no-op silencioso — medição jamais derruba o decode."""
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
        except Exception:  # medição best-effort: torch ausente/erro CUDA é ignorado
            pass

    @staticmethod
    def _ler_vram_peak() -> "Optional[float]":
        """Pico de VRAM alocada (MB) desde o último reset, ou None sem CUDA/torch."""
        try:
            import torch

            if torch.cuda.is_available():
                return torch.cuda.max_memory_allocated() / 1e6
        except Exception:  # medição best-effort: nunca propaga
            pass
        return None

    def _build_llama_kwargs(self) -> dict:
        """Monta os kwargs do construtor Llama a partir do settings.

        Puro/sem GPU (só monta um dict) — seguro chamar do event loop. Cada botão
        de tuning (§7) e o speculative decoding (§5) entram aqui, cada um guardado
        e logado: um valor inválido degrada para o default em vez de derrubar o load.

        O `import llama_cpp` vive DENTRO do ramo que precisa dele (as constantes
        GGML_TYPE_*): no topo, ele contradizia o "puro" do parágrafo acima e
        reprovava o CI, que não instala llama-cpp-python de propósito (compila por
        minutos). O caminho default agora monta o dict sem importar nada.
        """
        kwargs: dict = dict(
            model_path=self._caminho_modelo or settings.caminho_modelo_llama,
            n_gpu_layers=settings.n_gpu_layers,
            n_ctx=settings.n_ctx,
            n_batch=settings.n_batch,
            n_ubatch=settings.n_ubatch,
            flash_attn=settings.flash_attn,
            verbose=False,
        )

        # KV-cache quantizado (§7): metade da VRAM de KV a custo ínfimo de qualidade.
        # llama.cpp EXIGE flash_attn para o cache V quantizado — guardamos isso para
        # não cair num erro obscuro de runtime lá na frente.
        kv = settings.kv_cache_type.strip().lower()
        if kv and kv != "f16":
            if not settings.flash_attn:
                telemetry.warn(
                    "VRAM", f"kv_cache_type={kv} exige flash_attn=True; usando f16."
                )
            else:
                ggml_type = _ggml_type(kv)
                if ggml_type is not None:
                    kwargs["type_k"] = ggml_type
                    kwargs["type_v"] = ggml_type
                    telemetry.track("VRAM", f"KV-cache quantizado em {kv}.")

        # Speculative decoding (§5): prompt-lookup — sem modelo/VRAM extra. Acha
        # n-gramas no contexto e os propõe como rascunho; lossless, ideal para RAG.
        if settings.speculative_enabled:
            try:
                from llama_cpp.llama_speculative import LlamaPromptLookupDecoding

                kwargs["draft_model"] = LlamaPromptLookupDecoding(
                    num_pred_tokens=settings.speculative_num_pred_tokens
                )
                telemetry.track("LLM", "Speculative decoding (prompt-lookup) ativo.")
            except Exception as exc:  # dependência ausente/versão antiga -> segue sem
                telemetry.warn("LLM", f"Speculative decoding indisponível: {exc}")

        return kwargs

    async def load(self, warmup: bool = True) -> None:
        """Ancora o Qwen na GPU. Degradação graciosa se falhar.

        `warmup=False` no religar sob demanda: a própria requisição que disparou o
        reload já aquece o modelo, então pagar um decode extra de warm-up seria dobrar
        a latência do 1º token pós-idle sem ganho.
        """
        async with self._load_lock:
            if self._model is not None:
                return
            # Loga o ARQUIVO, não um nome fixo: o modelo é trocável por .env e um rótulo
            # cravado ("Qwen 7B") vira mentira no dia da troca — e some a resposta de
            # "qual modelo está rodando?", que é a 1ª pergunta em qualquer diagnóstico.
            # A MESMA fonte dos kwargs, não o settings direto: com o override
            # offline (modelo grande), ler o settings aqui faria o log jurar que
            # subiu o modelo do servidor — a mentira que o comentário acima
            # promete evitar, só que por outro caminho.
            nome = os.path.basename(self._caminho_modelo or settings.caminho_modelo_llama)
            telemetry.track("VRAM", f"Ancorando {nome} na GPU...")
            try:
                from llama_cpp import Llama

                kwargs = self._build_llama_kwargs()
                loop = asyncio.get_running_loop()
                self._model = await loop.run_in_executor(
                    self._gpu_executor,
                    lambda: Llama(**kwargs),
                )
                self._ready = True
                telemetry.track("VRAM", f"{nome} pronto na GPU.")
                if warmup:
                    await self._warmup()
            except Exception as exc:
                self._ready = False
                telemetry.error("VRAM", "Falha ao carregar o LLM — modo degradado", exc)

    async def ensure_loaded(self) -> None:
        """Religa o modelo se ele foi descarregado (unload no idle). Idempotente e
        barato quando já está na GPU (só um teste de atributo). Sem warm-up: a
        requisição do chamador é o próprio aquecimento."""
        if self._model is None:
            await self.load(warmup=False)

    async def unload(self) -> None:
        """Descarrega o Qwen da GPU, liberando a VRAM para OUTROS trabalhos fora do app.

        Chamado ao fim da fase de idle (depois que ETL + pesquisa proativa terminaram —
        eles PRECISAM do modelo). Religa sob demanda (ensure_loaded) na próxima mensagem,
        novo chat, abertura do live, ou necessidade do pipeline.

        Segurança do decode em curso: o fechamento roda no MESMO `gpu_executor` de uma
        thread só, então é enfileirado ATRÁS de qualquer decode em andamento — nunca
        puxa o modelo de baixo de uma inferência. E `_inference_lock` garante que nenhuma
        stream nova comece durante o descarregamento. Só os embeddings (MiniLM, ~0.5GB)
        ficam: o idle e o dedup dependem deles, e o custo em VRAM é ínfimo.
        """
        async with self._load_lock:
            if self._model is None:
                return
            async with self._inference_lock:
                modelo = self._model
                self._model = None
                self._ready = False

                def _fechar() -> None:
                    fechar = getattr(modelo, "close", None)
                    if callable(fechar):
                        fechar()

                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(self._gpu_executor, _fechar)
                    import gc

                    gc.collect()
                    telemetry.track("VRAM", "LLM descarregado — VRAM liberada para o idle.")
                except Exception as exc:
                    telemetry.error("VRAM", "Falha ao descarregar o LLM", exc)

    async def _warmup(self) -> None:
        """Prime o modelo para a 1ª resposta real não pagar o cold-start."""
        try:
            async for _ in self.stream(
                "ok", max_tokens=1, system_prompt="responda 'ok'", temperature=0.0
            ):
                pass
            telemetry.track("VRAM", "Warm-up concluído.")
        except Exception as exc:
            telemetry.warn("VRAM", f"Warm-up ignorado: {exc}")

    async def stream(
        self,
        prompt: str,
        *,
        max_tokens: int = 700,
        system_prompt: str = "Você é um assistente IA lógico e direto.",
        temperature: Optional[float] = None,
        preemptible: bool = False,
        tracker: "Optional[LatencyTracker]" = None,
    ) -> AsyncIterator[str]:
        """Gera tokens em tempo real sem bloquear o event loop.

        `preemptible=True` marca o decode como BAIXA PRIORIDADE: `preempt()` pode
        abortá-lo a qualquer token para liberar a GPU, e aí a stream levanta
        `InferenciaPreemptada`. Use só em trabalho de background (ETL) — nunca numa
        resposta ao usuário, que jamais deve ser interrompida por outra coisa.

        `tracker` (opcional) recebe a INSTRUMENTAÇÃO produtor-side best-effort:
        lock_wait/reload_frio/prefill (event loop) e decode_tok_s_gpu/vram_peak (na
        thread do worker). None (default) = NADA muda — os fakes de teste e os decodes
        de FUNDO (ETL/collect) não passam tracker, então seguem intactos. É o número de
        tok/s imune ao event loop/TTS que a métrica atual (lado consumidor) confunde.
        """
        # Religa sob demanda: se o idle descarregou o Qwen, a 1ª inferência o traz de
        # volta em vez de falhar. Idempotente e barato quando já está na GPU. Só o
        # decode PREEMPTÍVEL (ETL) não religa — se o modelo saiu, é porque o idle
        # acabou; ressuscitá-lo para trabalho de fundo anularia o unload.
        # reload_frio: separa o TTFT frio (pagou o reload pós-idle) do quente. Default 0;
        # vira 1 só se ESTE stream de fato religou o modelo (era None e voltou).
        if tracker is not None:
            tracker.reload_frio = 0
        if self._model is None and not preemptible:
            await self.ensure_loaded()
            if tracker is not None and self._model is not None:
                tracker.reload_frio = 1
                tracker.mark("reload")
        if self._model is None:
            if not preemptible:
                telemetry.warn("LLM", "Inferência solicitada sem modelo carregado.")
            return

        temp = settings.temperatura_resposta if temperature is None else temperature
        # Composição única do system (montar_system): "/no_think" do Qwen3 (o bloco
        # <think> ainda SAI, vazio — quem o remove é o _FiltroThink abaixo; no-op em
        # modelos que ignoram a diretiva) + preâmbulo comum (#10) quando ligado.
        sys_prompt = montar_system(system_prompt)

        # lock_wait_ms: quanto o decode esperou pela GPU já ocupada (o outro suspeito
        # do TTFT caixa-preta, junto do reload frio e do prefill). Monotônico, medido
        # ANTES/DEPOIS de adquirir o _inference_lock — best-effort (só com tracker).
        _t_call = time.monotonic() if tracker is not None else 0.0
        async with self._inference_lock:
            if tracker is not None:
                tracker.lock_wait_ms = (time.monotonic() - _t_call) * 1000
                tracker.mark("lock")
                if settings.trace_enabled:
                    # Só em modo trace: reset_peak_memory_stats() muta o contador GLOBAL de
                    # pico do torch — que o detector de vazamento e o _probe_vram também
                    # leem. Fora de uma sessão de medição, esse efeito colateral em TODO
                    # decode não paga por si (o vram_peak_mb fica None, como sem CUDA).
                    self._reset_vram_peak()   # zera o pico p/ medir só ESTE decode
            _t_lock = time.monotonic()
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            stop_event = threading.Event()

            def _worker() -> None:
                # decode_tok_s_gpu: cronometrado DENTRO da thread do worker (do 1º ao
                # último chunk gerado), imune ao event loop e ao TTS — o número que a
                # métrica atual (timestamps de envio, lado consumidor) confunde com
                # decode+síntese+contenção. Agrega em locais e ATRIBUI só o escalar
                # final no tracker (float é atômico no CPython — sem estrutura mutável
                # compartilhada com a thread).
                _t_first = None
                _t_last = None
                _n = 0
                try:
                    for chunk in self._model.create_chat_completion(
                        messages=[
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=max_tokens,
                        temperature=temp,
                        stream=True,
                    ):
                        if stop_event.is_set():
                            break
                        token = chunk["choices"][0]["delta"].get("content", "")
                        if token:
                            if tracker is not None:
                                _agora = time.monotonic()
                                if _t_first is None:
                                    _t_first = _agora
                                _t_last = _agora
                                _n += 1
                            loop.call_soon_threadsafe(queue.put_nowait, token)
                except Exception as exc:  # nunca engolir em silêncio
                    loop.call_soon_threadsafe(queue.put_nowait, _WorkerError(exc))
                finally:
                    if (tracker is not None and _n > 1
                            and _t_first is not None and _t_last is not None):
                        _dur = _t_last - _t_first
                        if _dur > 0:
                            tracker.decode_tok_s_gpu = round((_n - 1) / _dur, 1)
                    loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

            # Registrado ANTES do submit: assim uma pergunta que chegue no instante
            # seguinte já encontra este decode e consegue abortá-lo.
            if preemptible:
                self._preemptiveis.add(stop_event)

            future: Future = self._gpu_executor.submit(_worker)
            filtro = _FiltroThink() if settings.llm_strip_think else None
            _prefill_medido = False
            try:
                while True:
                    item = await queue.get()
                    if item is _SENTINEL:
                        break
                    if isinstance(item, _WorkerError):
                        telemetry.error("LLM", "Erro no worker de inferência", item.exc)
                        break
                    # prefill_ms: do lock ao 1º chunk produzido pelo worker (engolir o
                    # prompt). Repartir isto do lock_wait/reload é o que abre a caixa-preta
                    # do TTFT de 7-9s na rota 'banco'. Medido no 1º item real (pré-filtro).
                    if tracker is not None and not _prefill_medido:
                        tracker.prefill_ms = (time.monotonic() - _t_lock) * 1000
                        tracker.mark("prefill")
                        _prefill_medido = True
                    if filtro is not None:
                        item = filtro.push(item)
                        if not item:
                            continue     # ainda decidindo/dentro do bloco <think>
                    yield item
                if filtro is not None:
                    resto = filtro.flush()   # acabou indeciso: não engolir o texto
                    if resto:
                        yield resto
                # Aqui o stop_event só está setado se `preempt()` o setou (o finally
                # abaixo ainda não rodou) -> o decode foi cortado, não terminou.
                if preemptible and stop_event.is_set():
                    raise InferenciaPreemptada("decode de background cedeu a GPU")
            finally:
                self._preemptiveis.discard(stop_event)
                # Cancel path (barge-in): manda o decode parar e só solta o lock
                # depois que a thread da GPU está livre — sem overlap de VRAM.
                stop_event.set()
                try:
                    await asyncio.to_thread(future.result)
                except Exception:
                    pass
                # vram_peak_mb: pico alocado neste decode (a suspeita de oversubscrição
                # ~9-10/10GB e spill WDDM). Lido AQUI, com a thread da GPU já livre.
                # Pareado com o reset acima: só em modo trace (senão fica None).
                if tracker is not None and settings.trace_enabled:
                    tracker.vram_peak_mb = self._ler_vram_peak()

    async def collect(self, prompt: str, **kwargs) -> str:
        """Atalho: consome a stream inteira e devolve o texto concatenado."""
        return "".join([tok async for tok in self.stream(prompt, **kwargs)])

    def shutdown(self) -> None:
        self._gpu_executor.shutdown(wait=False, cancel_futures=True)
