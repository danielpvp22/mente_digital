# Consultoria "Primeiro Token" — 10 especialistas contra uma cética (2026-07-21)

> **Resultado em uma linha:** 12 ideias aceitas (nenhuma sem critério de medição), 7 rejeitadas,
> e a minha própria ideia — migrar de llama.cpp/GGUF para TensorRT-LLM — foi a maior derrotada da mesa.

Eu sou a detentora do Mente Digital. Contratei a **Primeiro Token — Engenharia de Latência Ltda.**
("nós cobramos por milissegundo removido") para uma sessão de um dia: 10 especialistas, cada um com
uma tese de melhoria, cada um obrigado a me convencer. Eu entrei cética por método — este projeto já
enterrou uma migração (ExLlamaV3: medida, 1,8× mais lenta em Ampere, revertida) e já desligou uma
feature "óbvia" (speculative decoding: mais lento em prompt curto e crash em RAG) porque **mediu antes
de acreditar**. A régua da sessão foi essa.

## Contexto que a banca recebeu

- Stack: Qwen3-8B Q4_K_M via llama-cpp-python (flash-attn ligado, KV q8_0), e5-base nos embeddings,
  faster-whisper **large-v3-turbo na CPU** (int8, greedy, 8 threads), Piper na CPU, ChromaDB com
  ~12,9 mil chunks. RTX 3080 10GB: **8.901/10.240 MiB de VRAM ocupados** (~1,3 GB de folga).
- Números medidos: **~120 tok/s** de decode; **TTFT 15 ms** (prompt curto) / **441 ms** (RAG ~2k tokens);
  STT ≈ **0,8× o tempo da fala** (já depois do greedy+threads do painel anterior); reload pós-idle 1–2s
  (já mitigado: religa no 1º frame de fala).
- Anatomia do TTFA de um turno de VOZ com fala de 5s, hoje:
  `~1,2s (janela de silêncio do VAD) + ~4,0s (STT na CPU) + 0–0,9s (extrator, quando o gate léxico não poupa) + ~0,1s (busca) + ~0,4s (TTFT RAG) + ~0,3s (1ª frase + Piper) ≈ 6–7s`.
  Um turno digitado com resposta local fica **sub-segundo**. Essa assimetria guiou a sessão inteira.
- Regra 1: **não repropor** o que já foi feito ou está no backlog aprovado (13 ideias do painel interno
  de 5 especialistas, 12 já implementadas). Regra 2: **nada é aceito sem um antes/depois medível.**

## A bancada

| # | Nome | Especialidade | Lema |
|---|------|---------------|------|
| 1 | **Dr. Heitor Vasconcelos** | Inferência LLM & kernels de GPU | "Prefill é imposto; sonegue." |
| 2 | **Marina Sato** | STT & pipeline de captura de voz | "A latência que você não mede mora antes do primeiro token." |
| 3 | **Caio Bernardes** | Tempo-real, WebSocket & VAD | "O silêncio no fim da fala também é latência." |
| 4 | **Bianca Furtado** | TTS & percepção de latência | "O usuário não ouve TTFT; ouve TTFA." |
| 5 | **Rômulo Andrade** | RAG, embeddings & recuperação | "Contexto irrelevante é prefill pago à toa." |
| 6 | **Ícaro Menezes** | Concorrência & asyncio | "Espera em série é dívida; sobreponha." |
| 7 | **Ingrid Weber** | Sistemas, Windows & hardware (VRAM/CCD) | "O hardware que você comprou já é mais rápido que o software que você roda." |
| 8 | **Vera Lucchesi** | Dados, SQLite & higiene de estado | "Todo número mente até a fixture ser isolada." |
| 9 | **Leila Chammas** | Observabilidade & profiling | "Sem waterfall, otimização é astrologia." |
| 10 | **Otávio Nunes** | Qualidade, avaliação & anti-alucinação | "Milissegundo ganho com resposta errada é prejuízo." |

---

## Ata — os pitches e o interrogatório

### 1. Dr. Heitor Vasconcelos (inferência LLM)

**Pitch.** Duas teses. (a) **Reuso de prefixo de KV-cache**: hoje cada chamada re-avalia o prompt do
zero, e extrator, roteador e resposta nem compartilham o começo do prompt — se todos abrirem com o
mesmo prefixo estático longo (system core comum), dá para reaproveitar o prefill entre chamadas
(via `LlamaRAMCache`/mecânica do wrapper, a confirmar). Os 441 ms de TTFT em RAG são prefill quase
puro. (b) **Cache semântico de respostas prontas** (pergunta→resposta+áudio): TTFA ≈ 0 em pergunta
repetida.

**Interrogatório.** Sobre (a): "a confirmar na mecânica do wrapper" não é um plano, é uma esperança —
e o chat template pode quebrar o alinhamento do prefixo. Sobre (b): sou mono-usuária e a base **muda
no idle** (o ETL insere átomos, a promoção tira tag) — invalidação desse cache vira um projeto; a
memória fresca de sessão e o cache de voz LRU já cobrem o caso recorrente real.

**Desfecho.** (a) **aceita como investigação** com critério de kill: 1 dia para provar no bench que o
prefixo comum sobrevive ao template e economiza ≥50 ms/turno RAG, senão arquiva. (b) **rejeitada**.
Heitor ainda recuperou um item adiado: **bump do llama-cpp-python + reteste do speculative
prompt-lookup** (hoje OFF por bug de shape em contexto longo na 0.3.34) — aceito, é a "Etapa 5" que
eu mesma já tinha previsto, e prompt-lookup em RAG é o regime ideal dele (lossless, sem VRAM extra).

### 2. Marina Sato (STT)

**Pitch.** Abriu com o slide da anatomia: "vocês otimizaram o teclado num produto de **voz**. Numa
fala de 5s, o STT na CPU custa ~4s — é **o maior item individual do TTFA**, maior que tudo que o
resto da mesa vai propor somado." Proposta A: **transcrição parcial/incremental durante a fala**
(o STT vai comendo o áudio enquanto o usuário fala; ao silêncio, falta só o rabo). Proposta B:
**mover o Whisper turbo para a GPU em int8**.

**Interrogatório.** Proposta A é **não-feature intencional documentada** — STT parcial está
explicitamente fora do produto (README), e não é só UI: é a mesma maquinaria. Não reabro decisão de
produto por milissegundo. Proposta B esbarra na física: sobram ~1,3 GB de VRAM e o turbo int8 come
~1 GB — resta quase nada numa GPU que também segura o desktop.

**Réplica.** "A decisão de produto é sua e eu a respeito — mas então me dê a B como *spike* medido:
o `vram.py` já governa orçamento de VRAM, o KV já está em q8_0, e se der OOM em 1 dia de teste,
morre com honra. 4 segundos, Dona. É o item mais caro da casa."

**Desfecho.** A **vetada** (fica registrado o porquê). B **aceita como spike time-boxed** com
critérios de aborto (OOM/instabilidade/fragmentação com o desktop na mesma GPU), condicionada ao
waterfall da Leila confirmar o peso do STT (vai confirmar).

### 3. Caio Bernardes (tempo-real / VAD)

**Pitch.** "Todo turno de voz paga **1,2 segundo de silêncio obrigatório** antes de o STT sequer
começar (`vad_silence_seconds=1.2`, [ws.py](ws.py) `_check_silence`). Isso é maior que o TTFT de
vocês. **Endpointing adaptativo**: fala curta encerra com ~0,6–0,7s de silêncio; fala longa com pausa
de respiração mantém margem maior; teto atual vira fallback. E carimbem o estágio na telemetria."
Segunda ideia: comprimir o áudio do WS com Opus.

**Interrogatório.** Cortar cedo demais decapita frase com pausa retórica — e o custo de errar é
transcrever metade de um comando. Quanto ao Opus: o transporte é LAN, PCM 16k mono é ~256 kbps,
ninguém sente. Complexidade de codec dos dois lados por dor que não existe?

**Réplica.** "Por isso *adaptativo com telemetria*: medimos taxa de corte-precoce antes de apertar o
default. Knob no .env, como vocês sempre fazem. O Opus eu retiro — era ideia de portfólio, não de
dor."

**Desfecho.** Endpointing **aceito** (knob + métrica de cortes; rollout conservador). Opus
**rejeitado** — pelo próprio autor, ao fim.

### 4. Bianca Furtado (TTS / percepção)

**Pitch.** Duas cirurgias pequenas. (a) No caminho web, o filler falado é sintetizado **em série**
antes de a busca começar ([respostas.py:176](respostas.py:176) — `await _falar_status(...)` e só
depois `web.search`): dispare a busca como task primeiro e o filler mascara latência que está
**correndo em paralelo**, não parada. (b) **Primeiro chunk agressivo no SentenceChunker**: hoje a
primeira frase pode acumular até 180 chars antes do flush — em ~120 tok/s isso é mais de um segundo
de decode antes do primeiro áudio. Um `max_len` menor **só para o primeiro chunk** (ex.: corte na
primeira vírgula ou ~60 chars) derruba o TTFA; do segundo em diante, volta o normal.

**Interrogatório.** (a) é gratuita, aprovo no ato — minha única exigência é não perder o
short-circuit do `NENHUM`. (b) mexe em prosódia: cortar cedo pode soar picado.

**Réplica.** "O Piper pausa em vírgula — cortar em vírgula soa como respiração, não como gagueira.
E é um parâmetro com teste de unidade, não uma arquitetura."

**Desfecho.** **Ambas aceitas.** As duas mais baratas do dia.

### 5. Rômulo Andrade (RAG)

**Pitch.** Três. (a) **Reranker cross-encoder** pós-Chroma para precisão de contexto. (b) **Cortar
`rag_max_chunks`/orçamento de contexto** para reduzir prefill (TTFT RAG). (c) A constrangedora:
"o `dedup_dist_max=0.08` de vocês é **escala do MiniLM morto** — o dry-run do painel anterior provou
que na escala do e5 isso marcaria 75% da base como duplicata (p50 do vizinho cross-fonte = 0,045).
E não é só retroativo: o `_ja_no_banco` usa esse número **ao vivo** e está descartando átomo legítimo
**hoje**. Vocês estão perdendo conhecimento em silêncio desde a troca de embedding."

**Interrogatório.** (a): +80–150 ms e VRAM no caminho de TODA pergunta local, numa mesa cujo tema é
TTFT — e o e5 acabou de dobrar o MRR@10 (0,20→0,375); retrieval não é o gargalo medido. (b): a base é
Zettelkasten atômica — reunir dezenas de átomos é **decisão de produto** (estimei 10~30 por resposta),
e a expansão da Malha já foi desligada por medição (+48% de prefill); o sistema já poda o que provou
não pagar. Corte às cegas, não. (c)... aqui ele me pegou. Está pendente desde o painel de 5, com os
números prontos (limiar <0,01 = 57 fontes; <0,02 = 823) esperando **a minha amostragem**.

**Desfecho.** (a) **rejeitada** ("volte quando a qualidade de retrieval for o gargalo medido").
(b) **rejeitada como está** — reapresentável com dados do waterfall se prefill dominar.
(c) **aceita com prioridade de topo** — necessidade crítica, TTFT zero, e a pendência é minha.

---

### ⚡ O interlúdio TensorRT-LLM

No meio da sessão, entre um café e o sexto pitch, coloquei minha carta na mesa:

> **Eu:** "Já que estamos falando de usar melhor o hardware — pretendo aposentar o llama.cpp e os
> GGUF e **migrar para TensorRT-LLM**. Quero a 3080 rendendo o que ela tem para render."

A mesa inteira se virou. O interrogatório, pela primeira vez no dia, foi contra mim:

- **Heitor:** "A direção é certa; o veículo, errado, *agora*. Um: a 3080 é Ampere — **sem FP8**, o
  grande salto do TRT-LLM; sobra INT4-AWQ, cujo ganho sobre um llama.cpp **tunado** (flash-attn,
  120 tok/s, batch=1) é incerto e não medido. Dois: engine do TRT é **estática** — você trocou de
  modelo duas vezes numa semana (Qwen2.5→Qwen3) e testa GGUF da comunidade à vontade; cada troca
  viraria rebuild com calibração AWQ. Três: o custo escondido é o **entorno** — barge-in por
  `stop_event`, preempção do ETL, unload/reload do idle que devolve VRAM pro desktop; nada disso
  mapeia 1:1 no runtime do TRT-LLM. Você reimplementaria sua máquina de estados inteira."
- **Ingrid:** "'Melhor utilização do hardware' não é trocar o runtime: seu decode é
  **memory-bandwidth-bound** e o TRT-LLM não muda a banda da 3080. Onde há hardware ocioso DE VERDADE
  é a GPU parada 4 segundos enquanto o Whisper transcreve na CPU. Ataque isso (propostas 2B e a minha)
  e você 'utiliza o hardware' de graça. Detalhe: suporte nativo a Windows no TRT-LLM foi descontinuado
  — seria Docker/WSL2. O Dockerfile do PR #26 até abre essa porta, mas aí áudio, paths e vault
  atravessam a fronteira do WSL."
- **Otávio:** "A jurisprudência é sua: ExLlamaV3 media 1,8× mais lento e foi revertido; speculative
  crashava em RAG e foi desligado. *Medir salvou uma migração ruim* — está escrito no seu roadmap."
- **Leila:** "E sem o waterfall ninguém aqui sabe nem que fração do TTFA é decode. Se VAD+STT dominam
  — e os números de bancada dizem que dominam — dobrar tok/s muda quase nada do que o usuário sente."

**Desfecho.** Fui convencida — **mantido o adiamento**, agora com critérios de reentrada escritos:
(1) modelo congelado por ≥1 mês; (2) waterfall provando decode+prefill ≥40% do TTFA-voz;
(3) spike de 1 dia via Docker (pós-merge do PR #26) com A/B no hardware real;
(4) ou GPU Ada/Hopper no horizonte (aí o FP8 muda o cálculo — e reabre até o ExLlamaV3).
Em troca, levei da mesa o caminho principiado de cortar latência single-stream: a aceita nº 12
(bump + reteste do speculative).

---

### 6. Ícaro Menezes (concorrência)

**Pitch.** "A fase (b) que vocês deferiram no painel anterior: quando o gate léxico NÃO poupa o
extrator, a pergunta paga 0,3–0,9s de LLM **em série** antes da busca. O embedding e o Chroma nem
usam a GPU do LLM — **sobreponha** a busca (com a pergunta crua) à chamada do extrator e descarte se
ele reescrever a query. Com o gate (a) rodando, dá até para medir exatamente quantos turnos ainda
pagam isso." Segunda ideia: trocar o event loop (uvloop/winloop).

**Interrogatório.** A fase (b) foi deferida por ser a parte difícil: cancelamento correto no barge-in
com a GPU serializada — quero ver o desenho do cancelamento antes de uma linha de código. O event
loop: uvloop nem roda em Windows, winloop é imaturo, e **nenhum perfil nosso aponta o loop** — decode
é GPU-bound e IO já vive em `to_thread`.

**Desfecho.** Fase (b) **aceita** (com design de cancelamento revisado antes; o número do gate diz o
tamanho do prêmio). Event loop **rejeitado** — micro-otimização sem dor.

### 7. Ingrid Weber (sistemas/hardware)

**Pitch.** "Três fatos do seu 7950X3D: dois CCDs, um com 3D V-Cache e outro com clock mais alto, e o
`whisper_cpu_threads=8` de vocês nem escolhe onde roda — o scheduler do Windows joga as threads do
CTranslate2 de um lado pro outro. **Varrer 8/12/16 threads × afinidade por CCD × prioridade de
processo** é uma tarde de trabalho e tipicamente devolve 10–25% do `stt_ms`. É o plano B barato da
proposta da Marina: se o Whisper for pra GPU, descarta; enquanto não vai, é grátis." Ela também
co-assina o spike de VRAM da Marina (dona do orçamento: o que sai, o que entra, onde mora o risco de
fragmentação com o desktop na mesma placa).

**Interrogatório.** Curto. Experimento barato, reversível, com métrica pronta (`stt_ms` já é logado).
Minha única condição: nada de mexer em plano de energia/BIOS — knob de app, não de máquina.

**Desfecho.** **Aceita** (morre sozinha se a 2B vingar — e ela mesma escreveu isso na proposta).

### 8. Vera Lucchesi (dados/estado)

**Pitch.** "Vocês vão pendurar decisões de arquitetura nos números do `/api/metrics` — e a suíte de
testes de vocês **vaza 3 lacunas-fixture pro banco de telemetria real** (pré-existente, já
diagnosticado, chip aberto). Se o waterfall da Leila for lido de um banco sujo de teste, a mesa
inteira decide em cima de ruído. Isolar o DB nos testes + expurgar as 3 lacunas é higiene, não
feature."

**Interrogatório.** Nenhum. É verdade, está diagnosticado, e é pré-requisito de leitura limpa da
aceita nº 1.

**Desfecho.** **Aceita** — TTFT zero, necessidade de dados limpos.

### 9. Leila Chammas (observabilidade)

**Pitch.** "Vocês passaram o dia discutindo às cegas: a Marina *estima* 4s de STT, o Caio *sabe* dos
1,2s porque leu o config, o Heitor *acha* que prefill domina o RAG. O `LatencyTracker` já grava
TTFT/TTFA e o F4 já mede tok/s e `stt_ms` — falta **o waterfall**: carimbos por estágio
(fim-da-fala → STT → gate/extrator → busca → prefill → 1º token → 1º áudio) persistidos por resposta,
com **p50/p95** no `/api/metrics`. Meio-dia de trabalho, e toda briga futura desta mesa vira uma
consulta SQL." Segunda: `eval/bench_ttfa.py` — bench reprodutível com áudio sintético e web fake
(os fakes da suíte já existem), para o antes/depois de cada otimização aprovada aqui.

**Interrogatório.** Nenhuma objeção de mérito — só de escopo: waterfall no caminho quente não pode
custar latência. (Ela: "são `time.perf_counter()` e um INSERT que já existe. Se isso aparecer no
waterfall, eu como o relatório.")

**Desfecho.** **Ambas aceitas.** O waterfall é a **nº 1 do ranking** — pré-requisito que arbitra
todas as outras, inclusive o meu TensorRT-LLM.

### 10. Otávio Nunes (qualidade)

**Pitch.** Ele abriu dizendo que ia propor "o que todos pensaram e ninguém teve coragem": **subir o
`rag_score_confident` para responder mais do local** — menos web, TTFT menor no papel.

**Interrogatório.** Neguei antes de ele terminar a frase: isso **reabre o Cache Hit falso**, a
patologia fundadora deste projeto; o 0,16 foi calibrado com eval próprio na troca pro e5. Latência
comprada com resposta errada é regressão, não otimização.

**Réplica.** "Exato. Era um teste de coerência da mesa — e a senhora passou. Agora a proposta real:
**nenhuma das 11 aceitas entra sem rodar o guardrail de qualidade** no bench da Leila — taxa de
sentinela por rota, aterramento, WER em amostra fixa pro que mexe em STT/endpoint. O contrato de
aceitação é latência **E** qualidade, no mesmo relatório."

**Desfecho.** A provocação **rejeitada** (como planejado por ele); o guardrail **aceito e acoplado**
ao bench (aceitas nº 1 e nº 6 andam juntas).

---

## Veredito final

### Aceitas (12), ranqueadas por necessidade e impacto em TTFT/TTFA

| # | Ideia | Autor(a) | Necessidade | Impacto TTFT/TTFA | Dificuldade | Condição |
|---|-------|----------|-------------|-------------------|-------------|----------|
| 1 | Waterfall de latência por estágio + p50/p95 no `/api/metrics` | Leila | **Crítica** | Habilitador (arbitra todas) | Baixa | Não pode custar latência própria |
| 2 | Recalibrar `dedup_dist_max` p/ escala e5 (retro + `_ja_no_banco` vivo) | Rômulo | **Crítica** | Nulo | Baixa | Minha amostragem (57 vs 823 fontes) |
| 3 | Endpointing adaptativo do VAD (1,2s → ~0,7s c/ fallback) | Caio | Alta | **Alto** (−0,4–0,5s todo turno de voz) | Baixa-média | Telemetria de corte-precoce antes de apertar |
| 4 | Whisper turbo → GPU int8 (spike sob governador de VRAM) | Marina + Ingrid | Alta | **Muito alto** (−2 a −4s em fala longa) | Média | Kill: OOM/instabilidade; waterfall confirma peso |
| 5 | Isolar DB de telemetria nos testes + expurgar 3 lacunas-fixture | Vera | Média | Nulo (dados limpos p/ a nº 1) | Baixa | Chip já aberto |
| 6 | `eval/bench_ttfa.py` + guardrail de qualidade acoplado | Leila + Otávio | Alta | Indireto (contrato de aceitação) | Baixa-média | Reusa fakes da suíte |
| 7 | Filler ∥ busca web (hoje síntese em série antes do `web.search`) | Bianca | Média | Médio (−0,1–0,3s no caminho web) | **Trivial** | Preservar short-circuit do `NENHUM` |
| 8 | 1º chunk agressivo no `SentenceChunker` (corte ~60 chars/1ª vírgula) | Bianca | Média | Médio (−0,2–0,5s quando 1ª frase é longa) | Baixa | Ouvir prosódia antes de cravar default |
| 9 | Fase (b) do QueryOptimizer: busca ∥ LLM extrator c/ cancelamento | Ícaro | Média | Médio-alto (−0,3–0,9s nos turnos não-gateados) | Média-alta | Design do cancelamento revisado antes |
| 10 | Reuso de prefixo KV / layout de prompt comum | Heitor | Média | Médio (prefill RAG, 441ms) | Média | Investigação 1 dia; kill se <50ms/turno |
| 11 | Varredura threads/afinidade CCD/prioridade p/ STT-CPU | Ingrid | Baixa-média | Baixo-médio (10–25% do `stt_ms`) | Baixa | Morre se a nº 4 vingar |
| 12 | Bump llama-cpp-python + reteste speculative prompt-lookup | Heitor | Baixa-média | Médio via tok/s → 1ª frase mais cedo | Baixa | Só religa se o bug de shape sumiu |

### Rejeitadas (7) e por quê

| Ideia | Autor(a) | Motivo do corte |
|-------|----------|-----------------|
| STT parcial/incremental durante a fala | Marina | **Veto de produto**: não-feature intencional documentada no README; não se reabre decisão de produto por ms — a dor real foi endereçada pela aceita nº 4 |
| Migrar llama.cpp/GGUF → **TensorRT-LLM** agora | **Eu mesma** | Ampere sem FP8; engine estática vs. modelo que troca toda semana; reimplementar barge-in/preempção/unload; Windows só via Docker/WSL2; decode é bandwidth-bound. **Mantido adiado com 4 critérios de reentrada escritos** |
| Cache semântico de respostas prontas | Heitor | Hit-rate especulativo em mono-usuário; base muda no idle (ETL/promoção) → invalidação vira projeto; sessão + cache de voz já cobrem o caso real |
| Reranker cross-encoder pós-Chroma | Rômulo | +80–150ms e VRAM no caminho de toda pergunta local; e5 dobrou o MRR@10; contradiz o tema da mesa |
| Corte às cegas de `rag_max_chunks`/orçamento | Rômulo | Reunir dezenas de átomos é decisão de produto (Zettelkasten); Malha já foi desligada por medição — reapresentável com waterfall provando prefill dominante |
| Trocar event loop (uvloop/winloop) | Ícaro | uvloop não existe em Windows; loop não aparece em perfil nenhum (decode GPU-bound, IO em to_thread) |
| Subir `rag_score_confident` p/ mais cache-hit | Otávio (provocação) | Reabre o Cache Hit falso — a patologia fundadora; 0,16 é calibrado por eval. Latência à custa de resposta errada é regressão |
| Opus/compressão no WS | Caio (retirada pelo autor) | LAN, PCM 16k ≈ 256 kbps, dor inexistente; complexidade de codec dos dois lados |

*(São 8 linhas porque o Caio retirou a dele antes do veredito — registro mesmo assim.)*

### Sequência sugerida (dependências)

- **Semana 1 — medir e limpar:** nº 5 (DB limpo) → nº 1 (waterfall) → nº 6 (bench+guardrail). A partir daqui, todo número é confiável.
- **Semana 1, em paralelo (não dependem de nada):** nº 7 (filler ∥ web, ~30 min) e nº 2 (dedup e5 — só depende da MINHA amostragem, que devo desde o painel passado).
- **Semana 2 — os grandes de voz:** nº 3 (endpointing) e nº 4 (spike Whisper-GPU); nº 11 se o spike falhar.
- **Semana 3 — os finos de LLM:** nº 8 (1º chunk), nº 10 (prefixo KV, investigação), nº 9 (fase b), nº 12 (bump+speculative).
- **TensorRT-LLM:** dorme no roadmap com os critérios de reentrada. Se o waterfall disser que decode+prefill ≥40% do TTFA-voz, eu mesma reabro a discussão — com A/B, como manda a casa.

## Implementação (2026-07-21, mesmo dia)

As 12 aceitas foram implementadas na branch `feat/consultoria-ttft` — suíte **658 verdes**
(624 + 34 testes novos). Mapa rápido:

| # | Ideia | Onde ficou | Estado |
|---|-------|-----------|--------|
| 1 | Waterfall p50/p95 por estágio | `LatencyTracker.{vad,extrator,busca}_ms` + `Database.latencia_percentis` → bloco `waterfall` do `/api/metrics` | ✅ ligado |
| 2 | Dedup escala e5 | `MENTE_DEDUP_DIST_MAX` 0.08→0.01 ([config.py](config.py)) | ✅ ligado |
| 3 | Endpointing adaptativo | `ws.janela_endpoint` + log de corte-precoce; knobs `MENTE_VAD_*` | ✅ ligado (0,7s fala curta) |
| 4 | Whisper→GPU int8 | fallback GPU→CPU automático em `SttService.load`; spike = `MENTE_WHISPER_DEVICE=cuda` no .env | ✅ mecanismo pronto; **ligar o spike é decisão do dono** |
| 5 | Higiene do DB | conftest redireciona `MENTE_DB_TELEMETRIA` pré-import; 3 lacunas-fixture expurgadas (backup `.pre-expurgo-2026-07-21.db`) | ✅ feito |
| 6 | Bench + guardrail | `eval/bench_ttfa.py` (exit≠0 se guardrail quebrar) | ✅ rodado, verde |
| 7 | Filler ∥ busca web | `respostas._responder_web` | ✅ ligado |
| 8 | 1º chunk agressivo | `SentenceChunker(primeiro_max)`, `MENTE_TTS_CHUNK_PRIMEIRO_MAX_CHARS=60` | ✅ ligado — **ouvir a prosódia** e ajustar/0 se soar picado |
| 9 | Fase (b) overlap | `Agent._otimizar_e_recuperar` + `VectorStore.recuperar` + `pagaria_llm`; `MENTE_OPTIMIZER_OVERLAP` | ✅ ligado (fail-open) |
| 10 | Preâmbulo KV comum | `llm.montar_system` + `MENTE_PROMPT_PREAMBULO_COMUM` | ⏸ **off** por contrato — ligar só com A/B ≥50ms |
| 11 | Threads/CCD do STT | `scripts/bench_stt_threads.py` | 🔧 pronto — **rodar na máquina real** |
| 12 | Bump llama-cpp + speculative | `eval/retest_speculative.py` + nota-gate no requirements | 🔧 pronto — **rodar em env isolada** |

O que fica com a Dona: testar voz real (endpointing + prosódia do 1º chunk), rodar os
benches #11/#12, e decidir o dia do spike #4. O waterfall começa a acumular percentis
no primeiro uso — quando o TensorRT-LLM bater na porta de novo, o critério (2) de
reentrada já terá número.

## O que me convenceu (fecho da Dona)

Não foi retórica — foi **composição de evidência**: quem chegou com número da minha própria máquina
(Marina com os 4s de STT, Caio com os 1,2s do VAD, Rômulo com o p50=0,045 do dry-run) saiu com
aprovação; quem chegou com benchmark alheio ou moda de infra (reranker, uvloop, cache de respostas —
e o meu TensorRT-LLM) saiu com dever de casa. A ironia da sessão: contratei 10 especialistas para me
convencerem de melhorias, e a maior vitória deles foi me **desconvencer** da minha. O hardware vai
ser melhor utilizado, sim — começando pela GPU que hoje assiste, parada, o Whisper suar na CPU.
