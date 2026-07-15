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
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import AsyncIterator, Optional

from config import settings
from telemetry import telemetry

_SENTINEL = object()


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

    @property
    def ready(self) -> bool:
        return self._ready

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

    async def load(self) -> None:
        """Ancora o Qwen na GPU. Degradação graciosa se falhar."""
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
                await self._warmup()
            except Exception as exc:
                self._ready = False
                telemetry.error("VRAM", "Falha ao carregar o LLM — modo degradado", exc)

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
    ) -> AsyncIterator[str]:
        """Gera tokens em tempo real sem bloquear o event loop."""
        if self._model is None:
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
            finally:
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
