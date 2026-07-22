<div align="center">

# 🧠 Mente Digital

### A **100% local** omni assistant — voice and text, no cloud, no API keys, no third-party telemetry.

*A second brain that talks: converses by voice, answers from **your** Obsidian notes, falls back to the web only when it must — **acts** on spoken commands (reminders, lists, routines), **takes care** of things on its own (alarms, briefings, pomodoro), and, while you're not looking, distills what it learned into new atomic notes.*

![CI](https://github.com/danielpvp22/mente_digital/actions/workflows/tests.yml/badge.svg)
![Tests](https://img.shields.io/badge/tests-624_no_GPU_no_network-success)
![License](https://img.shields.io/badge/License-Apache_2.0-blue)
![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)
![Cloud](https://img.shields.io/badge/cloud-zero-critical)

**Target hardware:** RTX 3080 (10 GB) · Ryzen 9 7950X3D · Windows

**This is the condensed English overview.** The full deep-dive — 1,100+ lines covering every design decision — is the [Portuguese README](README.md).

</div>

---

## The 30-second layer

Six numbers, all measured in this repository:

| | |
|---:|:---|
| **624 tests** | the whole suite runs with **no GPU and no network**, in ~6 s — it is literally the CI job |
| **33% → 8%** | rate of "I don't know" *with the context in hand*, switching `Qwen2.5-7B` → `Qwen3-8B` — decided by an **in-repo A/B harness** (`eval/ab_modelos.py`) |
| **~2×** | RAG ranking quality from the embedding swap (known-item MRR@10 0.20 → 0.375, `eval/ab_embeddings.py`) |
| **0.55 → 0.16** | relevance gate **recalibrated from data** against the real knowledge base (`eval/calibrar_gate.py`) |
| **TTFT ≈ 1.1 s** | measured live on a vault-grounded answer (decode ≈ 85 tok/s), with per-stage timing at `/api/metrics` |
| **8.9 / 10 GB** | the whole stack (Qwen3-8B + e5-base + `q8_0` KV cache) resident in an RTX 3080's VRAM, ~1.3 GB to spare |

> **Language note:** the assistant itself speaks **Brazilian Portuguese** — the TTS voice, the prompts and the spoken-command grammar are PT-BR. The engineering is language-agnostic; the product, as shipped, is not.

---

## What it is

You talk; it listens, thinks and talks back — on a local GPU, with the first audio playing while the model is still decoding the rest of the sentence. Nothing leaves the machine except a web search when (and only when) local knowledge isn't enough. Beyond answering, it **acts** (spoken reminders, lists, notes, routines), **takes care** of things proactively (persistent alarms, "tell me when the dollar crosses X" watchers, a daily briefing), and **remembers** (undo, correct, confirm — the last action always has an inverse).

Five theses run through every line of the code:

1. **The knowledge base is yours, and it's an Obsidian vault.** Plain `.md` files you read, edit and version. ChromaDB is a derived, disposable index — the filesystem is the source of truth. Swapping the embedding model is a reindex, not data loss.
2. **What matters is perceived latency, not real latency.** The metric the system optimizes is **TTFA — time to first audio** — a token nobody heard doesn't exist. Streaming, per-sentence chunking, a spoken filler and a prefix-holding guard all exist to shrink that number, and it is measured and persisted per answer.
3. **Anti-hallucination is flow control, not prompting.** The assistant prefers "I don't know" to inventing — but that sentinel is an **internal control signal** the user never hears: the system holds the audio, detects it mid-stream, discards the answer and escalates to the web without a syllable having leaked.
4. **A command is not a conversation — and the boundary is physical.** A sentence that *starts* with the master word (`"mestre, …"`) enters an isolated, deterministic flow: resolved by regex without paying for an LLM call whenever possible, and — crucially — **it never becomes knowledge**. A reminder's persistence is its table row, not a note polluting the vault.
5. **The system works while nobody is watching — on both sides.** An idle-time ETL distills conversations and web findings into atomic Zettelkasten notes that are born tagged `#conhecimento_novo` and only "mature" (lose the tag) when actually reused. Meanwhile a persistent scheduler carries the continuous responsibility — firing due alarms, checking watchers, delivering briefings — speaking on its own, and always yielding the GPU to live conversation.

---

## How a turn flows

Microphone → server-side VAD (RMS) → Whisper (`large-v3-turbo`) → query optimizer (resolves cross-turn pronouns) → a **source cascade with a relevance gate**: fresh session memory → Obsidian vault (cosine ChromaDB + lexical/IDF grounding) → web as last resort (DuckDuckGo, then an async **deep-fetch** of the top pages, `trafilatura` extraction, and ephemeral re-ranking of passages with the same embedding model — nothing gets indexed). The answer streams token-by-token through a sentence chunker into Piper TTS, so the first audio plays while the LLM is still decoding. Speaking over it (barge-in) cancels the in-flight decode via a per-request stop event; the GPU is structurally serialized by a single-worker executor, so two decodes can never overlap in VRAM.

---

## Module map

Thin wiring in `main.py` (lifespan builds the services and injects an `AppContext`); no domain module knows about the WebSocket — the pipeline receives a `send(dict) -> bool` callback.

| Module | Role |
|---|---|
| `config.py` | All settings (Pydantic, `MENTE_*` env prefix) — including the English→PT-BR phonetic dictionary for TTS |
| `state.py` | DI container + bounded session memory (`deque`s with `maxlen`) |
| `llm.py` | The **only door to the GPU**: single-worker executor, cooperative preemption, lock released only after the thread truly finished |
| `audio.py` | STT (faster-whisper), TTS (Piper), and the streaming sentence chunker |
| `rag.py` | Vector store (cosine, incremental reindex by `mtime`), web search with backend fallback, deep-fetch + ephemeral RAG |
| `agent.py` | The orchestrator: response pipeline, tool routing, idle ETL, on-demand synthesis (map-reduce) |
| `tools.py` | **Additive** function calling: a lexical gate decides if the LLM router is even consulted, so plain questions never pay for it |
| `mestre.py` | The command plane: regex-first parsing, spoken chaining, undo/correct/confirm, frequency-based shortcuts |
| `agenda.py` | A pure, injectable-clock PT-BR time parser ("tomorrow at 8", "every Monday", "in 30 min") |
| `scheduler.py` | Persistent proactive agents: alarms, watchers, briefings — with spoken push and re-delivery after reconnect |
| `acesso.py` | Access control: token (constant-time compare) or loopback-only by default, plus WebSocket `Origin` validation |
| `ws.py` | The live-session state machine (VAD, barge-in, conversation lifecycle) |
| `telemetry.py` | Thread-safe colored logs + SQLite (history, ETL log, per-answer TTFT/TTFA/tok/s metrics) |
| `prompts.py` | Every system/task prompt, centralized |

---

## Engineering highlights

- **Concurrency over a non-preemptible resource.** The GPU is serialized *by structure* (a single-worker executor), preemption is cooperative and first-class (barge-in cancels mid-decode), and the async lock is only released once the worker thread has actually exited — no VRAM overlap, ever.
- **Subtle streaming state machines.** The `<think>`-stripper holds a prefix only while it could still *become* `<think>`, decides on the first byte that proves otherwise, and its flush guarantees user text is never swallowed. The anti-hallucination guard applies the same pattern to the sentinel phrase. Property-based tests (Hypothesis) sweep both with random token partitions.
- **Decisions by measurement, not fashion.** Model, embedding and quantization choices each came from an in-repo A/B harness — and features get turned *off* with numbers too (speculative decoding: no win on short prompts, shape crash on long context → disabled by flag, documented).
- **Security awareness at the edges.** PII masking on outbound web queries, prompt-injection stripping on inbound web content, a confidential mode that keeps a turn RAM-only, an audit trail for mutating actions, and fallback paths for practically every failure.
- **Testability as a design constraint.** 624 tests with no GPU and no network: lazy heavy imports, pure modules with injected clocks, fakes that honor the real contracts (including preemption).

---

## War stories (condensed)

The full versions — with root-cause analyses — are in the [Portuguese README](README.md#-war-stories-os-bugs-que-moldaram-a-arquitetura).

1. **The false Cache Hit.** The gate treated *"found any context"* as a hit; with a big vault every question matched something vaguely similar, so the web was never consulted. Fix: relevance = lexical grounding **or** high semantic confidence — later hardened with IDF so matching a *generic* keyword no longer counts.
2. **The tasks the garbage collector ate.** The event loop holds only weak references to tasks; fire-and-forget background work died silently mid-flight. The insight wasn't "keep a strong reference" — it was *scope*: the reference set lives on the app context, not the WebSocket session, because prefetch/ETL/scheduler must outlive the connection.
3. **The gate that rejected everything (L2 vs. cosine).** Unnormalized embeddings + Chroma's default L2 meant a *good* match scored ~15 against cosine-scale thresholds — 100% of chunks failed, everything escalated to the web. Switching the metric requires rebuilding the index: the HNSW graph is constructed *with* it.
4. **"The web answered" but the model said it didn't know.** The search snippets genuinely didn't contain the answer — the LLM was right, and prompt-tweaking would have been the wrong fix. The fix was in the data: deep-fetch the page bodies, extract the main text, re-rank passages against the question.
5. **The generator blind to follow-ups.** *"Tell me more"* hit the sentinel even with the right notes retrieved: pronoun resolution was feeding the *retriever*, while the *generator* still saw the raw text. Knowing which of the two consumers to fix is the difference between a patch and a regression.
6. **Commands becoming knowledge — and a list's "and" becoming a cut.** Boundary bugs: reminders were being distilled into the knowledge base (fix: a physical boundary — command turns never feed the ETL), and spoken chaining split *"milk, flour and eggs"* into two actions (fix: a connector only splits when followed by the *start of a new action*).
7. **The bridge ranking that only found the obvious.** Connection discovery ranked concept bridges by co-occurrence and surfaced trivial big-topic pairs. Re-ranking by **surprise** (1 − Jaccard of concept neighborhoods) surfaced genuinely disjoint domains — validated on the real base of 12,778 atoms.

---

## Quick start

**Prereqs:** Python 3.10, an NVIDIA GPU (`llama-cpp-python` compiled with CUDA; CPU works but is slow).

```bash
git clone https://github.com/danielpvp22/mente_digital.git
cd mente_digital
python -m venv .venv && .venv\Scripts\activate   # source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
```

Download the models (not in the repo): the LLM (`Qwen3-8B` GGUF `Q4_K_M`) and the Piper voice (`pt_BR-cadu-medium.onnx` **and** its `.onnx.json`) go into `dados/modelos/`; Whisper and the embedding model download themselves on first run. The vault can start empty. Then:

```bash
python -m mente_digital.main   # http://localhost:8000
```

Configuration is `.env`-driven (`MENTE_*` prefix) — every knob is documented in [`config.py`](mente_digital/config.py). Tests:

```bash
pip install -r requirements-dev.txt
pytest                # 624 tests, no GPU, no network — CI installs requirements-ci.txt only
```

---

## Non-goals

This is a **single-user appliance** by thesis — "100% local, your machine, your vault". Multi-tenancy (per-user auth, vault isolation, fair GPU scheduling) would fight that thesis and is a project of its own. The chosen boundary is **one owner, many devices**: token-gated routes/WebSocket (or loopback-only by default) plus a TLS helper for LAN access. Also deliberate: no partial/streaming STT (would destabilize the VAD contract), and speculative decoding stays off — with the numbers that justify it.

## License

[Apache-2.0](LICENSE) · attribution in [NOTICE](NOTICE).
