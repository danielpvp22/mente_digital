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
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import AsyncIterator, Optional, Set

from config import settings
from telemetry import telemetry

_SENTINEL = object()


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


class LlamaManager:
    def __init__(self) -> None:
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

    def _build_llama_kwargs(self) -> dict:
        """Monta os kwargs do construtor Llama a partir do settings.

        Puro/sem GPU (só monta um dict) — seguro chamar do event loop. Cada botão
        de tuning (§7) e o speculative decoding (§5) entram aqui, cada um guardado
        e logado: um valor inválido degrada para o default em vez de derrubar o load.
        """
        import llama_cpp

        kwargs: dict = dict(
            model_path=settings.caminho_modelo_llama,
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
                ggml_type = getattr(llama_cpp, f"GGML_TYPE_{kv.upper()}", None)
                if ggml_type is None:
                    telemetry.warn("VRAM", f"kv_cache_type={kv} desconhecido; usando f16.")
                else:
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
            telemetry.track("VRAM", "Ancorando Qwen 7B na GPU...")
            try:
                from llama_cpp import Llama

                kwargs = self._build_llama_kwargs()
                loop = asyncio.get_running_loop()
                self._model = await loop.run_in_executor(
                    self._gpu_executor,
                    lambda: Llama(**kwargs),
                )
                self._ready = True
                telemetry.track("VRAM", "Qwen 7B pronto na GPU.")
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
                    telemetry.track("VRAM", "Qwen 7B descarregado — VRAM liberada para o idle.")
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
    ) -> AsyncIterator[str]:
        """Gera tokens em tempo real sem bloquear o event loop.

        `preemptible=True` marca o decode como BAIXA PRIORIDADE: `preempt()` pode
        abortá-lo a qualquer token para liberar a GPU, e aí a stream levanta
        `InferenciaPreemptada`. Use só em trabalho de background (ETL) — nunca numa
        resposta ao usuário, que jamais deve ser interrompida por outra coisa.
        """
        # Religa sob demanda: se o idle descarregou o Qwen, a 1ª inferência o traz de
        # volta em vez de falhar. Idempotente e barato quando já está na GPU. Só o
        # decode PREEMPTÍVEL (ETL) não religa — se o modelo saiu, é porque o idle
        # acabou; ressuscitá-lo para trabalho de fundo anularia o unload.
        if self._model is None and not preemptible:
            await self.ensure_loaded()
        if self._model is None:
            if not preemptible:
                telemetry.warn("LLM", "Inferência solicitada sem modelo carregado.")
            return

        temp = settings.temperatura_resposta if temperature is None else temperature

        async with self._inference_lock:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            stop_event = threading.Event()

            def _worker() -> None:
                try:
                    for chunk in self._model.create_chat_completion(
                        messages=[
                            {"role": "system", "content": system_prompt},
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
                            loop.call_soon_threadsafe(queue.put_nowait, token)
                except Exception as exc:  # nunca engolir em silêncio
                    loop.call_soon_threadsafe(queue.put_nowait, _WorkerError(exc))
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

            # Registrado ANTES do submit: assim uma pergunta que chegue no instante
            # seguinte já encontra este decode e consegue abortá-lo.
            if preemptible:
                self._preemptiveis.add(stop_event)

            future: Future = self._gpu_executor.submit(_worker)
            try:
                while True:
                    item = await queue.get()
                    if item is _SENTINEL:
                        break
                    if isinstance(item, _WorkerError):
                        telemetry.error("LLM", "Erro no worker de inferência", item.exc)
                        break
                    yield item
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

    async def collect(self, prompt: str, **kwargs) -> str:
        """Atalho: consome a stream inteira e devolve o texto concatenado."""
        return "".join([tok async for tok in self.stream(prompt, **kwargs)])

    def shutdown(self) -> None:
        self._gpu_executor.shutdown(wait=False, cancel_futures=True)
