# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## O que é

Mente Digital — assistente Omni 100% local (voz + texto), rodando em GPU local (RTX 3080 / Ryzen 9 7950X3D). Backend FastAPI com WebSocket para chat ao vivo (VAD/barge-in), LLM local via `llama-cpp-python`, STT (Whisper), TTS (Piper), e RAG sobre um vault Obsidian indexado em ChromaDB, com fallback para busca web (DuckDuckGo).

Este é o pacote modularizado (V2) de um MVP monolítico anterior (`mvp_mente.py`), preservando os pilares arquiteturais originais: GPU serializada, streaming+chunking de TTS, speculative pre-fetch, ETL post-chat idle, e anti-alucinação via RAG aterrado.

## Comandos

```bash
pip install -r requirements.txt
python main.py                          # ou: uvicorn main:app --host 0.0.0.0 --port 8000

pip install -r requirements-dev.txt     # deps de teste (pytest, pytest-asyncio)
pytest                                   # suíte roda sem GPU/rede (usa fakes)
```

Servidor em `http://localhost:8000`. Há uma suíte `pytest` (pasta `tests/`) cobrindo a
lógica pura e sensível — gate de relevância, buffer anti-sentinela, chunker, latência —
com fakes de LLM/TTS/store (sem GPU nem rede). Não há linter ou build configurados.

**Ambiente:** o projeto roda na env conda `llama-omni` (Python 3.10.20) — o `python` no
PATH do Windows costuma ser o atalho falso da Microsoft Store. Rode via caminho absoluto:
`C:\ProgramData\miniconda3\envs\llama-omni\python.exe -m pytest`.

Configuração via `.env` (prefixo `MENTE_`), sem tocar no código — ex.: `MENTE_N_CTX=4096`, `MENTE_RAG_SCORE_CONFIDENT=0.7`. Todos os campos configuráveis estão em [config.py](config.py).

## Arquitetura

Wiring fino em [main.py](main.py) (lifespan cria os serviços e injeta em `AppContext`); toda a lógica vive nos módulos abaixo. Nenhum módulo de domínio conhece o WebSocket diretamente — o pipeline do agente recebe um callback `send(dict) -> bool`.

- **[config.py](config.py)** — `Settings` (Pydantic), incluindo o dicionário fonético inglês→PT-BR usado pelo TTS.
- **[state.py](state.py)** — `AppContext` (DI container em `app.state.ctx`) e `SessionMemory` (histórico de chat, "memória fresca da sessão", fila de ETL — todas `deque` com `maxlen` para não crescer sem fim).
- **[llm.py](llm.py)** — `LlamaManager`. GPU serializada por um `ThreadPoolExecutor(max_workers=1)` ("gpu-infer"): garante estruturalmente que dois decodes nunca rodam juntos na GPU. Cancelamento (barge-in) usa `stop_event` por requisição; o `asyncio.Lock` só é liberado depois que a thread realmente terminou (sem overlap de VRAM).
- **[audio.py](audio.py)** — `SttService` (faster-whisper / CTranslate2; device via `resolve_device`; suba `MENTE_WHISPER_MODEL=large-v3` para máxima qualidade), `TtsService` (Piper), `SentenceChunker` (quebra streaming de tokens em frases prontas para TTS, ignorando abreviações/decimais, com flush por tamanho).
- **[rag.py](rag.py)** — `EmbeddingProvider` (singleton; device via `resolve_device`, `auto` usa GPU se houver), `VectorStore` (Chroma com **distância de cosseno** — não o L2 padrão, pois os embeddings não são normalizados e os thresholds do gate são de escala cosseno; reindex incremental por `mtime`, chunking por cabeçalho Markdown via `split_markdown`, dedup por `source`), `WebSearcher` (DuckDuckGo com **fallback de backend** via `buscar_com_fallback`, cache LRU, speculative pre-fetch). **Trocar a métrica de distância exige recriar o banco vetorial** (apagar `banco_vetorial_cerebro`).
- **[agent.py](agent.py)** — `QueryOptimizer` (resolve pronomes cruzados via LLM), `Agent.pipeline_resposta` (orquestra cache-hit local/RAM → escalada para web), `LatencyTracker` (TTFT/TTFA), roteamento aditivo de ferramentas (`_rotear`/`_pipeline_tools`), `EtlProcessor` (sintetiza conhecimento novo no idle, sempre cedendo a GPU para inferência interativa via `ctx.interactive_idle`).
- **[tools.py](tools.py)** — function calling ADITIVO. Gate lexical `talvez_acao` (só mensagens de AÇÃO acionam o roteador LLM; pergunta comum não paga a chamada extra → TTFA preservado). Roteador via JSON (`parse_decisao`), não o tool-calling nativo do llama.cpp. `ToolRegistry` + ferramentas (calcular, hora_atual, salvar_nota, listar_notas, ler_nota, buscar_web). Loop agêntico CAPADO por `max_tool_steps`; ferramentas `terminal=True` saem no 1º passo.
- **[textutils.py](textutils.py)** — normalização de texto e extração de keywords (sem acento/stopwords) usada para aterramento léxico e limpeza de query.
- **[ws.py](ws.py)** — `LiveSession`: máquina de estados do WebSocket `/ws/chat_live` (VAD por RMS no servidor, barge-in cancela o pipeline em andamento, `end_session` dispara o ETL idle).
- **[telemetry.py](telemetry.py)** — logs coloridos thread-safe (`telemetry.track/warn/error`) e `Database` (SQLite: histórico de chat, log de ETL, métricas, e **latência TTFT/TTFA por resposta** via `save_latency`; médias expostas em `/api/metrics`). Nunca usar `except: pass` — todo erro passa por `telemetry.error`. A instrumentação de latência vive no `agent.LatencyTracker` (clock injetável, envolve o `send`).
- **[prompts.py](prompts.py)** — todos os prompts de sistema/tarefa centralizados aqui (extrator de query, filler, resposta principal, síntese ETL, resumo de sessão).

### Pipeline de resposta (`Agent.pipeline_resposta`)

1. `QueryOptimizer` extrai termos de busca da pergunta (resolvendo pronomes via histórico recente).
2. Busca local no `VectorStore` + memória de sessão filtrada por tema (`_ram_relevante`).
3. **Gate de relevância** (correção do "Cache Hit falso" — ver seção abaixo): só é Cache Hit se houver aterramento léxico (chunk menciona keyword da pergunta) OU confiança semântica alta (`distância < rag_score_confident`).
4. Se Cache Hit: responde com streaming, mas segura o áudio/tokens até confirmar que a resposta não é o sentinela anti-alucinação `"não tenho informações suficientes"` — se confirmar, escala para web sem nunca "falar" o sentinela (preserva TTFA).
5. Cache Miss (ou escalada): dispara um filler falado para mascarar latência, busca na web, responde em streaming, e enfileira o resultado para o ETL idle.

### Correção do "Cache Hit falso"

Ver [README.md](README.md) para o diagnóstico completo. Resumo: o gate antigo tratava "tem algum contexto" como Cache Hit; com um vault grande, quase toda pergunta achava algo vagamente parecido e a web nunca era consultada. A correção exige contexto **relevante** (aterramento léxico via `textutils.contem_alguma` OU `rag_score_confident`). O botão de calibração é `MENTE_RAG_SCORE_CONFIDENT` (default `0.8`; menor = mais rígido/mais web, maior = mais confiança no local) — cada pergunta loga `[LOCAL] melhor_dist=... relevante=...` para calibrar.

### Convenções

- Toda chamada bloqueante/CPU-bound (IO de arquivo, Whisper, Piper, embeddings, SQLite) passa por `asyncio.to_thread`.
- Erros nunca são engolidos silenciosamente — sempre `telemetry.error(modulo, mensagem, exc)`.
- Comentários no código explicam o *porquê* (ex.: por que um lock existe, que bug histórico uma heurística corrige) — mantenha esse padrão ao editar esses módulos.
- STT parcial (transcrição em tempo real/streaming) é uma não-feature intencional — não implementar sem pedido explícito (ver README).
