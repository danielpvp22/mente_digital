# Guia de calibração (RAG / gate de relevância)

Todos os botões abaixo são lidos do arquivo `.env` na raiz (prefixo `MENTE_`, sem tocar
no código — ver [config.py](config.py)). **Edite o `.env` e reinicie `python main.py`**
(o `.env` é lido no startup). O `.env` está no `.gitignore` — não vai pro git.

## Ligar o diagnóstico primeiro

```dotenv
MENTE_RAG_DEBUG=true      # loga cada chunk recuperado (dist / fonte / trecho)
```

Com ele ligado, cada pergunta imprime no terminal:

```
[LOCAL]     melhor_dist=0.42 relevante=True ram=0
[LOCAL_DBG] termos='tensorrt yolo' recuperados=40 validos=12 aterrados=3
[LOCAL_DBG]   dist=0.310 [tensorrt_yolo.md] :: "o tensorrt acelera a inferência..."
[LOCAL_DBG] selecionados=8/12 átomos (aterrados=3, vizinhos_malha=0/0)
```

Essas linhas são a régua para calibrar tudo abaixo. **Desligue (`false`) em produção** —
polui o log.

---

## Os botões (com a conta base e como afinar)

### `MENTE_RAG_SCORE_CONFIDENT` (default `0.8`)
Distância abaixo da qual um match vale como Cache Hit **sem** casar keyword. Menor = mais
rígido (mais web); maior = confia mais no local. **Régua:** olhe `melhor_dist` no log
`[LOCAL]` — se boas respostas locais têm `melhor_dist` logo acima do corte, suba um pouco;
se lixo passa, baixe.

### `MENTE_ATERRAMENTO_IDF_MIN` (default `1.5`) — G3
Uma keyword só aterra a nota (léxico) se for **rara** (`idf = log(N/df) >= este mínimo`).
Corta o "OR sem peso" onde uma palavra comum casava a nota errada. Maior = mais rígido
(mais web). **Régua:** no `[LOCAL_DBG]`, se `aterrados=0` em perguntas que o vault deveria
responder, seu mínimo está alto demais → baixe. `0` desliga (volta ao OR simples).

### `MENTE_DEFINICIONAL_MIN_ATOMOS` (default `3`) — Part A + lever B
Numa pergunta definicional ("o que é X", "quem foi Y", "me explica Z"), o vault só é aceito
se cobrir o tema com **força**: `>=` N átomos **distintos** (`local.fontes`). Abaixo disso,
escala pra web. Pergunta pessoal (meu/eu/nosso) é excluída e sempre segue local.

**Conta base:** a base é Zettelkasten atômica (1 ideia/nota). Tema que você estudou de
verdade → **muitos** átomos (10–30); menção-piada/incidental → **1–2**. O corte separa os
dois.

| valor | efeito |
|-------|--------|
| `1`   | desliga o B na prática (qualquer match confia — o "Tarkov" volta a passar) |
| `2`   | tolerante (filtra só menção única) |
| `3`   | **default** — "tema desenvolvido", separa estudo de menção solta |
| `5`   | rígido (vai mais pra web) |
| alto  | quase toda definição → web (≈ Part A puro, ignora o vault) |

**Régua:** com `RAG_DEBUG`, uma definição escalada imprime
`[AGENT] Definicional com vault fraco (X < Y átomos) — escala pra web.` — o **X é a
cobertura real** do tema. Tema bom foi pra web? Baixe `Y` abaixo do X. Nota-piada respondeu
local? Suba `Y` acima da contagem dela. Botão on/off geral: `MENTE_ROTEAR_DEFINICIONAL_WEB`
(`false` = nunca roteia, cascata local normal).

### `MENTE_RAG_DEDUP_NEAR_JACCARD` (default `0.9`) — G6
Ao montar o contexto, um átomo cujo conjunto de tokens tem Jaccard `>=` este limiar vs. um
já escolhido é descartado (near-duplicate → economiza prefill/TTFT). Menor = poda mais
agressiva (risco de podar átomo legítimo); `0` ou `1.0` desliga (mantém só o dedup exato).
**Régua:** é o menos observável no log; `0.9` (quase idêntico) é seguro. Só baixe se notar
o mesmo fato repetido no contexto.

---

## Ordem crítica ao calibrar

`MENTE_ROTEAR_DEFINICIONAL_WEB` / `DEFINICIONAL_MIN_ATOMOS` decidem **antes** o roteamento
definicional; o `ATERRAMENTO_IDF_MIN` e o `RAG_SCORE_CONFIDENT` governam o **gate** de
quem responde local. Calibre um de cada vez, olhando o log, senão você não sabe qual botão
moveu o resultado.
