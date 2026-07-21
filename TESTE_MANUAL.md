# Manual de teste — implementações da sessão 2026-07-19

Tudo aqui é o que a suíte `pytest` (624 verdes) **não** cobre: precisa de GPU, microfone,
rede e do vault real. Faça na ordem. Cada bloco tem **passo → resultado esperado**.

**Pré-requisitos:**
- Rode pela env certa: `C:\ProgramData\miniconda3\envs\llama-omni\python.exe main.py`
- O `.env` já está com `MENTE_RAG_DEBUG=true` — os logs `[LOCAL]`/`[LATENCIA]` vão aparecer (é isso que a gente quer aqui; desligue em produção depois).
- Deixe o **terminal do servidor visível** — a maioria dos "resultados esperados" é uma linha de log.

---

## ✅ JÁ VALIDADO AUTOMATICAMENTE (por mim, 2026-07-19)

Rodei tudo que **não** precisa de microfone nem julgamento humano — não precisa refazer:

| Bloco | O que testei | Resultado |
|---|---|---|
| **0.1–0.5 Boot** | subi o servidor com o stack novo | ✅ sobe **sem erro**; logs `KV-cache q8_0` · `<modelo> pronto na GPU` · `Whisper 'large-v3-turbo' (cpu/int8)` · `e5 cuda prefixos q='query: ' p='passage: '` · malha 14.619 conceitos. ⚠️ Este boot foi com o Qwen2.5; **depois trocamos para o Qwen3-8B** — ele foi validado carregando pelo mesmo `LlamaManager` (4,2 s) e **sem vazar a tag `<think>`**, mas o boot completo do servidor com ele é o seu passo 0 |
| **0.6 VRAM** | `nvidia-smi` com o stack todo carregado | ✅ **8901 / 10240 MiB** com o **Qwen3-8B** (~1,3 GB livres). Com o Qwen2.5 eram 8062. Cabe — mas não sobra para pôr o Whisper na GPU |
| **1 Whisper turbo** | download + load + round-trip TTS→STT | ✅ baixa+carrega em 77s, transcreve certo. **STT ~0,8× tempo real na CPU** (4,1s p/ 5,1s) — se incomodar, `MENTE_WHISPER_DEVICE=cuda` |
| **2 e5 recuperação** | 12 perguntas reais + 4 controle pela busca | ✅ **12/12 reais respondem LOCAL** (dist 0.06–0.17); controle 0.13–0.21 |
| **2 resposta LOCAL** | dirigi "tensor rt + yolo" via WebSocket | ✅ respondeu **do vault** (`rota=banco`), texto substantivo |
| **2 pergunta GERAL** | dirigi "capital da Mongólia" via WebSocket | ✅ **foi pra web** ("não está nas suas notas — vou buscar… Ulã Bator"); **não** inventou de nota pessoal (anti-"Tarkov" ok) |
| **3 F4 timing** | linha `[LATENCIA]` + `/api/metrics` | ✅ `TTFT=1113ms` **`tok/s=85.2`** `n_tok=74`; `decode_tok_s_medio=85.2` populou ao vivo |

**Nota de calibração:** a busca com `0.16` é permissiva (marcou os controles como `relevante=True`), **mas** o roteamento definicional manda pergunta geral pra web ANTES do gate — então **`0.16` está ok na prática**. Só baixe p/ ~`0.13–0.14` se um dia vir uma nota pessoal respondendo pergunta geral.

---

## 🔲 FALTA VOCÊ TESTAR (precisa de microfone / voz / julgamento)

| # | Passo | Resultado esperado |
|---|---|---|
| **1.1** | No live, fale uma frase técnica com a **sua voz** | Transcrição fiel (turbo > `small`; o round-trip automático já foi bem) |
| **4.1** | `MENTE_MESTRE_WAKE=true`, restart, entre no live. Fale algo comum **sem** "mestre" | **Ignorado** — sem resposta (voz de outros não dispara) |
| **4.2** | Diga **"mestre, que horas são?"** | Acorda (toast "Acordei", orb "Ouvindo…") e responde. Log `[WS] Palavra-mestre acordou o live.` |
| **4.3** | Diga só **"mestre"** | "Ouvindo…", fica ativo, sem comando vazio |
| **4.4** | Acordado, fique **15 s** em silêncio | Dorme (orb "Dormindo — diga 'mestre'"). Log `[WS] Live dormiu…` |
| **4.6** | `MENTE_MESTRE_WAKE=false`, restart | Live responde tudo na hora, como antes |
| **5.1** | Resposta longa; **enquanto ele fala**, diga alto/perto "para" | Resposta **para** (barge-in) |
| **5.2** | Repita com ruído/voz **curta ou baixa** ao fundo | Resposta **continua** (não corta por fundo) |
| **6.1–6.4** | Texto; "mestre, adiciona X na lista"; "mestre, desfaça"; novo chat/histórico | Tudo funciona como antes |

> Os blocos **0, 2 e 3** do detalhamento abaixo já estão ✅ (tabela acima) — use-os só se quiser reproduzir na mão. Os blocos **1 (voz real), 4, 5, 6** dependem de você.

---

## 0. Pré-voo — o servidor sobe com tudo novo?

| Passo | Resultado esperado |
|---|---|
| 0.1 — Suba o servidor (`python main.py`). Na **1ª vez** ele baixa o Whisper turbo (~1,6 GB). Se travar/estagnar, pare e rode com `HF_HUB_DISABLE_XET=1`. | Sobe sem traceback. |
| 0.2 — Veja o log do embedding. | `[EMBED] Embeddings multilingues carregados (singleton, cuda, prefixos q='query: ' p='passage: ').` |
| 0.3 — Veja o log do Whisper. | `[WHISPER] faster-whisper 'large-v3-turbo' carregado (cpu/int8).` |
| 0.4 — Veja o log do KV-cache. | `[VRAM] KV-cache quantizado em q8_0.` |
| 0.5 — Veja o log do LLM. | `[VRAM] Qwen3-8B-Q4_K_M.gguf pronto na GPU.` (o log mostra o **arquivo real**, não um rótulo fixo) |
| 0.6 — Com uma conversa ativa, rode `nvidia-smi` noutro terminal. | Uso total **~8,5 GB / 10 GB** (cabe, ~1,4 GB de folga). Se passar de ~9,8 GB, veja "Se estourar" no fim. |

---

## 1. Whisper turbo (qualidade de transcrição)

| Passo | Resultado esperado |
|---|---|
| 1.1 — Entre no modo **live** (🎤) e fale uma frase com termos técnicos em PT-BR (ex.: "me explica o pipeline de RAG e o TensorRT"). | A transcrição na tela sai **mais fiel** que antes (menos palavra trocada), principalmente nos termos técnicos. |
| 1.2 — Olhe o `stt=..ms` no log `[LATENCIA]` da resposta. | Na CPU, ~algumas centenas de ms a ~1–2 s por fala. Se doer demais, considere `MENTE_WHISPER_DEVICE=cuda` (e remeça a VRAM). |

---

## 2. Embedding e5-base + gate recalibrado (0.16) — o coração do RAG

| Passo | Resultado esperado |
|---|---|
| 2.1 — Pergunte algo que o seu vault cobre BEM (ex.: "o que eu sei sobre treinamento de YOLO?"). | Responde **local** (sem ir à web). Log: `[LOCAL] melhor_dist=0.0X relevante=True` com dist **~0.09–0.16**. |
| 2.2 — Pergunte um conhecimento GERAL que NÃO está no vault (ex.: "qual a capital da Mongólia?"). | **Não** responde a partir de uma nota pessoal tangente (o problema "Tarkov"). Vai pra **web** (roteamento definicional) ou dá o sentinela e escala. |
| 2.3 — Faça 5–10 perguntas reais suas e observe a coluna `melhor_dist` no `[LOCAL]`. | Perguntas boas do vault: dist baixo (< 0.16). Perguntas fora: dist mais alto. |
| **Calibrar** (só se precisar) | Nota pessoal aparecendo em pergunta geral → **baixe** `MENTE_RAG_SCORE_CONFIDENT` p/ `0.14`. Pergunta legítima do vault indo à web → **suba** p/ `0.17`. Reinicie após editar. (Receita em `CALIBRACAO.md`.) |

---

## 3. F4 — Análise de tempo por estágio

| Passo | Resultado esperado |
|---|---|
| 3.1 — Faça qualquer pergunta e olhe o log ao fim da resposta. | Linha `[LATENCIA] rota=... stt=...ms TTFT=...ms tok/s=XX.X TTFA=...ms total=...ms n_tok=...` |
| 3.2 — Confira o `tok/s`. | ~**100 tok/s** (Qwen2.5 no 3080). É a velocidade real do decode. |
| 3.3 — Abra `http://localhost:8000/api/metrics` no navegador. | JSON com `latencia`: `{... "decode_tok_s_medio":~100, "stt_ms_medio":..., "ttft_ms_medio":..., "ttfa_ms_medio":..., "total_ms_medio":...}` |
| 3.4 — Compare `stt` (voz) vs `TTFT` vs `tok/s` entre perguntas. | Dá pra ver ONDE o tempo vai: transcrição, tempo até o 1º token, ou o decode. |

---

## 4. F3 — Palavra "mestre" acorda o live (modo Alexa)

**Ativar:** ponha `MENTE_MESTRE_WAKE=true` no `.env` e reinicie. Entre no live.

| Passo | Resultado esperado |
|---|---|
| 4.1 — Com o live aberto, fale uma frase comum **sem** "mestre" (ou peça pra outra pessoa falar). | **Nada acontece** — a fala é ignorada (é o objetivo: voz de outros não dispara). Sem resposta, sem bolha. |
| 4.2 — Diga **"mestre, que horas são?"**. | Acorda: toast **"Acordei — pode falar."**, orb vira "Ouvindo…", e **responde a hora**. Log: `[WS] Palavra-mestre acordou o live.` |
| 4.3 — Diga só **"mestre"** (sem comando). | Status **"Ouvindo…"**, fica **ativo**, mas **não** executa comando vazio. |
| 4.4 — Depois de acordar, fique **15 s em silêncio**. | Dorme: orb **"Dormindo — diga 'mestre'"**, toast idem. Log: `[WS] Live dormiu (silêncio)...`. Ajuste o tempo com `MENTE_MESTRE_SLEEP_SECONDS`. |
| 4.5 — Já dormindo, fale algo comum, depois "mestre ...". | Fala comum ignorada; "mestre..." **acorda de novo**. |
| 4.6 — Ponha `MENTE_MESTRE_WAKE=false`, reinicie. | Live volta a responder **tudo** na hora, como antes (sem dormência). |

---

## 5. F5 — Barge-in só do dono (Tier 1)

**Ativar:** só **recarregue a página** (mudança é no `index.html`).

| Passo | Resultado esperado |
|---|---|
| 5.1 — Faça uma pergunta de resposta LONGA e, **enquanto o assistente fala**, diga alto e perto "para" / "chega". | A resposta **para** (barge-in) após ~250 ms de fala sustentada; orb volta p/ "Ouvindo…". |
| 5.2 — Repita, mas com um som/voz **curto ou baixo** ao fundo (alguém falando longe, um estalo). | A resposta **continua** — não corta mais por ruído de fundo. |
| **Afinar** (no `index.html`, função `onaudioprocess`) | Seu "para" não registra → **baixe** `BARGE_RMS` (0.12→0.10) ou `BARGE_FRAMES` (4→3). Fundo ainda corta → **suba** os dois. |
| **Limite conhecido** | Uma voz de fundo **igualmente alta e perto** ainda pode cortar — distinguir a SUA voz é o **Tier 2** (speaker-ID), que ficou adiado. |

---

## 6. Regressão — nada do que já funcionava quebrou

| Passo | Resultado esperado |
|---|---|
| 6.1 — Chat por **texto** (sem voz). | Responde normal. |
| 6.2 — Comando-mestre por texto (ex.: "mestre, adiciona leite na lista"). | Executa a ação (lista/lembrete) como antes. |
| 6.3 — "mestre, desfaça" logo depois. | Desfaz a última ação. |
| 6.4 — Encerrar conversa / novo chat / histórico. | Funcionam; o idle consolida em segundo plano. |

---

## Se estourar / rollback

- **VRAM apertada** (> ~9,8 GB, travas): ponha o embedding em fp16 ou na CPU (`MENTE_EMBEDDING_DEVICE=cpu`, +~50 ms/query), ou o Whisper de volta em `small`.
- **RAG pior que antes**: calibre `MENTE_RAG_SCORE_CONFIDENT` (passo 2). Se quiser reverter tudo do embedding: restaure `D:\projetos\_mente_backup_etapa3\` sobre `banco_vetorial_cerebro/`, reverta as linhas da Etapa 3 no `.env`, `git checkout config.py rag.py`.
- **F3/F5 atrapalhando**: `MENTE_MESTRE_WAKE=false` (desliga o wake) e/ou `git checkout templates/index.html` (reverte o barge-in).
