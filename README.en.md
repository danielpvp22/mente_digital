<div align="center">

# 🧠 Mente Digital

### A **100% local** omni assistant — voice and text, no cloud, no API keys, no third-party telemetry.

*A second brain that talks: converses by voice, answers from **your** Obsidian notes and **your books**, falls back to the web only when it must — **acts** on spoken commands (reminders, lists, routines), **takes care** of things on its own (alarms, briefings, pomodoro), and, while you're not looking, distills what it learned into new atomic notes.*

![CI](https://github.com/danielpvp22/mente_digital/actions/workflows/tests.yml/badge.svg)
![Tests](https://img.shields.io/badge/tests-1378_no_GPU_no_network-success)
![License](https://img.shields.io/badge/License-Apache_2.0-blue)
![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)
![Cloud](https://img.shields.io/badge/cloud-zero-critical)

**Target hardware:** RTX 3080 (10 GB) · Ryzen 9 7950X3D · Windows

**This is the condensed English overview.** The full deep-dive — every design decision, with war stories and measured trade-offs — is [ARQUITETURA.md](ARQUITETURA.md) (in Portuguese). How the project got here, read commit by commit, is [docs/EVOLUCAO_DO_PROJETO.md](docs/EVOLUCAO_DO_PROJETO.md).

**🎬 Demo (Portuguese audio):** [voice mode](docs/demo/mente_digital_voz.mp4) · [text mode](docs/demo/mente_digital_texto.mp4) — Task Manager (GPU/VRAM) and the terminal are visible on purpose: real local inference with per-answer route and latency, not an API wrapper.

</div>

---

## The 30-second layer

Eight numbers, all measured in this repository:

| | |
|---:|:---|
| **1,378 tests** | the whole suite runs with **no GPU and no network**, in ~12 s — it is literally the CI job |
| **33% → 8%** | rate of "I don't know" *with the context in hand*, switching `Qwen2.5-7B` → `Qwen3-8B` — decided by an **in-repo A/B harness** (`eval/ab_modelos.py`) |
| **~2×** | RAG ranking quality from the embedding swap (known-item MRR@10 0.20 → 0.375, `eval/ab_embeddings.py`) |
| **0.55 → 0.16** | relevance gate **recalibrated from data** against the real knowledge base (`eval/calibrar_gate.py`) |
| **27 s → 10-12 s** | a web-escalated turn in live voice mode, after the latency round (raced deep-fetch, parallel filler) |
| **31.7 s → 12.4 s** | server boot, across three measured passes (lazy TTS → RAM pre-assembly → parallelism) |
| **1,736 figures** | searchable visual corpus extracted from books via **semantic layout** from the OCR model (vs. 777 from the pixel heuristic it replaced) |
| **8.9 / 10 GB** | the whole stack (Qwen3-8B + e5-base + `q8_0` KV cache) resident in an RTX 3080's VRAM, ~1.3 GB to spare |

> **Language note:** the assistant itself speaks **Brazilian Portuguese** — the TTS voice, the prompts and the spoken-command grammar are PT-BR. The engineering is language-agnostic; the product, as shipped, is not.

---

## What it is

You talk; it listens, thinks and talks back — on a local GPU, with the first audio playing while the model is still decoding the rest of the sentence. Nothing leaves the machine except a web search when (and only when) local knowledge isn't enough. Beyond answering, it **acts** (spoken reminders, lists, notes, routines), **takes care** of things proactively (persistent alarms, "tell me when the dollar crosses X" watchers, a daily briefing), and **remembers** (undo, correct, confirm — the last action always has an inverse).

Five theses run through every line of the code:

1. **The knowledge base is yours, and it's an Obsidian vault.** Plain `.md` files you read, edit and version. ChromaDB is a derived, disposable index — the filesystem is the source of truth. Swapping the embedding model is a reindex, not data loss. It grows through two doors: **your curiosity** (idle ETL distills what you asked about) and **bulk ingestion** — drop a PDF in a folder and it becomes hundreds of atoms with page-level provenance plus a searchable figure corpus.
2. **What matters is perceived latency, not real latency.** The metric the system optimizes is **TTFA — time to first audio** — a token nobody heard doesn't exist. Streaming, per-sentence chunking, a spoken filler and a prefix-holding guard all exist to shrink that number, and it is measured per answer with p50/p95 per stage.
3. **Anti-hallucination is flow control, not prompting.** The assistant prefers "I don't know" to inventing — but that sentinel is an **internal control signal** the user never hears: the system holds the audio, detects it mid-stream, discards the answer and escalates to the web without a syllable having leaked.
4. **A command is not a conversation — and the boundary is physical.** A sentence that *starts* with the master word (`"mestre, …"`) enters an isolated, deterministic flow: resolved by regex without paying for an LLM call whenever possible, and — crucially — **it never becomes knowledge**. A reminder's persistence is its table row, not a note polluting the vault.
5. **The system works while nobody is watching — on both sides.** An idle-time ETL distills conversations, web findings and whole books into atomic Zettelkasten notes, born tagged `#conhecimento_novo` and only "maturing" when actually reused. Meanwhile a persistent scheduler carries the continuous responsibility — firing due alarms, checking watchers, delivering briefings — speaking on its own, and always yielding the GPU to live conversation.

---

## How a turn flows

Microphone → server-side VAD (RMS) → Whisper (`large-v3-turbo`) → query optimizer (resolves cross-turn pronouns) → a **source cascade with a relevance gate**: fresh session memory → Obsidian vault (cosine ChromaDB + IDF-weighted lexical grounding) → web as last resort (DuckDuckGo, then a raced async **deep-fetch** of the top pages, `trafilatura` extraction, and ephemeral re-ranking of passages with the same embedding model — nothing gets indexed). The answer streams token-by-token through a sentence chunker into TTS, so the first audio plays while the LLM is still decoding. Speaking over it (barge-in) cancels the in-flight decode via a per-request stop event; the GPU is structurally serialized by a single-worker executor, so two decodes can never overlap in VRAM.

---

## Module map

Thin wiring in `main.py` (lifespan builds the services and injects an `AppContext`); no domain module knows about the WebSocket — the pipeline receives a `send(dict) -> bool` callback. **~21,000 lines of Python across 52 modules.**

| Module | Role |
|---|---|
| `config.py` | All settings (Pydantic, `MENTE_*` env prefix) — 282 knobs, including the English→PT-BR phonetic dictionary for TTS |
| `state.py` | DI container + bounded session memory (`deque`s with `maxlen`) |
| `llm.py` | The **only door to the GPU**: single-worker executor, cooperative preemption, lock released only after the thread truly finished |
| `audio.py` / `tts_xtts.py` | STT (faster-whisper), TTS (Piper on CPU by default; XTTS-v2 on GPU, opt-in and lazily loaded), and the streaming sentence chunker |
| `rag.py` | Vector store (cosine, incremental reindex by `mtime`), the concept graph, web search with backend fallback, deep-fetch + ephemeral RAG, and the figure search space |
| `agent.py` + `respostas.py` | The orchestrator: response pipeline, tool routing, on-demand synthesis (map-reduce), maturity promotion |
| `etl.py` | Idle-time worker: web queue, conversation distillation, proactive research, book ingestion |
| `tools.py` | **Additive** function calling: a lexical gate decides if the LLM router is even consulted, so plain questions never pay for it |
| `mestre.py` + `comandos_mestre.py` | The command plane: regex-first parsing, spoken chaining, undo/correct/confirm, frequency-based shortcuts |
| `agenda.py` / `scheduler.py` | A pure, injectable-clock PT-BR time parser; persistent proactive agents with spoken push and re-delivery after reconnect |
| *ingestion* | `livro.py`, `ocr.py`, `figuras.py`, `figuras_recorte.py`, `encyclopedia.py`, `obras.py`, `fusao.py`, `triagem.py`, `idioma.py`, `reparo.py` — mostly pure; the GPU/disk work lives in the idle ETL |
| `acesso.py` | Access control: token (constant-time compare) or loopback-only by default, plus WebSocket `Origin` validation |
| `ws.py` | The live-session state machine (VAD, barge-in, half-duplex against its own echo, conversation lifecycle) |
| `telemetry.py` | Thread-safe colored logs + SQLite (history, ETL log, per-stage latency percentiles) |

Plus eleven tiny **pure agent modules** (spaced repetition, habit streaks, graph bridges, PII masking, VRAM governor, prompt-injection stripping, circuit breaker…) — data in, data out, no GPU, no network, no disk. That's where GPU-free testability comes from.

---

## Engineering highlights

- **Concurrency over a non-preemptible resource.** The GPU is serialized *by structure* (a single-worker executor), preemption is cooperative and first-class (barge-in cancels mid-decode), and the async lock is only released once the worker thread has actually exited — no VRAM overlap, ever. Three different background producers (ETL, watcher, briefing) yield through the same two-level priority primitive.
- **Subtle streaming state machines.** The `<think>`-stripper holds a prefix only while it could still *become* `<think>`, decides on the first byte that proves otherwise, and its flush guarantees user text is never swallowed. The anti-hallucination guard applies the same pattern to the sentinel phrase. Property-based tests (Hypothesis) sweep both with random token partitions.
- **Decisions by measurement, not fashion.** Model, embedding and quantization choices each came from an in-repo A/B harness — and features get turned *off* with numbers too: speculative decoding (no win on short prompts, shape crash on long context), a concept-graph expansion built, measured three times and disabled, HyDE measured and rejected (better distances, *worse* answers — which invalidated the metric being optimized).
- **Measurement bugs are treated as first-class bugs.** An eval harness that had "right" and "wrong" inverted; a TTFT comparison whose fixed ordering produced the opposite conclusion; a language detector with 463 false positives. Each got its own fix commit.
- **Security awareness at the edges.** PII masking on outbound web queries, prompt-injection stripping on inbound web content (including at the *persistence* choke point — a payload would otherwise become a permanent vault note), a confidential mode that keeps a turn RAM-only *and blocks web escalation*, an audit trail for mutating actions, and fallback paths for practically every failure.
- **Testability as a design constraint.** 1,378 tests with no GPU and no network: lazy heavy imports, pure modules with injected clocks, fakes that honor the real contracts (including preemption) and a contract test that binds fake signatures to the real ones.

---

## War stories (condensed)

The full versions — with root-cause analyses — are in [ARQUITETURA.md](ARQUITETURA.md#-war-stories-os-bugs-que-moldaram-a-arquitetura) (in Portuguese).

1. **The false Cache Hit.** The gate treated *"found any context"* as a hit; with a big vault every question matched something vaguely similar, so the web was never consulted. Fix: relevance = lexical grounding **or** high semantic confidence — later hardened with IDF so matching a *generic* keyword no longer counts.
2. **The tasks the garbage collector ate.** The event loop holds only weak references to tasks; fire-and-forget background work died silently mid-flight. The insight wasn't "keep a strong reference" — it was *scope*: the reference set lives on the app context, not the WebSocket session, because prefetch/ETL/scheduler must outlive the connection.
3. **The gate that rejected everything (L2 vs. cosine).** Unnormalized embeddings + Chroma's default L2 meant a *good* match scored ~15 against cosine-scale thresholds — 100% of chunks failed, everything escalated to the web. Switching the metric requires rebuilding the index: the HNSW graph is constructed *with* it.
4. **"The web answered" but the model said it didn't know.** The search snippets genuinely didn't contain the answer — the LLM was right, and prompt-tweaking would have been the wrong fix. The fix was in the data: deep-fetch the page bodies, extract the main text, re-rank passages against the question.
5. **The generator blind to follow-ups.** *"Tell me more"* hit the sentinel even with the right notes retrieved: pronoun resolution was feeding the *retriever*, while the *generator* still saw the raw text. Knowing which of the two consumers to fix is the difference between a patch and a regression.
6. **Commands becoming knowledge — and a list's "and" becoming a cut.** Boundary bugs: reminders were being distilled into the knowledge base (fix: a physical boundary — command turns never feed the ETL), and spoken chaining split *"milk, flour and eggs"* into two actions (fix: a connector only splits when followed by the *start of a new action*).
7. **The 46-second freeze.** Saving a note triggered a full re-index and concept-graph rebuild **inline, on the serialized GPU, mid-conversation**. The user re-asked the same question three times thinking it had hung. Any background work touching the GPU needs an owner that defers it — and the right gate is the session, not the operation.
8. **The TTS that killed the LLM.** An over-long sentence blew the internal GPT-2 token cap of the neural TTS and fired a device-side assert that corrupted the CUDA context — taking llama.cpp, Whisper and the process down with it. Later, two concurrent syntheses did the same. "One thread per model" is not isolation: there is one CUDA context per process.
9. **The assistant answering its own ghosts.** Without echo cancellation, the mic picked up the assistant's own speech; Whisper hallucinated short utterances from the degraded echo, and each hallucination opened a new turn. Fixed with half-duplex plus a `no_speech_prob` heuristic.
10. **The VRAM "leak" that was two copies of the app.** uvicorn binds the port only *after* the lifespan finishes — and the lifespan is where everything loads. A second launch loaded the full stack (~45 s, ~4.7 GB), printed "online", and only then discovered the port was taken, lingering as a zombie holding the GPU. A ~0.2 ms bind test at the top of the lifespan fixed it. Per-process metrics can't see cross-process contention.
11. **I measured the wrong thing for hours.** Eleven commits of atomization tuning, and blind tests still favored the old knowledge base. The bottleneck wasn't atom quality — it was how the *context* was assembled (figure notes eating 40% of the char budget). Two retrieval fixes, zero GPU and zero atoms rewritten, flipped the score. Root cause of the confusion: the quality tests bypassed the production retrieval path.

---

## Quick start

**Prereqs:** Python 3.10, an NVIDIA GPU (`llama-cpp-python` compiled with CUDA; CPU works but is slow).

```bash
git clone https://github.com/danielpvp22/mente_digital.git
cd mente_digital
python -m venv .venv && .venv\Scripts\activate   # source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
cp .env.example .env                             # don't skip — see the note below
```

> ⚠️ **Copy `.env.example` — the adopted stack lives there, not in the code defaults.** `config.py` still ships the previous era's coherent set (MiniLM + empty prefixes + gate `0.8`); the adopted one (e5-base + `query:`/`passage:` prefixes + gate `0.16`) is in `.env.example`. Running without it silently gives you the **old embedding, ~2× worse at ranking**. And never change one without the other: distance scale is a function of the model, so a threshold measured on one embedding applied to another is the exact signature of the L2-vs-cosine bug that once took this project's retrieval down entirely.

Download the models (not in the repo): the LLM (`Qwen3-8B` GGUF `Q4_K_M`) and the Piper voice (`pt_BR-cadu-medium.onnx` **and** its `.onnx.json`) go into `dados/modelos/`; Whisper and the embedding model download themselves on first run. The vault can start empty. Then:

```bash
python main.py   # http://localhost:8000 — online in ~12 s
```

Configuration is `.env`-driven (`MENTE_*` prefix) — every knob is documented in [`config.py`](mente_digital/config.py). Tests:

```bash
pip install -r requirements-dev.txt
pytest                # 1,378 tests, no GPU, no network — CI installs requirements-ci.txt only
```

---

## Non-goals

This is a **single-user appliance** by thesis — "100% local, your machine, your vault". Multi-tenancy (per-user auth, vault isolation, fair GPU scheduling) would fight that thesis and is a project of its own. The chosen boundary is **one owner, many devices**: token-gated routes/WebSocket (or loopback-only by default) plus a TLS helper for LAN access. Also deliberate: no partial/streaming STT (would destabilize the VAD contract), and speculative decoding stays off — with the numbers that justify it.

## License

[Apache-2.0](LICENSE) · attribution in [NOTICE](NOTICE).
