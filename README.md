# Mente Digital — V2 (modularizado)

Assistente Omni 100% local (RTX 3080 / Ryzen 9 7950X3D). Refatoração do MVP
monolítico (`mvp_mente.py`) em um pacote modular, **preservando os pilares da
arquitetura**: GPU serializada, streaming + chunking de TTS, speculative
pre-fetch, ETL post-chat idle e anti-alucinação RAG.

## Estrutura

```
mente_digital/
├── main.py        # FastAPI: lifespan, rotas HTTP, endpoint WebSocket (wiring fino)
├── config.py      # Pydantic Settings (paths, params da GPU, thresholds, dicionário fonético)
├── prompts.py     # Prompts diretivos centralizados
├── state.py       # AppContext + memória de sessão (coleções com maxlen) + LRU
├── telemetry.py   # Logs coloridos (thread-safe) + SQLite (ETL, histórico, métricas)
├── llm.py         # LlamaManager: inference_lock endurecido (executor single-thread)
├── audio.py       # SttService (Whisper), TtsService (Piper), SentenceChunker
├── rag.py         # EmbeddingProvider (singleton), VectorStore (reindex por mtime), WebSearcher
├── agent.py       # QueryOptimizer, Agent.pipeline_resposta, EtlProcessor (idle)
├── ws.py          # LiveSession: máquina de estados VAD/barge-in/end_session
├── templates/index.html
├── requirements.txt
└── README.md
```

## Setup / Instalação

### Pré-requisitos

- **Python 3.10.20** (o projeto roda em 3.10; use exatamente essa versão para evitar
  incompatibilidades de wheels de ML).
- **GPU NVIDIA + CUDA Toolkit** para rodar o LLM na GPU. O `llama-cpp-python` precisa
  ser **compilado com suporte CUDA** (não basta o `pip install` padrão); no Windows
  isso exige também o "Visual Studio Build Tools" com C++. Sem GPU NVIDIA, dá para
  rodar em CPU, porém lento.

### 1. Clonar e criar a venv

```bash
git clone https://github.com/danielpvp22/mente_digital.git
cd mente_digital

# venv com Python 3.10.20
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Baixar os modelos (NÃO vêm no repositório)

Crie uma pasta para os modelos (ex.: `C:\IA\modelos`) e coloque nela:

```
C:\IA\modelos\
├── Qwen2.5-Coder-7B-Instruct-Uncensored.Q4_K_M.gguf   # LLM (~4.7 GB)
├── pt_BR-cadu-medium.onnx                              # voz TTS (Piper)
└── pt_BR-cadu-medium.onnx.json                         # config da voz (fica junto do .onnx)
```

- **Voz Piper** (`pt_BR-cadu`, medium): repositório oficial
  [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) em `pt/pt_BR/cadu/medium/`.
  Baixe o `.onnx` **e** o `.onnx.json`.
- **Whisper** (STT) e **embeddings** baixam sozinhos na 1ª execução (via internet).
- O **banco vetorial** (`./banco_vetorial_cerebro`) e a **pasta do vault Obsidian** são
  criados automaticamente no startup — o vault pode começar vazio (cai no fallback web
  até você adicionar notas `.md`).

### 3. Criar o `.env` (caminhos e parâmetros, sem tocar no código)

Cada máquina tem o seu — o `.env` está no `.gitignore`. Crie um na raiz do projeto
apontando para onde você salvou os arquivos:

```
MENTE_CAMINHO_MODELO_LLAMA=C:\IA\modelos\Qwen2.5-Coder-7B-Instruct-Uncensored.Q4_K_M.gguf
MENTE_CAMINHO_VOZ_PIPER=C:\IA\modelos\pt_BR-cadu-medium.onnx
MENTE_CAMINHO_OBSIDIAN=C:\IA\Cerebro_Digital
# (opcional) qualquer outro parâmetro, ex.:  MENTE_N_CTX=8192
```

### 4. Rodar

```bash
python main.py            # ou: uvicorn main:app --host 0.0.0.0 --port 8000
```

Abra `http://localhost:8000`.

## O que mudou (resumo)

**Bugs & races**
- `inference_lock` endurecido: um `ThreadPoolExecutor(max_workers=1)` garante que
  dois decodes **nunca** rodem juntos na GPU, e um `stop_event` faz o barge-in
  liberar a thread sem overlap de VRAM (antes a daemon thread seguia decodificando
  após o cancelamento).
- Frontend: `encerrarLiveMode()` agora existe e centraliza o teardown do áudio.
- Reindex do Chroma por `mtime` (corrige a heurística `len(ids) < len(arquivos)`,
  que quebrava após o primeiro split), com dedup por `source`.
- Fim dos `except: pass`: todo erro passa por `telemetry`.

**Latência / TTFA**
- `SentenceChunker` quebra por fim-de-frase real (ignora `Dr.`, `3.5`, `etc.`) e
  faz flush por tamanho — menos picote no áudio.
- Embeddings carregados **uma vez** (singleton), não a cada reindexação.
- Warm-up do LLM no boot para a 1ª resposta não pagar o cold-start.

**Robustez / features**
- Histórico persistido em SQLite (`/api/historico` sobrevive a restart).
- Endpoint `/api/metrics` (contagens de ETL/chat + estado dos serviços).
- Coleções de sessão com `maxlen` (sem creep de RAM); cache web LRU.
- Degradação graciosa: se um modelo falhar ao carregar, o servidor sobe e avisa.
- Reconexão do WebSocket com backoff exponencial + indicador de conexão.
- ETL idle cede a vez para a inferência interativa (`interactive_idle`).

## Correção do "Cache Hit falso" (não tenho informações suficientes)

Sintoma: o agente respondia "Não tenho informações suficientes" em loop, mesmo com
a web disponível, porque o gate tratava *"tem algum contexto"* como Cache Hit —
quando deveria ser *"tem contexto **relevante**"*. Com um vault grande, quase toda
pergunta achava algo vagamente parecido (ou herdava uma entrada velha da RAM), então
a web nunca era consultada e o guard anti-alucinação disparava sobre contexto errado.

O que passou a valer como contexto relevante:

- **Aterramento léxico** (`rag.py`): um chunk só conta se menciona uma keyword da
  pergunta OU é semanticamente muito próximo (`rag_score_confident`).
- **RAM filtrada por tema** (`agent.py`): só injeta memória da sessão cujo tema casa
  com a pergunta — nada de herdar o assunto anterior.
- **Extrator enxuto** (`textutils.limpar_query`): tira saudação/fillers e capa a
  query, então "Olá gostaria de entender… TensorFlow RT" vira "TensorFlow RT".
- **Rede de segurança** (`agent._responder_contexto`): se o contexto não bastar, a
  resposta é escalada para a web **sem "falar" o 'não tenho'** (segura o áudio até
  descartar o sentinela; TTFA preservado para respostas reais).

**Botão de calibração:** cada pergunta agora loga `[LOCAL] melhor_dist=... relevante=...`.
Rode algumas perguntas, veja a distância dos bons matches locais e ajuste
`MENTE_RAG_SCORE_CONFIDENT` (default `0.8`) — menor = mais rígido (mais web), maior =
mais confiança no local.

## Nota sobre STT parcial (transcrição em tempo real)

Ficou **intencionalmente adiada**. Transcrição parcial estável exige um ASR de
streaming (ex.: `faster-whisper` com janelas deslizantes) e mexeria no contrato
do VAD atual — risco desproporcional para esta rodada, que priorizou
estabilidade. O ponto de entrada natural é `SttService`; dá para evoluir sem
tocar no resto do pipeline. Me avise que eu implemento numa próxima passada.
