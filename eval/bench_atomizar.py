"""Banca de throughput da ATOMIZACAO — mede as tres alavancas de uma vez.

Pergunta que ela responde (2026-07-25): a atomizacao roda a ~78 atomos/min com a
GPU em 88-90% de uso, MAS com 4,9 GB de 10 usados e um modelo de 4B. Os 88% dizem
"tem kernel rodando", nao "a placa esta saturada" — decode de UMA sequencia e
limitado por banda de memoria: rele os pesos inteiros para produzir um token. As
alavancas:

  1. BATCHING  — N instancias concorrentes dividem o custo de ler os pesos. O
     worker de OCR deste projeto ja mediu 2,02x indo de sequencial para 4 slots.
  2. N_BATCH   — prefill mais gordo usando a VRAM ociosa (lote de 6000 chars).
  3. SPECULATIVE — prompt-lookup: o atomo COPIA trechos que ja estao no prompt,
     entao o n-grama acerta muito. Desligado hoje por um crash de shape.

POR QUE ISTO NAO TOCA NO PROJETO: a banca instancia `llama_cpp.Llama` DIRETO, com
os mesmos kwargs do `LlamaManager`, e nao importa AppContext, VectorStore nem
vault. Nada e escrito em disco — nenhum atomo, nenhum Chroma. Medir o TETO da GPU
nao pode ter o custo de arriscar a base.

Uso (a GPU tem de estar livre — a banca recusa se houver fila atomizando):
    python eval/bench_atomizar.py                     # varredura padrao
    python eval/bench_atomizar.py --workers 1,2,3
    python eval/bench_atomizar.py --speculative
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import prompts  # noqa: E402
from mente_digital.config import settings  # noqa: E402

MAX_TOKENS = 320          # atomo tipico cabe folgado; fixo p/ comparar variantes


def _trecho_real() -> Tuple[str, str, str]:
    """Um pedaco REAL de capitulo — prompt sintetico mediria outra coisa."""
    for pasta in ("pendentes", "processados"):
        for f in sorted((Path(settings.dir_ingestao) / pasta).glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            texto = (d.get("texto") or "").strip()
            if len(texto) >= settings.ingestao_lote_chars:
                return (d.get("livro", "?"), d.get("titulo_cap", "?"),
                        texto[:settings.ingestao_lote_chars])
    raise SystemExit("Nenhum capitulo encontrado em dados/ingestao para servir de amostra.")


def _kwargs(n_batch: int, speculative: bool) -> dict:
    """Espelha `LlamaManager._build_llama_kwargs` — a banca so vale se medir a
    MESMA configuracao que roda de verdade."""
    kw = dict(
        model_path=settings.caminho_modelo_llama,
        n_gpu_layers=settings.n_gpu_layers,
        n_ctx=settings.n_ctx,
        n_batch=n_batch,
        n_ubatch=settings.n_ubatch,
        flash_attn=settings.flash_attn,
        verbose=False,
    )
    if speculative:
        from llama_cpp.llama_speculative import LlamaPromptLookupDecoding

        kw["draft_model"] = LlamaPromptLookupDecoding(
            num_pred_tokens=settings.speculative_num_pred_tokens)
    return kw


def _uma_rodada(llama, prompt: str, sistema: str) -> Tuple[int, float]:
    """Um decode. Devolve (tokens gerados, segundos)."""
    t0 = time.perf_counter()
    saida = llama.create_chat_completion(
        messages=[{"role": "system", "content": sistema},
                  {"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS, temperature=0.0,
    )
    dt = time.perf_counter() - t0
    return int(saida["usage"]["completion_tokens"]), dt


def _variante(workers: int, n_batch: int, speculative: bool, repeticoes: int,
              prompt: str, sistema: str) -> Optional[dict]:
    """Carrega `workers` instancias, dispara `repeticoes` decodes em cada uma em
    paralelo e mede o agregado. Instancias separadas (e nao slots de um servidor)
    porque e o que o `llama-cpp-python` oferece em processo."""
    from llama_cpp import Llama

    modelos = []
    try:
        for _ in range(workers):
            modelos.append(Llama(**_kwargs(n_batch, speculative)))
    except Exception as exc:                       # VRAM insuficiente, tipicamente
        for m in modelos:
            m.close()
        print(f"  workers={workers} n_batch={n_batch} spec={speculative}: FALHOU ({exc})")
        return None

    resultados: List[Tuple[int, float]] = []
    trava = threading.Lock()

    def _trabalhar(m):
        for _ in range(repeticoes):
            try:
                r = _uma_rodada(m, prompt, sistema)
            except Exception as exc:
                with trava:
                    resultados.append((0, float("nan")))
                print(f"    decode falhou: {exc}")
                continue
            with trava:
                resultados.append(r)

    # Aquece: a 1a chamada paga alocacao de KV e nao representa o regime.
    _uma_rodada(modelos[0], prompt, sistema)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=_trabalhar, args=(m,)) for m in modelos]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    parede = time.perf_counter() - t0
    for m in modelos:
        m.close()

    bons = [(tk, dt) for tk, dt in resultados if tk and dt == dt]
    if not bons:
        return None
    tokens = sum(tk for tk, _ in bons)
    return {
        "workers": workers, "n_batch": n_batch, "spec": speculative,
        "tok_s_agregado": tokens / parede,
        "tok_s_por_worker": statistics.median(tk / dt for tk, dt in bons),
        "parede": parede, "decodes": len(bons), "tokens": tokens,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", default="1,2,3", help="instancias concorrentes")
    ap.add_argument("--n-batch", default=f"{settings.n_batch},2048",
                    help="valores de n_batch a varrer")
    ap.add_argument("--speculative", action="store_true",
                    help="mede TAMBEM com prompt-lookup ligado")
    ap.add_argument("--repeticoes", type=int, default=3, help="decodes por worker")
    ap.add_argument("--forcar", action="store_true",
                    help="roda mesmo com fila de atomizacao pendente")
    args = ap.parse_args()

    pend = list((Path(settings.dir_ingestao) / "pendentes").glob("*.json"))
    if pend and not args.forcar:
        print(f"Ha {len(pend)} capitulo(s) na fila — a atomizacao esta usando a GPU e a\n"
              "medicao sairia contaminada pela disputa (mede ruido, nao teto).\n"
              "Espere a fila zerar, ou use --forcar se souber o que esta fazendo.")
        return 3

    livro, cap, trecho = _trecho_real()
    prompt = prompts.prompt_atomizar_livro(livro, cap, trecho)
    print(f"Amostra: '{livro}' / '{cap}' — {len(trecho)} chars de prompt")
    print(f"Modelo: {Path(settings.caminho_modelo_llama).name} | n_ctx={settings.n_ctx}\n")

    combos = [(w, nb, False)
              for nb in (int(x) for x in args.n_batch.split(","))
              for w in (int(x) for x in args.workers.split(","))]
    if args.speculative:
        combos += [(w, settings.n_batch, True)
                   for w in (int(x) for x in args.workers.split(","))]

    linhas = []
    for w, nb, spec in combos:
        print(f"medindo workers={w} n_batch={nb} spec={spec}…", flush=True)
        r = _variante(w, nb, spec, args.repeticoes, prompt, prompts.SYS_SINTESE)
        if r:
            linhas.append(r)
            print(f"  -> {r['tok_s_agregado']:.1f} tok/s agregado "
                  f"({r['tok_s_por_worker']:.1f} por worker)")

    if not linhas:
        print("\nNenhuma variante completou.")
        return 1
    base = next((x for x in linhas
                 if x["workers"] == 1 and not x["spec"]), linhas[0])
    print(f"\n{'workers':>7} {'n_batch':>8} {'spec':>5} {'tok/s':>8} {'por wrk':>8} {'ganho':>7}")
    for r in sorted(linhas, key=lambda x: -x["tok_s_agregado"]):
        print(f"{r['workers']:>7} {r['n_batch']:>8} {str(r['spec']):>5} "
              f"{r['tok_s_agregado']:>8.1f} {r['tok_s_por_worker']:>8.1f} "
              f"{r['tok_s_agregado']/base['tok_s_agregado']:>6.2f}x")
    print(f"\nBase = workers={base['workers']}, n_batch={base['n_batch']}, spec=False.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
