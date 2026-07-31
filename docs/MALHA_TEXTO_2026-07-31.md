# Malha do TEXTO — a passada das 6.324, e duas ideias medidas e descartadas

**2026-07-31.** Continuação de `scripts/melhorar_links.py` (que na véspera tratou as
1.751 figuras). Aqui ficou a outra metade — as notas de TEXTO —, mais duas construções
que foram levantadas, medidas e **revertidas** porque a medição não as sustentou.

O vault não é versionado (`.gitignore:21`). Este documento existe porque, sem ele, os
números abaixo não sobrevivem em lugar nenhum: o resultado da passada é uma mutação de
dados, não de código, e não aparece em `git log`.

---

## 1. A passada — feita

```bash
python scripts/melhorar_links.py --so-desconectadas --ancorar-dominio --aplicar
python scripts/reindexar.py     # obrigatório: a malha só chega ao índice depois
```

6.324 notas alvo (as com menos de 2 conceitos CONECTORES), 6.319 mudadas, **32,6 min a
0,31 s/nota** na GPU. Backup dos originais em `dados/backups/links_20260731_050014`.

| restrito às 6.324 | antes | depois |
|---|---|---|
| notas inertes | 2.155 (9,6%) | **75 (0,3%)** |
| conceitos conectores por nota | 0,66 | **3,48** |
| % com ≥2 conectores | 0,0 | **95,6** |
| conceitos solteiros (vault) | 69,1% | 50,1% |

O reindex confirma o efeito pretendido: o índice de conceitos caiu de **24.019 para
16.275** sobre os mesmos 24.515 átomos. Menos rótulo único, mais rótulo compartilhado —
que é a definição de sucesso deste script, não "mais links".

`--ancorar-dominio` **não é opcional** aqui, apesar do default OFF: sem ela o casamento
por token atravessa cultivo × dev (é o mecanismo que fez uma foto de aranha receber
`[[Detecção de Objetos]]`).

### O que ficou por investigar

**1.344 conceitos inventados (5,9%)** contra 3,6% da passada das figuras, e **944
conceitos de outro domínio em 616 notas (9,7%)** contra 3,3% das figuras. Quase o
triplo. Ninguém mediu se são ruins — só que são mais. E **75 notas seguem inertes**.

---

## 2. Conserto do "domínio magro" — construído, medido, REVERTIDO

**A hipótese.** A origem de uma nota consolidada é
`Consolidação de N átomos (a.md, b.md, …)` — embute a lista dos arquivos-fonte, logo é
única por nota. Como `dominio_da_origem` usava a string inteira como chave, **45 notas
viravam 45 domínios de UMA nota** (verificado aplicando a regra antiga literalmente).
Domínio de uma nota é pior que domínio nenhum: o vocabulário oferecido ao modelo passa
a ser a Malha da própria nota, o reuso é vazio por construção e tudo que ele responde
conta como invenção.

**O que se mediu antes de construir.** Agrupar pelo ASSUNTO dos arquivos-fonte foi
medido e não serve — deixaria **22 das 27 chaves ainda sozinhas**, porque em
`Sintese_capital_da_franca_…` o slug é o próprio tema. O que agrupa é o **tipo** do
fonte (`Livro_`/`LivroSintese_` contra `Sintese_`/`Lacuna_`/`Conversa_`): 45 ilhas → 2
domínios (21 de livro, 24 de conversa), nenhuma sozinha.

**Por que foi revertido.** A parte estrutural funcionou; o resultado nas notas, não:

| 44 notas reprocessadas | valor | passada principal |
|---|---|---|
| candidatos/nota | 10,1 | 19,6 |
| % com ≥2 conectores | 2,3 → **27,3** | 0 → 95,6 |
| conceitos inventados | **35%** | 5,9% |

E a **qualidade regrediu**, o que os contadores não mostravam:

- `[[pH]] [[Enxofre]] [[Solos]] [[Dose]]` → `[[Ação de Tomar]] [[Solos]] [[Ação de Pegar]]`
- `[[Nitrogênio]] [[Fósforo]] [[Potássio]]` → `[[Solo]] [[Cultivo]] [[Sinalização]]`
- a nota de Michael Jackson perdeu `[[Michael Jackson]]`, `[[Indiana]]`, `[[Estados Unidos]]`
- `[[Regulação genética]]` e `[[Adaptação visual]]` vazaram das notas de biologia
  vegetal para as de cogumelo e de clima — porque o conserto as pôs no mesmo balde

### As duas lições, que valem mais que o conserto

**"Conceito INVENTADO" não é sinônimo de "conceito RUIM".** Com domínio de uma nota só,
o modelo inventava a partir do texto da própria nota — e produzia `Enxofre`,
`Nitrogênio`, `Fósforo`: conceitos excelentes, que apenas não conectavam. O contador de
invenção media desconexão, não erro. **Não otimizar um contador sem olhar o que ele
conta.**

**Fundir domínios apaga a régua que enxerga contaminação entre eles.** Depois da fusão,
o script reportou **1** conceito de outro domínio enquanto a contaminação era visível a
olho nu — os dois domínios contaminados agora eram o mesmo domínio.

O problema real daquelas 45 notas não é a chave: **é conteúdo órfão** neste vault
(Michael Jackson, capital da França, LSD num acervo de cultivo). Agrupamento nenhum
conserta ausência de vizinho.

Revertido: 42 notas restauradas do backup `links_20260731_053358`, código e teste
desfeitos, suíte de volta a **1305**.

---

## 3. "Seis/sete graus de separação" — a ideia já estava cumprida

**A proposta.** Garantir que qualquer nota esteja a poucos saltos de qualquer outra,
gerando mais links, e manter os vizinhos "em standby" para entrarem na resposta.

**Método.** Grafo BIPARTIDO nota–conceito (2 passos bipartidos = 1 salto nota→nota,
custo O(arestas) em vez de O(clique)), BFS de 200 sementes com semente fixa, sem GPU.
Aresta = duas notas compartilham um conceito da Malha.

| medida | valor |
|---|---|
| mediana de saltos | **3** |
| p95 | **4** |
| máximo observado | 8 (diâmetro real ∈ [8, 14]) |
| componente gigante | **98,36%** (o 2º maior tem 4 notas) |
| pares a ≤7 saltos | **98,36%** — e 98,35% já a ≤4 |

Cortar os hubs (`df > 5%`) não muda a mediana: a conectividade vem da cauda, não do
`[[Cannabis]]`.

### A tensão âncora × alcance é CUSTO PURO, não trade-off

Ablação: removendo os **900 conceitos-ponte** entre os dois mundos, o vault **parte em
dois** (13.117 de balde + 8.600 de obra) — não há tecido conectivo por baixo. Mas as
pontes **não compram alcance**: dentro de cada mundo a mediana já é 2,61 (obra) e 3,02
(balde). Elas compram só contaminação — **74,9% das notas de cultivo já têm um vizinho
de dev a 1 salto**. E 73,2% dessas "pontes" têm ≤3 notas do lado menor: vazamento
acidental, não tema comum. As reais são `segurança`, `eficiência`, `custo`, `tempo`.

### O gargalo real

**A nota mediana tem 93 vizinhos a 1 salto** (sob a régua mais rígida do código,
`idf ≥ 4,0`) **e o orçamento de contexto é 8** (`malha_max_vizinhos`, `config.py:599`).
Doze vezes de excesso, em 93% das notas. O problema nunca foi ALCANÇAR o vizinho certo
— é ESCOLHER entre 93. Mais links pioram exatamente esse número.

### "Vizinhos em standby" já existe, e está desligado de propósito

- `MalhaIndex.vizinhos` — `rag.py:473`; chamada em `rag.py:1402`
- botão `malha_expandir = False` — `config.py:596` (o comentário ao lado registra o motivo)
- os testes em `tests/test_malha.py` e `tests/test_malha_proximidade.py` fazem
  `monkeypatch.setattr(settings, "malha_expandir", True)` — prova de que o default é off
- `centralidade` está LIGADA (`rag.py:947`), mas só reordena lotes do map-reduce da
  síntese sob demanda; **no pipeline normal de resposta nenhum vizinho de Malha entra**

**Conclusão:** o trabalho com número por trás é **afinar a seleção (93 → 8)**, não
aumentar a adjacência. Não foi feito.

---

## 4. Pendente

1. Os **9,7% de conceitos de outro domínio** da passada grande (contra 3,3% das
   figuras) — o número mais suspeito que sobrou.
2. As **75 notas ainda inertes**.
3. **Validação pelo navegador não completou** — a interface entrou em modo Live e o
   painel não estava sendo exibido. Não se afirma que passou nem que falhou.
