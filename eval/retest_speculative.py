"""
Reteste do speculative decoding prompt-lookup (§5 do roadmap / consultoria TTFT #12)
— o GATE para religar MENTE_SPECULATIVE_ENABLED e para subir o llama-cpp-python.

Histórico (benchmark 2026-07, llama-cpp-python 0.3.34, RTX 3080):
  (a) prompt curto: MAIS LENTO com spec (93 vs 121 tok/s — overhead sem aceitação);
  (b) contexto longo (RAG): CRASH "could not broadcast array ... shape mismatch"
      no LlamaPromptLookupDecoding — justo no caso de uso principal.

Procedimento (NUNCA no llama-omni de produção — env isolada, como no teste do
ExLlamaV3):
    conda create -n spec-test python=3.10 -y
    conda activate spec-test
    pip install "llama-cpp-python==<VERSAO_CANDIDATA>"   # wheel CUDA adequada
    python eval/retest_speculative.py

Veredito PASS exige: (1) NENHUM crash no prompt longo com spec ligado, e
(2) tok/s(spec) >= tok/s(baseline) em pelo menos um regime. Só então: subir o pin
no requirements e ligar MENTE_SPECULATIVE_ENABLED=true no .env (e medir de novo
no app real via /api/metrics).
"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mente_digital.config import settings  # noqa: E402

PROMPT_CURTO = "Explique em poucas frases o que é uma lista em Python."
# ~2k tokens de contexto repetitivo — o regime onde o prompt-lookup DEVERIA brilhar
# (n-gramas do contexto reaparecem na resposta) e onde a 0.3.34 crashava.
_PARAGRAFO = (
    "O TensorRT acelera a inferência de redes neurais aplicando fusão de kernels, "
    "quantização e otimização de grafo. O YOLO se beneficia porque convoluções "
    "dominam o tempo de inferência e a fusão reduz lançamentos de kernel. "
)
PROMPT_LONGO = (
    "Contexto:\n" + _PARAGRAFO * 40 +
    "\n\nPergunta: segundo o contexto acima, por que o TensorRT acelera o YOLO?"
)


def _construir(spec: bool):
    from llama_cpp import Llama

    kwargs = dict(
        model_path=settings.caminho_modelo_llama,
        n_gpu_layers=-1,
        n_ctx=4096,
        flash_attn=True,
        verbose=False,
    )
    if spec:
        from llama_cpp.llama_speculative import LlamaPromptLookupDecoding

        kwargs["draft_model"] = LlamaPromptLookupDecoding(
            num_pred_tokens=settings.speculative_num_pred_tokens
        )
    return Llama(**kwargs)


def _medir(model, prompt: str, max_tokens: int = 160):
    """Devolve (tok/s de decode, n_tokens) ou levanta a exceção do crash."""
    t_first = None
    n = 0
    t0 = time.perf_counter()
    for chunk in model.create_chat_completion(
        messages=[
            {"role": "system", "content": "/no_think\nResponda direto."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.0,
        stream=True,
    ):
        if chunk["choices"][0]["delta"].get("content"):
            n += 1
            if t_first is None:
                t_first = time.perf_counter()
    fim = time.perf_counter()
    if n < 2 or t_first is None:
        return 0.0, n
    return (n - 1) / (fim - t_first), n


def main() -> int:
    resultados: dict[tuple, object] = {}
    for spec in (False, True):
        rotulo = "spec" if spec else "base"
        print(f"\n== {rotulo}: carregando o modelo ==")
        model = _construir(spec)
        try:
            for nome, prompt in (("curto", PROMPT_CURTO), ("longo", PROMPT_LONGO)):
                try:
                    toks, n = _medir(model, prompt)
                    resultados[(rotulo, nome)] = toks
                    print(f"  {nome:<6} {toks:7.1f} tok/s  ({n} tokens)")
                except Exception as exc:
                    resultados[(rotulo, nome)] = exc
                    print(f"  {nome:<6} CRASH: {exc}")
        finally:
            fechar = getattr(model, "close", None)
            if callable(fechar):
                fechar()
            del model
            gc.collect()

    crash_longo = isinstance(resultados.get(("spec", "longo")), Exception)
    ganho = any(
        isinstance(resultados.get(("spec", r)), float)
        and isinstance(resultados.get(("base", r)), float)
        and resultados[("spec", r)] >= resultados[("base", r)]
        for r in ("curto", "longo")
    )
    print("\n== veredito ==")
    if crash_longo:
        print("FAIL: o bug de shape em contexto longo AINDA existe — manter speculative OFF "
              "e o pin atual.")
        return 1
    if not ganho:
        print("FAIL: sem crash, mas o spec não empata/ganha em nenhum regime — "
              "religar não compensa (overhead líquido).")
        return 1
    print("PASS: sem crash no longo e com ganho em pelo menos um regime. Pode subir o pin "
          "e ligar MENTE_SPECULATIVE_ENABLED=true — e conferir no /api/metrics real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
