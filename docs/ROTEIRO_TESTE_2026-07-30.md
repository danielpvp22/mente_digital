# Roteiro de teste — 2026-07-30

Complementa o [ROTEIRO_TESTE_TEXTO.md](ROTEIRO_TESTE_TEXTO.md) (que segue válido para
figura, legenda e os números do Bloco 4). Aqui só o que **mudou desde o seu último
teste** e nunca foi validado ao vivo: a ponte de vocabulário, a fusão reaplicada, o
reparo de idioma, e as três correções da PR #77 que foram mescladas sem você usar.

---

## Como rodar

```bash
python main.py
```

**Sem redirecionar.** O log se grava sozinho. `Tee-Object` no PS 5.1 grava UTF-16 e
estraga o arquivo — foi por isso que a telemetria passou a escrever direto.

### O que já está preparado

| botão | estado | por quê |
|---|---|---|
| `MENTE_RAG_DEBUG` | **ligado agora** | loga cada chunk recuperado com distância, arquivo e trecho — é o que deixa julgar a recuperação sem adivinhar |
| `MENTE_TRACE_ENABLED` | já estava ligado | timeline por turno em `dados/traces/` |

> Depois do teste, devolva `MENTE_RAG_DEBUG=false` — ele é verboso e a doc manda
> desligar em produção. Backup do arquivo original em `.env.bak-20260730`.

### Onde a evidência cai (cinco lugares, todos automáticos)

| arquivo | o que tem |
|---|---|
| `dados/logs/mente.log` | o log inteiro sem cor — **a fonte principal** |
| `dados/logs/turnos.jsonl` | 1 linha por turno: pergunta, resposta **como ficou na tela**, imagens com posição |
| `dados/traces/AAAAMMDD.jsonl` | timeline por turno: lock_wait, prefill, decode do produtor, síntese por frase, pico de VRAM |
| `telemetria_etl.db` → `chat_history` | pergunta + resposta + `conversa_id` |
| `telemetria_etl.db` → `metricas_latencia` | TTFT, TTFA, total, e por estágio: vad, stt, extrator, busca, prefill — mais `melhor_dist`, `relevante`, `sentinela`, `escalou_web` |

`http://localhost:8000/api/metrics` fecha o bloco `waterfall` com **p50/p95 por
estágio**. É ele que arbitra qualquer conversa sobre latência — não a impressão.

### As linhas que valem a leitura

```
[VOCAB]   ponte: 'topping' -> 'topping poda apical meristema auxinas'
[LOCAL]   melhor_dist=0.146 relevante=True
[LOCAL_DBG] termos='...' recuperados=40 validos=38 aterrados=11
[LOCAL_DBG]   dist=0.146 [Livro_auxinas_apical_dominance...] :: 'A dominância apical...'
[AGENT]   Fusão: passada Banco.
[AGENT]   Definicional com vault fraco (2 < 3 átomos) — escala pra web.
[LOCAL]   Expansão por página: N origem(ns) sem página ignorada(s).
[LATENCIA] rota=... extrator=..ms busca=..ms prefill=..ms TTFT=..ms TTFA=..ms total=..ms
```

---

## Bloco A — A ponte de vocabulário (o "topping" que virou pizza)

**Faça numa conversa NOVA.** É o teste principal.

1. **"o que é topping"**
2. **"e o fimming, qual a diferença?"**
3. **"o que é supercropping"**

**PASSA se:** responder sobre **poda / dominância apical / meristema** — nada de
comida. No log tem de aparecer `[VOCAB] ponte:` e depois `[AGENT] Fusão: passada
Banco`.

**FALHA reveladora:** se aparecer `Definicional com vault fraco (N < 3 átomos) —
escala pra web`, a ponte disparou mas não trouxe átomo suficiente. Copie o
`[LOCAL_DBG]` desse turno — ele diz exatamente quais chunks entraram.

> Medi offline, sem o servidor: sem ponte 0 átomos; com ponte no embedding, 11
> átomos e distância 0,146. O que **não** medi é a resposta gerada em cima deles —
> é isso que só você vê.

---

## Bloco B — A ponte não pode atrapalhar o resto

O risco de ampliar query é aterrar a pergunta errada. Estas **não** podem mudar de
comportamento:

4. **"quanto tempo leva a secagem"**
5. **"qual o melhor meio para enraizar estacas"**

**PASSA se:** `[VOCAB]` **não** aparece nesses turnos (sem jargão inglês, sem ponte) e
a resposta vem do vault normalmente. A 5 é a que já pegou o teto de verbosidade
fabricando cache miss — se voltar a falar de "areia grossa" em vez de "rocha-pó",
é regressão.

---

## Bloco C — Contaminação do turno anterior (PR #77, nunca testada ao vivo)

**A ORDEM IMPORTA — é assim que o defeito aparecia.** Mesma conversa, seguidas:

6. **"quanto tempo até a colheita depois da floração?"** (espere responder)
7. **"o que o livro diz sobre cultivo de tomate?"**

**PASSA se:** a 7 disser que não tem informação sobre tomate. **FALHA se** ela
inventar título de livro ou repetir o número da 6 ("6 a 9 semanas") — era exatamente
essa a alucinação.

8. **"explique melhor"** (logo depois de qualquer resposta)

**PASSA se:** entender que é continuação. Este é o caso para o qual o histórico
existe — o portão novo tem de deixar passar. Se ele responder "sobre o que?", o
portão ficou rígido demais.

---

## Bloco D — A fusão reaplicada (375 átomos, hoje)

Os números que a re-atomização tinha perdido e a fusão devolve:

9. **"qual a variação de pH aceitável?"** → espera-se **±0,5 ponto**
10. **"quantos dias para a folha reverdecer depois de corrigir a deficiência?"** → **3 a 5 dias**
11. **"me fale sobre lâmpadas HPS"**

A 11 é alvo específico: o átomo de HPS foi um dos fundidos hoje, e a versão antiga
saía com **inglês colado no meio** (*"Watt for watt… 7 percent…"*). **PASSA se** a
resposta vier inteiramente em português. Se aparecer palavra inglesa solta, copie a
frase — é o resíduo que a régua de frase não pega.

---

## Bloco E — Irmão de página (PR #77, nunca testada ao vivo)

12. **"como controlar o oídio?"**

**PASSA se:** `[LOCAL] Expansão por página: N origem(ns) sem página ignorada(s)`
aparecer com N > 0 **e** a resposta seguir coerente. Essa linha é a guarda nova
trabalhando: ela descarta os baldes de até 1.947 átomos que antes injetavam 12
notas aleatórias no contexto.

**Olho na figura:** você relatou a **mesma sequência de imagens** se repetindo entre
perguntas diferentes. Isso continua **em aberto e sem mecanismo identificado** — medi
e os baldes têm 0 figuras, então não era isso. Se repetir, `turnos.jsonl` grava cada
imagem com nome e posição: é o dado que falta.

---

## Bloco F — Boot e latência

13. Cronometre o **boot** até `[SERVER] Mente Digital online`.

Esperado **~12,4 s** (era 17,8). Se perguntar nos primeiros ~5 s depois do "online",
a MALHA ainda está montando em background — comportamento conhecido, não defeito.

14. Faça **qualquer pergunta** e olhe `[LATENCIA]`.

O TTS não deve mais dominar: em modo texto ele não sintetiza (PR #71). Se
`tts_total` vier alto num turno **digitado**, é regressão.

---

## Se só der para fazer cinco

**1, 2, 6→7, 11, 12.** Nessa ordem. Cobrem a ponte, a contaminação, a fusão e a
expansão por página — as quatro coisas mescladas sem validação.

---

## Avisos — para não confundir defeito velho com regressão

- **Reclassificação de figura** segue aberta, sem mecanismo achado.
- **Topping em inglês no vault**: a palavra só existe em notas de YOLO/Ollama. Se a
  resposta citar fonte estranha, é isso — e é o que a ponte contorna.
- A busca vetorial é **aproximada**: duas execuções idênticas podem trazer contagens
  levemente diferentes. Não leia diferença pequena como sinal.
- O primeiro turno pode cair numa `Passada de consolidação (sem sessão)` que começa
  antes de você conectar. Se um turno não produzir resposta, repita antes de anotar.
