# O que a RTX 5080 16 GB muda no Mente Digital

Análise escrita em **2026-08-03**, com a 3080 ainda na máquina, para responder três
perguntas: que **qualidade** a placa nova permite, que **velocidade**, e **o que fazer**
com a VRAM que sobra.

> **Método.** Tudo aqui está marcado como **MEDIDO** (número que existe no repo, no
> `.env`, nos traces ou que eu medi hoje), **CITADO** (spec de terceiro, com fonte) ou
> **EXTRAPOLADO** (conta minha, com o raciocínio à vista). Não há benchmark inventado.
> Onde só a placa nova responde, digo qual é o experimento.

---

## 0. ⚠ Leia isto antes de comprar: a placa não funciona no dia 1

**Trocar a placa e ligar o PC deixa o assistente quebrado.** Não é opinião — rodei o
verificador hoje, nesta máquina:

```
python scripts/verificar_stack_cuda.py --arch 120
```

| Componente | Veredito hoje (2026-08-03) | O que acontece com a 5080 |
|---|---|---|
| `torch` 2.5.1 (CUDA 12.1) | **NÃO COBERTO** — *zero PTX no binário* | **XTTS e embeddings morrem.** Sem PTX não há JIT: só reinstalando. |
| `cuBLAS` / `cudart` no processo | **NÃO COBERTO** — 12.4.5 | **Whisper fica mudo.** As libs math da NVIDIA não são forward-compatible. |
| `llama-cpp-python` 0.3.34 | JIT (cubins sm_60…sm_90, PTX sm_90) | Sobe por JIT, degradado. `FORCE_MMQ=1` ⇒ **não use CUDA 13.x**. |
| `ctranslate2` 4.8.1 | JIT (cubins até sm_86) | Depende do cuBLAS acima — cai junto. |
| `llama-server` b10107 (OCR) | JIT (cubins sm_86/sm_89) | Traz DLLs CUDA 12.4 **na própria pasta**, que vencem o PATH. |

Ambiente medido junto: driver **591.44**, RTX 3080 compute **8.6**, 10.239 MiB.

⚠ **O modo de falha é silencioso, e é o pior tipo.** O app sobe "saudável", responde
`/api/metrics`, e o microfone fica mudo **para sempre**: `audio.py` só arma o pára-quedas
de CPU dentro do `load`; o `transcribe` faz `telemetry.error` + `return ""`; e o `ws.py`
descarta texto com menos de 3 chars sem avisar ninguém. Você não vai ver um traceback —
vai ver um assistente que não te escuta.

**O roteiro completo, com gate por passo e rollback, já existe:
[UPGRADE_BLACKWELL.md](UPGRADE_BLACKWELL.md).** A ideia que o organiza é boa e vale
repetir: **dá para preparar e provar o upgrade inteiro com a 3080 ainda montada**,
compilando para `86-real;120a-real` e verificando que o código `sm_120` está lá dentro.
Isso transforma "compra e reza" em "prepara, verifica, compra". A seção 8 deste
documento traz a ordem do dia da troca.

---

## 1. As duas placas, lado a lado

| | RTX 3080 10 GB | RTX 5080 16 GB | razão |
|---|---|---|---|
| Arquitetura | Ampere GA102, **sm_86** | Blackwell GB203, **sm_120** | — |
| CUDA cores | 8.704 | 10.752 | 1,24× |
| VRAM | 10 GB GDDR6X | 16 GB GDDR7 | **1,60×** |
| Barramento | 320-bit | 256-bit | 0,80× |
| **Banda de memória** | **760 GB/s** | **960 GB/s** | **1,26×** |
| TGP | 320 W | 360 W | 1,13× |
| Conector | 2× 8-pin (típico) | **1× 16-pin** | — |
| FP8 / FP4 nativos | não | **sim** | — |

**CITADO.** 5080: [ASUS TUF RTX 5080 — tech specs](https://www.asus.com/us/motherboards-components/graphics-cards/tuf-gaming/tuf-rtx5080-16g-gaming/techspec/)
(10.752 CUDA cores, 16 GB GDDR7, 256-bit, 30 Gbps, boost 2.617/2.640 MHz, **PSU recomendada
850 W**, 1× 16-pin) e [Tom's Hardware](https://www.tomshardware.com/pc-components/gpus/nvidia-rtx-5080-allegedly-adopts-faster-30-gbps-gddr7-modules-delivering-960-gb-s-of-bandwidth-the-remaining-blackwell-lineup-is-expected-to-stick-with-slower-28-gbps-memory)
(960 GB/s a 30 Gbps). 3080 10 GB: 8.704 cores, 320-bit, 760 GB/s, 320 W — spec pública,
confirmada em [MSI](https://www.msi.com/Graphics-Card/GeForce-RTX-3080-GAMING-X-TRIO-10G/Specification)
e [ASUS TUF RTX 3080](https://www.asus.com/motherboards-components/graphics-cards/tuf-gaming/tuf-rtx3080-10g-gaming/techspec/).

**MEDIDO nesta máquina** (memória `blackwell-5080-toolchain-e-device0`, 2026-07-29): a
3080 puxa **317 W a 75 °C**, `power.min_limit=100 W`, PCIe 4.0 x16.

⚠ **Dois itens físicos que não são software.** (a) A 5080 usa **um conector 16-pin
(12V-2x6)** — confira se a sua fonte tem o cabo nativo ou se vai depender do adaptador
que vem na caixa; (b) a ASUS recomenda **850 W** de fonte. Nenhum dos dois aparece em
runbook de CUDA e os dois impedem o PC de ligar.

**O número que governa tudo abaixo é a banda: 1,26×.** Guarde-o. Não é 2×.

---

## 2. O que HOJE dói neste projeto (com evidência)

Antes de falar do ganho, o diagnóstico. **O `.env` desta máquina é uma crônica de fome de
VRAM** — três decisões de produção foram tomadas *contra* a qualidade, para caber em 10 GB.

### 2.1 O modelo em produção é o 4B, e a troca foi por VRAM

`MENTE_CAMINHO_MODELO_LLAMA=dados/modelos/Qwen3-4B-Instruct-2507-Q4_K_M.gguf`

O default do código é o Qwen3-8B. O `.env` o substituiu, e o comentário do próprio dono
diz por quê (**MEDIDO**, A/B congelado em `eval/ab_modelos.py`, 2026-07-21):

> "+43% decode (129,1 vs 90,6 tok/s), TTFT 242 ms (-38%) […] **O objetivo REAL da troca:
> liberar ~2,5 GB de VRAM para subir o Whisper turbo pra GPU**."

E o que o 8B entregava, e do que se abriu mão (**MEDIDO**, A/B de 2026-07-19):

> "o Qwen3 **LÊ muito melhor os átomos recuperados** — sentinela COM contexto na mão caiu
> de **33% para 8%** — e atomiza melhor (tags **25% → 50-62%**)."

⚠ Isto é o pilar do produto. O sentinela é `SENTINELA_INSUF` — a resposta *"não tenho
informações suficientes"*. Um terço das vezes, com o contexto certo na mão, o modelo
anterior não conseguia usá-lo; o 8B derrubou isso para 8%. **A troca por VRAM devolveu
parte desse terreno.**

### 2.2 O embedding foi expulso para a CPU por 474 MiB

**MEDIDO** (A/B do dono no `.env`, 2026-07-29, servidor inteiro no ar, mesmo turno real):

| | `embeddings=cpu` | `embeddings=cuda` |
|---|---|---|
| VRAM em repouso | 7.413 MiB | 8.833 MiB (+1.420) |
| **Pico num turno real** | 8.662 MiB | **9.766 MiB** |
| **Folga no pico** (de 10.240) | 1.578 MiB | **474 MiB** ← o preço |
| Embedding da query | 23,3 ms | **5,9 ms** (3,9×) |
| Lote (reindex) | 40,7 docs/s | **600 docs/s** (14,7×; 112 s → ~10 s) |

> "**FICA NA CPU.** Num turno isolado coube (9.766 < 10.240), mas 474 MiB de folga não
> sobrevive ao uso real […] cai no WDDM, que é **precipício de desempenho, não crash**."

E ainda: *"8B + e5 juntos deixam 1.373 MiB de folga, e nesse regime **eu vi 1 crash em 2
execuções**"*. Ou seja — **o 8B e o embedding na GPU já foram testados juntos na 3080 e não
cabem.** Não é teoria.

### 2.3 O KV-cache está quantizado e o contexto é o que sobra

`MENTE_KV_CACHE_TYPE=q8_0` — "~metade da VRAM de KV a custo de qualidade ínfimo […] libera
folga p/ o Whisper turbo/embeddings". Escolha de capacidade, não de qualidade.

**MEDIDO** (`.env`, A/B controlado no mesmo processo, 2026-07-31):

| | VRAM |
|---|---|
| sem modelo (desktop) | 930 MiB |
| `n_ctx=8192` | 3.502 MiB |
| `n_ctx=16384` | 4.110 MiB → **+608 MiB, 76,0 KiB/token** |

E eu confirmei a fórmula hoje, parseando o GGUF direto:

```
KV_MiB = n_ctx × n_layer × 2 × (n_head_kv × head_dim) × B_elem / 1048576   (q8_0 = 1,0625 B)
```

| n_ctx | q8_0 | f16 |
|---|---|---|
| 8.192 | **612,0 MiB** | 1.152,0 |
| 16.384 | **1.224,0 MiB** | 2.304,0 |
| 32.768 | 2.448,0 | 4.608,0 |
| 65.536 | 4.896,0 | 9.216,0 |

Os 612,0 MiB batem **exatamente** com o caso validado 5/5 registrado na memória do projeto.

🔑 **MEDIDO hoje, e é o achado que muda a conta do upgrade:** parseei os dois GGUF e o
**Qwen3-8B tem a geometria de KV IDÊNTICA à do 4B** — 36 camadas, 8 cabeças KV, head_dim
128 (4.096/32). Portanto **voltar para o 8B custa APENAS o delta de pesos (+2.413 MiB) e
nem um MiB a mais de KV-cache.** Isso torna a volta do 8B muito mais barata do que parece.

| Modelo | Arquivo | Pesos | KV @16k/q8_0 | Compute | **Total** |
|---|---|---|---|---|---|
| Qwen3-4B-2507-Q4_K_M | 2,497 GB | 2.382 MiB | 1.224 | ~302 | **3.908 MiB** |
| Qwen3-8B-Q4_K_M | 5,028 GB | 4.795 MiB | 1.224 | ~302 | **6.321 MiB** |

### 2.4 O que os traces dizem — e desmentem

**MEDIDO.** Agreguei `dados/traces/*.jsonl`: **3.379 turnos, dos quais 51 de voz completos.**

| Estágio (51 turnos de voz) | p50 | p95 | max |
|---|---|---|---|
| `ttfa_ms` (tempo até o 1º áudio) | **3.096** | 5.763 | 6.070 |
| `vad_ms` | 700 | 1.200 | 1.200 |
| `stt_ms` | **176** | **3.479** | 4.547 |
| `busca_ms` | 45 | 484 | 684 |
| `prefill_ms` | 422 | 766 | 891 |
| `decode_tok_s_gpu` | **114** | 131 | 141 |
| `tts_synth_ms_total` | **7.594** | 24.961 | 32.097 |
| `tts_synth_ms_max` (frase mais lenta) | **4.181** | 7.204 | 8.137 |
| `vram_peak_mb` (lado torch) | 2.949 | 3.068 | 3.096 |

Três conclusões que mudam a prioridade do upgrade:

**(a) O LLM não é o gargalo do turno falado.**
> **fração `(prefill + decode) / TTFA`: p50 = 12,5%**, máximo = 43,7%.

Dobrar a velocidade do LLM mexeria em ~6% do que o usuário sente. Isto **fecha
formalmente o critério (2) de reentrada do TensorRT-LLM** escrito em
[CONSULTORIA_TTFT.md](CONSULTORIA_TTFT.md) ("waterfall provando decode+prefill ≥ 40% do
TTFA-voz"): **12,5% ≠ 40%.** Não reabra.

**(b) O XTTS é o gargalo, e por uma margem absurda.**
> **fração `tts_synth_max / TTFA`: p50 = 136%.**

Leia de novo: a **frase mais lenta** da síntese sozinha demora **mais que o TTFA inteiro**.
O TTFA só é 3 s porque o chunker manda a primeira frase cedo — o resto da resposta vai
sendo sintetizado atrás, a ~7,6 s de síntese acumulada por turno (p50, 3 frases).

**(c) O `stt_ms` tem uma cauda de 20× que a mediana esconde.**
p50 = 176 ms (o Whisper na GPU funciona), mas p95 = **3.479 ms**. Isso é o `.env` avisando
em voz alta:
> "large-v3-turbo (~2 GB) + llama + XTTS pode raspar os 10 GB e **VOLTAR o spill WDDM** →
> OLHE o `stt_ms` no trace: se voltar a spikar (>5 s), o spill voltou."

**Ele voltou.** ⚠ Esta é a evidência mais forte a favor da troca em todo o documento, e
não estava sendo lida: o p95 do STT é **pressão de VRAM**, não lentidão de modelo. É um
sintoma que **a capacidade cura e a velocidade não**.

### 2.5 A GPU é serializada por um executor de 1 worker

`llm.py:243` — `ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-infer")`.
Uma thread ⇒ zero overlap de decode, por construção.

O XTTS **não** passa por ele. `tts_xtts.py` (docstring do módulo):
> "roda no PRÓPRIO `asyncio.to_thread` […] Rotear pelo executor do LLM **DEADLOCKARIA**: o
> `stream()` do LLM ocupa o worker único no turno todo. Consequência: na MESMA GPU do LLM há
> **contenção real (VRAM/compute)** — mitigada por fp16, mas **inerente**; o cenário ideal é
> GPU separada (`tts_xtts_device=cuda:1`)."

### 2.6 O OCR derruba tudo para poder rodar

`scheduler.py:212` `_executar_ocr` chama `ctx.liberar_vram()`, que **descarrega LLM, XTTS,
Whisper e embeddings**:
> "o OCR sobe ~3 GB próprios e **não caberia junto na 3080**. O `restaurar_vram` no
> `finally` é **OBRIGATÓRIO**: STT/TTS não auto-carregam, então sem ele **a voz voltaria
> muda em silêncio**."

Os arquivos: `DeepSeek-OCR-Q8_0.gguf` (3,126 GB) + `mmproj` (448 MB) = **~3,57 GB**, com
`ocr_n_ctx=16384` dividido entre 4 slots.

### 2.7 O governador de VRAM está estrangulando o trabalho de fundo

`vram.orcamento_tokens` calibra os tokens das tarefas de fundo pela fração livre:
`vram_frac_ok=0,35` (folga → 512 tokens), `vram_frac_min=0,08` (aperto → 128).

**EXTRAPOLADO** a partir do repouso medido (7.413 de 10.240 ⇒ `livre_frac` ≈ 0,28):
interpolando, o ETL hoje recebe ~**412** dos 512 tokens. Com 16 GB e o mesmo consumo,
`livre_frac` ≈ 0,55 ⇒ **512 cheios**, sem tocar em uma linha de código. O ETL passa a
destilar átomos menos truncados de graça.

---

## 3. Ganho de QUALIDADE

Ordenado pelo que o produto sente. Todos os números de VRAM são MEDIDOS; as velocidades
projetadas estão marcadas.

### 3.1 🥇 O Qwen3-8B de volta — pela velocidade que você já tem hoje

**A conta (EXTRAPOLADA, raciocínio à vista).** Decode em batch-1 é *memory-bandwidth-bound*
— foi o argumento que enterrou o TensorRT-LLM na consultoria ("seu decode é
memory-bandwidth-bound e o TRT-LLM não muda a banda da 3080") e o `.env` confirma com
número: *"o decode está a **69% do teto de banda da 3080**"*.

Verifiquei essa eficiência contra as três medições do projeto:

| Modelo | Pesos | tok/s MEDIDO (3080) | Banda efetiva | % de 760 GB/s |
|---|---|---|---|---|
| Qwen2.5-7B-Q4_K_M | 4,683 GB | 110,8 | 518,9 GB/s | **68,3%** ← bate com o "69%" do `.env` |
| Qwen3-8B-Q4_K_M | 5,028 GB | 90,6 | 455,5 GB/s | 59,9% |
| Qwen3-4B-2507 | 2,497 GB | 129,1 | 322,4 GB/s | 42,4% |

Se a eficiência se mantiver (conservador — Blackwell tende a igualar ou melhorar), tudo
escala por **960/760 = 1,263×**:

| Modelo | 3080 (medido) | **5080 (extrapolado)** |
|---|---|---|
| Qwen3-4B-2507 | 129,1 tok/s | **~163 tok/s** |
| **Qwen3-8B** | 90,6 tok/s | **~114 tok/s** |
| Qwen3-14B (est. 8,6 GB) | — | ~69 tok/s |

🔑 **O 8B na 5080 roda a ~114 tok/s. O p50 medido nos traces HOJE, com o 4B, é 114 tok/s.**

Ou seja: **você recupera o modelo que lê melhor os átomos (sentinela 33% → 8%) e atomiza
melhor (tags 25% → 50-62%), pagando ZERO em velocidade percebida.** É exatamente o trade
que o dono autorizou — "trocar milissegundos por qualidade" — só que aqui o preço em
milissegundos é **nulo**, porque a placa devolve o que o modelo maior consome.

E lembre de 2.3: **o 8B não custa nem um MiB a mais de KV.** Só +2.413 MiB de pesos.

### 3.2 🥈 O embedding e5-base de volta na GPU

A decisão "fica na CPU" foi tomada por **474 MiB de folga**. Na 5080 a mesma configuração
deixa **> 4 GB**. A decisão se inverte sozinha, e o ganho já está medido: **query 23,3 → 5,9
ms (3,9×)** e **reindex 14,7× (112 s → ~10 s)**.

⚠ O `.env` já tem o botão: `MENTE_EMBEDDING_DEVICE=cuda`. Mas o ganho de 17 ms por query é
o *menor* dos ganhos — o grande é o **reindex do vault**, que hoje é a operação que faz o
dono esperar. E ele já roda offline em GPU (`MENTE_EMBEDDING_DEVICE_OFFLINE`); o que muda
é que a passada **ao vivo** deixa de ser o gargalo.

### 3.3 🥉 Whisper `large-v3` inteiro (não o turbo)

Hoje: `MENTE_WHISPER_MODEL=large-v3-turbo` — escolhido porque é "qualidade ~large-v3 a
velocidade ~medium", isto é, **um compromisso de recursos**. Com folga, o `large-v3`
completo em `float16` é a melhor transcrição que o faster-whisper entrega.

⚠ **E há um efeito colateral do upgrade que empurra nessa direção sozinho:** o
`UPGRADE_BLACKWELL.md` avisa que, com o guard de `sm_120`, o `MENTE_WHISPER_COMPUTE_TYPE=int8`
do seu `.env` **será auto-convertido para `float16`** com log. Mais VRAM — irrelevante em
16 GB — e provavelmente mais rápido em Blackwell. Não é falha; é o comportamento esperado.
Só não se assuste ao ver o número de VRAM do Whisper subir.

### 3.4 Contexto maior e KV sem quantização

Dois botões que hoje estão em modo econômico:

| Mudança | Custo em VRAM | O que devolve |
|---|---|---|
| `n_ctx` 16k → 32k | +1.224 MiB | Mais átomos por resposta; menos fatiamento no ETL |
| `kv_cache_type` q8_0 → f16 | +1.080 MiB (@16k) | A perda "ínfima, mas real" do KV quantizado |

⚠ **`n_ctx` maior NÃO custa latência** — quem paga prefill é o prompt de fato enviado, não
o teto. Isso está medido e escrito no `.env`. O limite sempre foi VRAM.

### 3.5 Modelo de atomização grande nas passadas offline

`settings.caminho_modelo_atomizacao` já existe como override e hoje aponta para o mesmo
8B. Com a GPU inteira livre (o servidor fecha nas passadas offline), o teto sobe para a
**classe 24B** (ver 5.1). Atomizar é a tarefa que **constrói a base** — é onde um modelo
maior rende mais, e é 100% offline, então **latência não importa**. Este é o uso mais puro
de "troque ms por qualidade" que o projeto tem.

---

## 4. Ganho de VELOCIDADE

**Seja realista aqui.** A tentação é ler "5080" e esperar 2×. A banda diz 1,26×.

| Estágio | Hoje (MEDIDO, p50) | Projeção | Por quê |
|---|---|---|---|
| **Decode** (mesmo modelo) | 114 tok/s | ~144 tok/s (**1,26×**) | Bandwidth-bound. Teto duro. |
| **Prefill** | 422 ms | ~330 ms (**~1,25×**) | Compute-bound; 1,24× cores + arquitetura nova. |
| **XTTS (síntese)** | 4.181 ms/frase | **1,2–1,4×** (estimado) | GPT-2 autorregressivo (banda) + vocoder (compute). |
| **STT p50** | 176 ms | pouca mudança | Já é rápido. |
| **STT p95** | **3.479 ms** | **→ ~p50** (o maior ganho isolado) | Cauda causada por **spill WDDM**, que é capacidade. |
| **Boot** | ~26,6 s | igual | Dominado por IO de disco, não GPU. |

🔑 **O maior ganho de latência percebida não vem da velocidade da placa — vem da
capacidade.** Matar o p95 de 3,5 s do STT vale mais, para quem está falando com o
assistente, do que 30 tok/s a mais no decode. E ele sai de graça, sem tocar em código.

### O que NÃO muda com mais VRAM

Isto importa tanto quanto o que muda:

1. **A serialização continua.** `ThreadPoolExecutor(max_workers=1)` é decisão de
   arquitetura (garantir que dois decodes nunca coabitem a GPU), não consequência de VRAM.
   Com **uma** placa, continua um decode por vez. **Mais VRAM não dá paralelismo — dá
   espaço.** Trocar isso exigiria repensar barge-in, preempção do ETL e o `interactive_idle`.

2. **A contenção XTTS × LLM afrouxa, mas não some.** Eles nunca brigaram só por VRAM (os
   dois já cabem) — brigam por **SM e banda**, e continuam na mesma placa. A 5080 tem 1,24×
   cores e 1,26× banda, então a contenção diminui proporcionalmente. **Só GPU separada
   resolve de verdade**, e aí você cai nos 11 pontos device-0 (seção 8.4).

3. **O carregamento preguiçoso do XTTS continua valendo.** ⚠ Corrijo aqui uma entrada
   desatualizada do índice de memória, que lista isso como "plano aberto": **já está
   implementado e mergeado** (PR #72; `tts_carga_preguicosa: bool = True` em `config.py:90`,
   consumido em `main.py:115` e `:353`). O upgrade **rebaixa a prioridade** de qualquer
   ajuste fino ali — os 1,4 GB deixam de ser críticos —, mas **não torna a feature inútil**:
   ela ainda poupa ~17 s de boot numa sessão só de texto, e boot não é VRAM.

4. **O gargalo do turno falado continua sendo o XTTS.** 1,2–1,4× num item que responde por
   136% do TTFA é bom, mas não muda a natureza do problema. Se o dono quiser um salto real
   na latência de voz, o caminho é **arquitetural** (streaming de áudio do XTTS, ou uma voz
   mais barata para as frases curtas), não hardware.

---

## 5. Ideias: o que hoje não cabe e passaria a caber

### 5.1 O orçamento de VRAM, cenário a cenário

Componentes (MiB). Os marcados 🔬 são medidos no repo; os demais, derivados das fórmulas
validadas.

| Componente | MiB |
|---|---|
| Desktop / display | 🔬 930 (medido "sem modelo"; hoje o verificador mostrou 1.552 com apps abertos) |
| LLM 4B @16k/q8_0 | 3.908 |
| LLM 8B @16k/q8_0 | 6.321 |
| Whisper `large-v3-turbo` cuda | ~1.500 |
| XTTS-v2 fp16 (repouso) | 🔬 ~1.400 |
| XTTS-v2 **pico de síntese** | 🔬 ~2.949 (`vram_peak_mb` p50 dos traces — **o dobro do repouso**) |
| e5-base em cuda | 🔬 +1.420 |
| OCR (DeepSeek-Q8 + mmproj) | 🔬 ~3.573 |

| Cenário | Pico | Folga em 10.240 | Folga em **16.384** |
|---|---|---|---|
| **A** — hoje (4B, e5 na CPU) | 🔬 8.662 | 🔬 1.578 | **7.722** |
| **B** — 4B + e5 na GPU | 🔬 9.766 | 🔬 **474** ⛔ rejeitado | **6.618** ✅ |
| **C** — 8B + e5 na GPU | ~12.179 | ⛔ não cabe | **4.205** ✅ |
| **D** — C + `n_ctx` 32k | ~13.403 | ⛔ | **2.981** ✅ |
| **E** — D + KV f16 | ~15.563 | ⛔ | **821** ⚠ apertado |
| **F** — 8B + OCR juntos | ~10.824 | ⛔ | **5.560** ✅ (mas ver 5.3) |

⚠ **O cenário E é o aviso.** Os quatro upgrades **não cabem todos juntos**, e "821 MiB de
folga" é território conhecido: o dono já rejeitou 474 MiB e já viu **1 crash em 2
execuções** com 1.373 MiB. **A régua histórica desta casa é ~1,5 GB de folga.** Pare no
cenário C ou D.

**Teto de modelo** (densidade Q4_K_M = **587 MiB por bilhão**, validada em 3 amostras):

| Regime | Conta | Teto |
|---|---|---|
| LLM sozinho, GPU limpa (passada offline) | (16.384 − 930 − 1.224 − 302) / 587 | **~23,7 B** → classe 24B |
| LLM ao vivo, com XTTS+Whisper+e5 e 1,5 GB de folga | (16.384 − 930 − 1.400 − 1.500 − 1.420 − 1.224 − 302 − 1.500) / 587 | **~13,8 B** → classe 14B, no limite |
| **Ao vivo, confortável** | — | **8B**, com 4,2 GB de folga |

> A fronteira entre 16 e 24 GB é a **classe 32B — degrau, não gradiente**. A 5080 não a
> alcança. Se o objetivo fosse rodar um 32B, a placa é a errada.

### 5.2 O OCR sem descarregar tudo

Hoje uma passada de OCR **mata o assistente**: `liberar_vram()` derruba LLM, XTTS, Whisper
e embeddings, e a voz só volta no `restaurar_vram`. Em 16 GB, LLM 8B + OCR + desktop =
~10.824 MiB — **cabe com 5,5 GB de sobra**.

⚠ **Mas "cabe" não é o mesmo que "deve".** Três ressalvas honestas:
- Há um **veto explícito do dono**, citado no código: *"só quando nada do projeto estiver
  na VRAM, pra não misturar duas venvs"*. Isso é decisão dele, não limitação técnica.
- O OCR sobe **4 slots paralelos** e satura a GPU; um turno interativo concorrente ficaria
  lento mesmo cabendo.
- O ganho real **não é caber — é o OCR deixar de ser destrutivo.** Poder manter o Whisper e
  o XTTS vivos significa que uma passada de OCR não deixa mais o assistente mudo.

**Proposta mínima e segura:** manter o `liberar_vram()` do LLM, mas **parar de descarregar
STT/TTS**. Isso remove a causa de toda a classe de bug de "voz volta muda" documentada em
`state.py:485-495` — que já mordeu em produção uma vez — sem violar o veto de misturar as
duas venvs de inferência pesada.

### 5.3 Speculative decoding com um modelo DRAFT de verdade

Hoje `speculative_enabled=False`, desligado por dois defeitos MEDIDOS do
`LlamaPromptLookupDecoding` 0.3.34: mais lento em prompt curto (93 vs 121 tok/s) e **crash
de shape em contexto longo** — justo o caso RAG. O gate para religar é
`eval/retest_speculative.py`.

Duas coisas mudam:
1. **O upgrade força um binário novo de qualquer jeito.** É o momento natural de rodar o
   gate — o próprio `UPGRADE_BLACKWELL.md` já manda fazer isso no Passo 4.
2. **Com 16 GB, um draft model de verdade passa a caber.** Um Qwen3-0.6B (~400 MiB)
   rascunhando para o 8B é uma técnica diferente e muito mais robusta que o prompt-lookup.

⚠ **NÃO CONFIRMEI** que o `llama-cpp-python` 0.3.34 aceita um GGUF como `draft_model` pela
API Python — `_build_llama_kwargs` só instancia `LlamaPromptLookupDecoding`, e o
`llama.cpp` nativo suporta `--model-draft` via `llama-server`, que é outro caminho. **Trate
isto como hipótese a verificar, não como plano.**

### 5.4 Reabrir o TensorRT-LLM? **Não.**

Os quatro critérios de reentrada, contra a evidência:

| Critério | Status |
|---|---|
| (1) modelo congelado ≥ 1 mês | 🟡 o 4B-2507 está desde 2026-07-21 — ~2 semanas |
| (2) waterfall provando decode+prefill ≥ 40% do TTFA-voz | 🔴 **MEDIDO: 12,5% p50**. Reprovado por 3× de margem. |
| (3) spike de 1 dia via Docker com A/B real | ⚪ não feito |
| (4) GPU Ada/Hopper no horizonte (FP8) | 🟢 **Blackwell tem FP8 e FP4** |

O critério (4) abre, mas o (2) — o único que mede o que o usuário sente — **fecha com
folga**. E os outros argumentos da mesa continuam de pé: engine estática vs. modelo que
troca, Windows sem suporte nativo, e reimplementar barge-in/preempção/unload. **Minha
recomendação: mantido adiado.** Se algum dia reabrir, que seja pelo XTTS (que é 136% do
TTFA), não pelo LLM (que é 12,5%).

### 5.5 Medir joules por resposta — o experimento que esta branch habilita

Você está em `feat/wattimetro`, e `potencia.py` lê energia acumulada via
`nvmlDeviceGetTotalEnergyConsumption` (mJ). Isso funciona igual na 5080 — a `nvml.dll` vem
com o driver.

⚠ **A pergunta certa não é "quantos watts", é "quantos joules por resposta".** A 5080 tem
TGP 13% maior, mas termina antes. Se ela responde 1,26× mais rápido consumindo 1,13× mais
potência, **a energia por turno CAI ~10%**. Com o wattímetro já pronto, isso deixa de ser
especulação: **meça o mesmo conjunto de turnos antes e depois** e você tem o número real.
É o tipo de medição que só existe porque a instrumentação foi construída primeiro.

---

## 6. O trade-off, dito sem rodeio

Você **não** pode ter tudo ao mesmo tempo. As quatro melhorias de qualidade custam:

| Melhoria | Custo | Ganho |
|---|---|---|
| 4B → 8B | +2.413 MiB | sentinela 33%→8%; tags 25%→50-62% |
| e5 na GPU | +1.420 MiB | query 3,9×; reindex 14,7× |
| `n_ctx` 16k → 32k | +1.224 MiB | mais contexto; menos fatiamento |
| KV f16 | +1.080 MiB | perda "ínfima, mas real" do q8_0 |
| Whisper large-v3 fp16 | +~1.500 MiB | melhor transcrição |
| **Soma** | **+7.637 MiB** | |

Os 6.144 MiB extras da 5080 **não cobrem os cinco.** Cobrem confortavelmente os **três
primeiros** (5.057 MiB), que é o cenário D — e é o que eu recomendaria. O KV f16 é o de
menor retorno (o próprio `.env` chama a perda do q8_0 de "ínfima") e o Whisper large-v3
inteiro depende de o `large-v3-turbo` estar de fato incomodando, o que os traces **não**
mostram (p50 176 ms).

---

## 7. O que só se resolve MEDINDO depois da troca

Sou honesto sobre os limites desta análise. Cada item traz o experimento.

| Incerteza | Experimento |
|---|---|
| A eficiência de banda se mantém em Blackwell? | `eval/ab_modelos.py` com 4B e 8B; compare tok/s com os 129,1 / 90,6 de hoje. |
| Quanto o XTTS realmente acelera? | `tts_synth_ms_max` nos traces, mesmo conjunto de frases. É a métrica que mais importa. |
| O p95 do STT some mesmo? | `stt_ms` p95 nos traces após 50 turnos de voz. Se continuar >3 s, **não era spill** — era outra coisa. |
| Os kernels `sm_120` do ggml estão corretos? | ⚠ Há bugs conhecidos: store fora de faixa no epílogo MMA do `mul_mat_q` Q8_0 e Xid 43 no `flash_attn_stream_k_fixup`. Compilar nativo (`120a-real`) reduz a exposição, **não elimina**. Rode a suíte + turnos reais. |
| O XTTS sobrevive ao torch 2.8? | ⚠ Nunca foi executado sobre 2.7/2.8 — só os gates de versão foram lidos no fonte. Como é opt-in e fail-soft, uma regressão custa a voz, não o app. |
| Quanto custa o JIT em segundos? | Não medido. Pior caso absoluto: 300 s para os 932 MB inteiros. Compilando nativo, zero. |
| Um draft model GGUF funciona no binding Python? | Ler o fonte do `llama_cpp` instalado (5.3). |
| Energia por resposta cai? | `potencia.py` + o mesmo roteiro de turnos, antes e depois. |

---

## 8. O dia da troca — em ordem

### 8.1 ANTES de a placa chegar (com a 3080 ainda montada)

Este é o ponto alto do runbook e vale insistir: **quase tudo pode ser feito e provado
agora.**

```bash
# 1. baseline — guarde o JSON, o diff no fim é a prova do upgrade
python scripts/verificar_stack_cuda.py --arch 120 --json docs/stack_antes.json   # espera FAIL
python scripts/verificar_stack_cuda.py --arch 86                                  # controle positivo: PASS
python -m pytest -q                                                               # o que "não regrediu" significa
echo "$PATH" > docs/path_antes.txt                                                # rollback do Passo 1
```

Depois, os passos 1 a 6 do [UPGRADE_BLACKWELL.md](UPGRADE_BLACKWELL.md), **numa env nova**
(`conda create -n mente-blackwell python=3.10`) — nunca na `llama-omni`, que é a produção e
o rollback. Compile para `86-real;120a-real`: as duas arquiteturas no mesmo artefato,
então a 3080 continua funcionando enquanto você prova que o `sm_120` está lá dentro.

### 8.2 A ordem, e o que quebra primeiro

| # | Passo | Se pular | Como soa o defeito |
|---|---|---|---|
| 1 | **Tirar `D:\projetos\llama-omni\llamacpp` do PATH** | Todo o rebuild **não pega** | Silencioso. As DLLs 12.4 vencem o conda por nome. |
| 2 | CUDA Toolkit **12.8/12.9** (⚠ **não 13.x**) | Sem `sm_120` para compilar | `nvcc` ausente ou segfault de MMQ |
| 3 | **torch 2.8.0+cu128** (⚠ não 2.9+) | **XTTS e embeddings mortos** | App sobe **sem voz**; fail-soft esconde |
| 4 | llama-cpp-python `86-real;120a-real` | JIT lento (~5,7× no prefill, medição pública em 5090) | Lentidão, não crash |
| 5 | cuBLAS ≥12.8 **dentro de** `site-packages/ctranslate2/` | **Whisper mudo** | ⚠ **Totalmente silencioso** |
| 6 | Binário novo do OCR (release CUDA 12.8+) | OCR quebrado | `llama-server` morre no boot |

⚠ **Duas armadilhas de resolução de DLL que custam um fim de semana:**
- `pip install nvidia-cublas-cu12` **NÃO resolve o passo 5**. O `ctranslate2/__init__.py`
  faz `os.add_dll_directory` **só do próprio package_dir**. O jeito determinista é
  **copiar** `cublas64_12.dll` e `cublasLt64_12.dll` para dentro da pasta do ctranslate2.
- `ocr.py:210` faz `Popen(..., cwd=Path(comando[0]).parent)` **sem `env=`** — verifiquei
  hoje, continua assim. No Windows o **diretório do EXE vence o PATH**, então **consertar a
  env conda não conserta o OCR**. Tem de ser um binário novo.

### 8.3 Como saber que voltou a funcionar

Os gates do runbook (`verificar_stack_cuda --arch 120` PASS + suíte verde) **não bastam** —
eles não pegam a falha silenciosa. Suba o app e confirme os quatro serviços de GPU **um por
um**:

1. `/api/metrics` responde;
2. **fale no microfone** e veja a transcrição voltar **com conteúdo** (o modo de falha do
   STT é devolver `""` para sempre);
3. **ouça** uma resposta (XTTS `ready=True`);
4. **rode um ciclo de OCR** (`python scripts/ocr_agora.py`) — exercita o quarto binário;
5. confira `prefill_ms` e `decode_tok_s_gpu` sendo gravados; ⚠ **se `vram_peak_mb` vier
   nulo, algo mudou de device.**

**E o teste que só este documento propõe:** rode ~20 turnos de voz e agregue os traces.
Você tem a linha de base MEDIDA na seção 2.4 — `ttfa_ms` p50 3.096, `stt_ms` p95 3.479,
`decode_tok_s_gpu` p50 114. **Se o `stt_ms` p95 não desabar, a hipótese de spill WDDM estava
errada** e vale investigar antes de mexer em mais nada.

### 8.4 ⚠ Se você ficar com AS DUAS placas

Aí o roteiro **não é suficiente**. Verifiquei hoje, por amostragem, os pontos que a memória
do projeto lista — **continuam todos válidos**:

| Ponto | Verificado hoje | Consequência com 2 placas |
|---|---|---|
| `llm.py` `_build_llama_kwargs` | ✅ sem `main_gpu`/`split_mode`/`tensor_split` | ⚠ **O pior de todos.** O default do llama.cpp é **layer-split entre TODAS as GPUs visíveis** — ele fatia o modelo sozinho e devolve parte do LLM à 3080, disputando com o XTTS. O **oposto** do que a compra quer. |
| `audio.py:216,238` | ✅ compara `device == "cuda"` como **string** | `cuda:1` perde o `float16` **e desarma o pára-quedas de CPU** |
| `rag.py:143-152` `resolve_device` | ✅ só olha o booleano `cuda_available` | devolve `cuda:1` com uma placa só → `invalid device ordinal` |
| `vram.py:29` `mem_get_info()` | ✅ sem `device` | o governador calibra pela placa **errada** |
| `llm.py:281-290` `max_memory_allocated()` | ✅ sem `device` | `vram_peak_mb` vira ~0 em silêncio |
| `ocr.py:210` `Popen` | ✅ sem `env=` | não dá para pinar o subprocesso |
| `potencia.py:201` | ✅ NVML fixo no **device 0** | 🆕 o wattímetro desta branch mede só uma placa |

**O atalho que dispensa quase tudo isso:** `CUDA_VISIBLE_DEVICES` **por processo**. Os
trabalhos offline já são processos separados; falta `env=` no `Popen` do `ocr.py` (1 linha).
⚠ **Mas antes é preciso resolver o Chroma de escritor único** — `rag.py` e
`scripts/atomizar_agora.py` abrem o MESMO `persist_directory`, e o script recusa rodar com
o servidor no ar. **É software, não VRAM, que bloqueia "atomiza numa placa e conversa na
outra".**

**Se vender a 3080**, nada da seção 8.4 importa e o passo 4 fica mais simples:
`-DCMAKE_CUDA_ARCHITECTURES=120` — mais rápido de compilar.

---

## 9. Ranking: o que fazer no dia seguinte

Ordenado por **retorno ÷ esforço**.

| # | Ação | Esforço | Retorno | Por quê |
|---|---|---|---|---|
| **1** | **`MENTE_CAMINHO_MODELO_LLAMA` → Qwen3-8B** (+ `LLM_NO_THINK=true`, `LLM_STRIP_THINK=true`) | **3 linhas de `.env`** | 🟢🟢🟢 | Sentinela 33%→8% e tags 25%→50-62%, **a ~114 tok/s — o mesmo p50 de hoje**. O rollback já está escrito no `.env`. ⚠ Os dois botões `think` **têm** de voltar: o 8B é Qwen3 com raciocínio; sem o strip, a tag `<think>` vaza e o **TTS fala a marcação**. |
| **2** | **Medir 20 turnos de voz e agregar os traces** | 30 min | 🟢🟢🟢 | É o que valida ou refuta metade deste documento. Você já tem a linha de base da seção 2.4. Faça **antes** de mexer em mais botões. |
| **3** | **`MENTE_EMBEDDING_DEVICE=cuda`** | **1 linha** | 🟢🟢 | Decisão já medida e só rejeitada por 474 MiB de folga; agora sobram > 4 GB. Reindex 14,7×. |
| **4** | **Rodar `eval/retest_speculative.py`** | 1 h, env isolada | 🟢🟢 | O upgrade força binário novo de qualquer jeito. O gate já existe. Pode devolver decode de graça. |
| **5** | **`MENTE_N_CTX=32768`** | 1 linha | 🟢 | Contexto não custa latência (medido), só VRAM. |
| **6** | **OCR parar de descarregar STT/TTS** | ~10 linhas em `scheduler.py`/`state.py` | 🟢 | Remove a classe de bug "a voz volta muda", que já mordeu em produção. Respeita o veto de venvs. |
| **7** | **Modelo de atomização 14B/24B no offline** | 1 linha + uma passada longa | 🟢 | Melhora a **base**, que é o ativo do projeto. Offline ⇒ latência não importa. |
| — | ~~Reabrir o TensorRT-LLM~~ | — | 🔴 | **Reprovado pelos próprios critérios: 12,5% ≠ 40%.** |
| — | ~~KV f16 + Whisper large-v3 + tudo junto~~ | — | 🔴 | Cenário E: **821 MiB de folga**. A régua desta casa é ~1,5 GB. |

### A recomendação em uma frase

**Faça o #1 e o #2 no primeiro dia.** O 8B de volta é o único item que devolve qualidade
já medida, custa três linhas, e não cobra nada em velocidade — e os traces são o que vai
dizer se o resto deste documento estava certo.

---

## 10. Apêndice — o que eu não consegui confirmar

Listado para você não tomar decisão em cima:

- **Nenhum número da 5080 é medido.** Não existe Blackwell nesta máquina. Toda projeção de
  velocidade é o modelo de roofline da seção 3.1, calibrado com três medições reais do
  projeto na 3080.
- **A VRAM do Whisper `large-v3-turbo` tem três valores conflitantes no repo**: `~1 GiB`
  (nota do `n_ctx` no `.env`), `~1,5 GB` (docstring de `state.py:462`) e `~2 GB` (aviso de
  risco no `.env`). Usei 1.500 MiB. ⚠ Não medi — e como esse número entra em todas as
  contas de folga, vale medir com `nvidia-smi` antes de calibrar o cenário final.
- **Não sei se o `llama-cpp-python` 0.3.34 aceita um GGUF como `draft_model`** pela API
  Python (seção 5.3).
- **A eficiência de banda em Blackwell pode não ser a mesma da Ampere.** GDDR7 e o cache L2
  maior podem melhorar; MMQ com bugs conhecidos em `sm_120` pode piorar. Assumi *igual*, que
  é a hipótese conservadora para o ganho e otimista para o risco.
- **Não medi o pico de VRAM do OCR** — usei o tamanho dos GGUF (~3,57 GB) mais o que o
  código afirma (~3 GB). O KV dos 4 slots não está nessa conta.
- **`vram_peak_mb` só enxerga o lado torch** (`torch.cuda.max_memory_allocated()`). As
  alocações do llama.cpp e do CTranslate2 **não** aparecem ali. Por isso os 2.949 MiB do p50
  são XTTS em síntese, não o total do processo — e por isso as contas de folga desta análise
  usam os A/B do `.env`, que mediram a placa inteira.
