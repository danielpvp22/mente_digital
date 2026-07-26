# Kit de teste manual — 2026-07-26

Perguntas para rodar no servidor (`python main.py` → http://localhost:8000) e ver
como o sistema se comporta depois da entrada das 1.735 figuras.

Os números da coluna **esperado** não são chute: foram medidos pelo mesmo caminho
que o app usa (`VectorStore.search` contra o banco real, 33.288 chunks). Se o que
você vir divergir muito deles, é sinal de que algo mudou entre a busca e a tela.

## Antes de começar

Para enxergar o que a busca decidiu, ligue o debug no `.env` e reinicie:

```
MENTE_RAG_DEBUG=true
```

No log, a linha que importa é:

```
[LOCAL_DBG] selecionados=N/M átomos (aterrados=..., vizinhos_malha=.../..., figuras=X/Y)
```

`figuras=X/Y` = X entraram no contexto, Y passaram o gate. Se X < Y, o orçamento de
caracteres cortou — é o comportamento correto, não um defeito.

---

## Bloco A — a figura deve aparecer

Perguntas cujo assunto os livros ilustram. **Esperado** = figuras que o gate aprovou.

| # | pergunta | esperado |
|---|---|---|
| A1 | como identificar deficiência de magnésio nas folhas? | ~5 figuras + 30 textos |
| A2 | qual o pH ideal do solo para cultivo? | ~6 figuras + 30 textos |
| A3 | o que são tricomas e para que servem? | ~4 figuras + 30 textos |
| A4 | quais pragas atacam a planta e como controlar? | ~12 figuras + 12 textos |
| A5 | como funciona a digestão nos animais? | ~4 figuras + 30 textos |
| A6 | como é a estrutura da célula vegetal? | ~4 figuras + 30 textos |

**O que observar, em ordem de importância:**

1. **A imagem aparece na tela?** Este é o teste de verdade. A figura só vira `<img>`
   se o modelo copiar o `![[Figuras/...webp]]` literal na resposta. O contexto manda
   isso explicitamente ("copie o `![[...]]` exatamente como está"), mas o prompt de
   sistema não foi alterado — se o modelo preferir descrever a figura com palavras,
   você lê sobre ela e não a vê. **É o ponto mais provável de falha do dia.**
2. A figura é a **certa** para a pergunta? Isso nenhuma medição minha responde.
3. A resposta ficou pior por causa das figuras? Em A4 elas ocupam metade do contexto.

---

## Bloco B — a figura NÃO deve aparecer

O gate é adaptativo, não cota: assunto fora dos livros tem que dar zero, sem nada
forçar imagem.

| # | pergunta | esperado |
|---|---|---|
| B1 | o que é o protocolo Stratum de mineração? | **0 figuras**, ~30 textos |
| B2 | o que é VRAM e por que ela limita o modelo? | **0 figuras**, ~30 textos |
| B3 | o que é TensorRT? | **0 figuras**, ~29 textos |

Se aparecer figura aqui, o limiar está frouxo: baixe `MENTE_FIGURAS_SCORE_CONFIDENT`
(hoje herda `rag_score_confident=0.16`).

Se o Bloco A vier vazio e o B também, o limiar está apertado demais — suba.

---

## Bloco C — o gate local × web

| # | pergunta | esperado |
|---|---|---|
| C1 | quais os meus compromissos de amanhã? | **não ancora** local (0 fontes) |
| C2 | quem ganhou a última eleição presidencial? | ancora com só **3 fontes** |

C2 é o caso interessante: 3 fontes fracas bastaram para o gate considerar Cache Hit.
Veja se a resposta é boa ou se ele deveria ter ido para a web — é exatamente o
"Cache Hit falso" que o `rag_score_confident` calibra.

C3: pergunte algo que **só** a web sabe (ex.: *"qual a cotação do dólar hoje?"*).
Espere: filler falado → busca web → resposta. Confirme que o sentinela
("não tenho informações suficientes") **nunca** é falado.

---

## Bloco D — agentes (palavra-mestre)

Estes não passam pelo pipeline de conhecimento; testam o fluxo isolado.

| # | comando | esperado |
|---|---|---|
| D1 | mestre, adiciona leite, farinha e ovos na lista de compras | 3 itens (o "e" interno não corta) |
| D2 | mestre, lê a lista de compras | os 3 itens |
| D3 | mestre, corrige para pão | troca o último item, não duplica |
| D4 | mestre, desfaça | reverte a última mutação |
| D5 | mestre, me lembra de tomar água daqui a 2 minutos | agenda + dispara falado em 2 min |
| D6 | mestre, o que eu fiz hoje? | trilha de auditoria |

D5 é o que vale esperar: o push falado chega sozinho, sem você perguntar nada.

---

## Bloco E — verbosidade

| # | pergunta | esperado |
|---|---|---|
| E1 | que horas são? | uma frase curta |
| E2 | me explica em detalhes como funciona a fotossíntese | resposta cheia |
| E3 | explica fotossíntese como se eu fosse uma criança | analogia do dia a dia, sem jargão |

---

## Bloco F — voz (se for testar com microfone)

- F1: fale uma frase curta (≤3s) e pare — o endpointing adaptativo deve fechar em
  ~0,7s em vez de 1,2s.
- F2: comece a falar **por cima** da resposta dele — barge-in deve cortar o áudio.
- F3: diga "pare" no meio de uma resposta longa — a parada por palavra deve funcionar.
- F4: fique em silêncio perto do microfone — nada deve ser transcrito (o filtro de
  fantasma do Whisper).

---

## O que me reportar

Para cada bloco, o mais útil é:

- a linha `[LOCAL_DBG] ... figuras=X/Y` do log;
- se a **imagem apareceu** ou só o texto;
- qualquer resposta que pareça inventada (o anti-alucinação falhando é mais grave
  que figura faltando).

Se A1–A6 trouxerem figura no contexto mas **nenhuma imagem na tela**, o conserto é
no prompt de sistema (`prompts.py`), não na busca — e nesse caso a busca está certa.
