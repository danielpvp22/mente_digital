# Mapa: Mente Digital → competências de Engenharia de Dados (vaga XP Pleno)

> Este documento é a **munição** por trás do CV, do LinkedIn e dos posts.
> Ele traduz o que o projeto **Mente Digital** demonstra para a linguagem que
> a vaga de **Engenheiro de Dados Pleno da XP** procura. Use-o para responder
> em entrevista "onde você fez X?" apontando para código real.

## A vaga exata (XP Inc. — Engenheiro(a) de Dados | Pleno | São Paulo, presencial flexível)

> Fonte: https://job-boards.greenhouse.io/xpinc/jobs/8504488002
> **Atenção:** a vaga é anunciada como **"Exclusiva PCD"** — confirme se você se enquadra antes de aplicar.

**Responsabilidades:**
- Modelar e organizar dados para consumo pelos times de **IA**
- Criar e otimizar **camadas analíticas Bronze/Silver/Gold (medallion)** no Databricks
- Preparar **datasets de alta qualidade** a partir de múltiplas **fontes bancárias**
- Ajustes de **performance, custo e eficiência**
- Suporte aos **cientistas de dados** com dados consistentes
- Apoiar pipelines existentes via **ADF e Airflow**

| Requisito da vaga | Peso |
|---|---|
| SQL avançado + modelagem de dados | Essencial |
| Databricks (Spark, Delta) | Essencial |
| Python para engenharia de dados | Essencial |
| ADF e/ou Airflow | Essencial |
| DataOps: versionamento, boas práticas | Essencial |
| Dados bancários | Diferencial |
| Governança (Unity Catalog, Data Quality, Lineage) | Diferencial |
| Otimização de performance/custo em Databricks | Diferencial |

## Os dois ganchos de ouro desta vaga específica

1. **"Modelar dados para consumo pelos times de IA."** O Mente Digital é, na essência,
   um sistema que **transforma dados brutos em dados prontos para um modelo de IA
   consumir** (RAG): ingestão → limpeza → atomização → indexação → recuperação
   ranqueada. Você não "usou IA"; você **fez a engenharia de dados que alimenta a IA**
   — que é exatamente a missão do time.

2. **"Bronze/Silver/Gold (medallion)."** Descreva seu ETL nessa linguagem:
   - **Bronze (bruto):** `chat_dump_bruto.md` + páginas web baixadas (`httpx`) — dado cru, sem tratamento.
   - **Silver (limpo/conformado):** extração do texto principal (`trafilatura`), normalização, deduplicação, atomização em unidades canônicas com proveniência (frontmatter = metadados de linhagem).
   - **Gold (pronto para consumo):** notas atômicas indexadas em vetor, ranqueadas por relevância e servidas ao modelo — o "dataset de alta qualidade" que a camada de IA consome.

   Falar do seu projeto em Bronze/Silver/Gold na entrevista mostra que você **já pensa
   no modelo mental da XP**, mesmo sem ter usado o Databricks ainda.

## Como o Mente Digital comprova cada eixo

A vaga é Databricks/Spark, e o projeto é um assistente de voz local — então **não
finja Spark onde não tem**. O que o projeto prova de verdade é o *pensamento* de
engenharia de dados: pipelines, ETL, qualidade, otimização, observabilidade e
DataOps. É isso que você conecta.

### 1. Pipeline de ETL de verdade (ingestão → transformação → carga)
- **`etl.py`** roda um processo idle que destila conversas e pesquisas web em
  **notas atômicas** (Zettelkasten) — ingestão incremental, transformação
  (atomização, normalização de estrutura, deduplicação) e carga na base.
- **Reindex incremental por `mtime`** (`rag.py`): o timestamp do filesystem é um
  *change-data-feed* grátis — só reprocessa o que mudou. É exatamente o padrão de
  **ingestão incremental / CDC** que se usa em Delta Lake (`MERGE`/upsert).
- **Deduplicação** por `source` e near-dup por Jaccard antes da carga — controle
  de qualidade na entrada, o mesmo problema de *data quality* de um pipeline batch.

> **Analogia para a entrevista:** "o reindex por `mtime` é o mesmo racional do
> incremental load com watermark/CDC no Delta; a dedup por Jaccard é o meu
> `dropDuplicates` com regra de negócio."

### 2. Modelagem e arquitetura relacional + não-relacional
- **SQLite** (`telemetry.py`): fatos episódicos e estado dos agentes, com
  **migrações idempotentes** (`PRAGMA table_info` + `ALTER TABLE`) — versionamento
  de schema na prática, o mesmo cuidado de um migration de produção.
- **ChromaDB** (não-relacional / vetorial) com **métrica de cosseno explícita** —
  escolha de arquitetura justificada por dado, não por default.
- Convivência dos dois paradigmas (relacional para fatos, vetorial para busca
  semântica) é literalmente "arquitetura relacional **e** não-relacional".

### 3. Qualidade de dados e avaliação orientada a dados (DataOps)
- Pasta **`eval/`**: harnesses de A/B que **decidem por métrica**, não por achismo:
  - `eval/ab_embeddings.py` — MRR@10 0.20 → 0.375 (2× no ranqueamento).
  - `eval/ab_modelos.py` — taxa de "não sei" 33% → 8%.
  - `eval/calibrar_gate.py` — recalibrou o gate de relevância por dados (0.55 → 0.16).
- **`eval/bench_ttfa.py`** — bench de regressão com *guardrails* que falha o build
  (exit != 0) se um invariante quebrar. Isso é **teste de pipeline de dados**.

> **Analogia:** "meus harnesses de `eval/` são o meu Great Expectations / testes de
> data quality: definem o critério de aceite e quebram o CI se a métrica regride."

### 4. Otimização de performance e custo
- **Orçamento de recursos escasso** (10 GB de VRAM) forçou decisões de custo:
  quantização, KV-cache `q8_0` (metade da memória), serialização de GPU.
- **Percentis p50/p95 por estágio** (`waterfall` em `/api/metrics`) — profiling de
  pipeline para saber **onde** o tempo vai antes de otimizar. É o mesmo instinto de
  otimizar custo/tempo de um job Databricks (Spark UI, skew, spill).

### 5. Observabilidade e DataOps
- **Métricas persistidas** (latência por estágio, decode tok/s) em SQLite, expostas
  em endpoint — telemetria de pipeline.
- **624 testes sem GPU nem rede**, rodando em CI a cada PR — testabilidade como
  restrição de design. Configuração 100% por variável de ambiente (128 knobs),
  reprodutível — **infra como configuração**, sem editar código para calibrar.

### 6. Orquestração e processamento assíncrono
- **`scheduler.py`**: loop de background persistente que dispara trabalho agendado
  (o racional de um **Airflow**: tarefas com hora marcada, recorrência, reentrega do
  que falhou). O ETL idle é o "job oportunista"; o scheduler é o "job com cron".
- Tudo I/O-bound passa por `asyncio.to_thread` — controle de concorrência explícito.

## Frases-ponte prontas (para entrevista e para o "Sobre" do LinkedIn)

- *"Construí um pipeline de ETL incremental de ponta a ponta — ingestão com
  detecção de mudança por watermark, transformação com deduplicação e controle de
  qualidade, e carga em duas engines (relacional e vetorial)."*
- *"Todas as minhas decisões de arquitetura foram validadas por A/B com métrica —
  tenho harnesses versionados que quebram o CI quando a qualidade regride."*
- *"Trabalhei com orçamento de recursos apertado, então otimização de custo e
  profiling por estágio (p50/p95) fazem parte do meu default, não do meu extra."*

## O gap honesto (e como fechar antes da entrevista)

O projeto **não** usa Spark/Databricks/Airflow/cloud. A XP é Databricks-first.
Recomendação: some ao seu portfólio um mini-projeto que use **PySpark + Delta Lake**
(pode ser local com `pyspark` + `delta-spark`, ou Databricks Community Edition, de
graça) reproduzindo o mesmo racional de ETL incremental. Aí a ponte deixa de ser
analogia e vira experiência direta. No CV, isso vira uma linha em "Estudando/Em
progresso" — honesto e proativo.
