# Roteiro de teste por TEXTO — base Cannabis Encyclopedia

Escrito em 2026-07-29, depois das PRs #66–#69. Serve para julgar **por uso real**
o que até aqui só foi medido por teste cego com juiz único.

Base sob teste: 24.545 átomos + 1.817 notas de figura, só a Cannabis Encyclopedia
(Cervantes). Todas as legendas de figura estão em **português** desde a PR #67.

---

## Como rodar

```bash
C:\ProgramData\miniconda3\envs\llama-omni\python.exe main.py
```

Abra `http://localhost:8000`, use o **modo texto**, cole as perguntas na ordem.
Não precisa redirecionar nada: desde 2026-07-29 o servidor grava sozinho.

### As três fontes de evidência

| Arquivo | O que guarda |
|---|---|
| `dados/logs/mente.log` | o **comportamento**: rota, distância, verbosidade, figuras, latência — a mesma telemetria do console, sem cor e sem morrer ao fechar o terminal |
| `dados/logs/turnos.jsonl` | o que o **usuário viu**: um turno por linha, com a resposta exatamente como ficou na tela e cada imagem com sua posição em chars |
| `dados/telemetria_etl.db` | `chat_history(pergunta, resposta, conversa_id)` — o texto das respostas, por conversa |

Tudo sob `dados/`, que é gitignorado (carrega perguntas reais e trechos do vault).

Antes de 2026-07-29 nada disso existia: a telemetria só ia ao console e nem o
nome das imagens era registrado — descobrir *qual* imagem apareceu exigia
reencenar o turno na mão.

### Linhas que valem a leitura

| Linha | O que ela responde |
|---|---|
| `[FIGURAS] contexto: N candidata(s): nome 'legenda'…` | **quais** imagens a recuperação entregou e do que tratam — é aqui que se vê imagem sem relação com a pergunta e a mesma sequência repetindo |
| `[FIGURAS] inline: N nos chars [...] de T. -> nomes` | onde cada imagem caiu no texto |
| `[FIGURAS] N anexada(s) ao fim da resposta. -> nomes` | as que nenhuma frase casou |
| `[LOCAL] melhor_dist=… relevante=…` | atendida pelo vault ou escalada para a web |
| `[AGENT] Definicional com vault fraco (N < 3 átomos)` | o portão que manda direto para a web **sem** montar contexto local |

---

## Bloco 1 — Imagem INLINE (PR #69)

**Julgue:** a imagem aparece logo depois da frase que fala dela, ou empilhada no
fim? Cair tudo no fim não é crash — é o comportamento antigo.

| # | Pergunta | Por que esta | Figuras no acervo |
|---|---|---|---|
| 1 | Como sei se minha planta está com deficiência de nitrogênio? | legendas bem específicas ("essas mudas estão sofrendo de deficiência de nitrogênio") | 11 |
| 2 | Como faço um teste de pH do solo? | testa o corte de plural ("Testes de Solo" × "teste de solo"), que era o que derrubava o casamento | 33 |
| 3 | Qual o melhor meio para enraizar estacas? | rocha-pó/rockwool — legenda longa, casamento fácil | 66 clone / 13 estaca |
| 4 | Como faço o transplante de uma muda? | "Transplante: etapas a seguir" é **rótulo de seção** — o caso exato para o qual o limiar proporcional foi feito | 15 |

- 1 ▸
- 2 ▸
- 3 ▸
- 4 ▸

## Bloco 2 — Legendas em PT abriram busca nova (PR #67)

**Julgue:** a resposta encontra o assunto. Antes da PR essas legendas estavam em
inglês e o aterramento léxico não as alcançava (100 → 1.490 notas aterradas).

| # | Pergunta | Legenda que deve ser alcançada |
|---|---|---|
| 5 | O que são fungos micorrízicos e para que servem? | "fungos micorrízicos, disponíveis na forma de pó" |
| 6 | Como identificar ácaro da teia nas folhas? | "aphídeos, ácaros da teia e trips estão nesta folha" |

- 5 ▸
- 6 ▸

## Bloco 3 — As duas correções que viraram o placar

Teto de figura (15% do orçamento) + expansão por página (12 irmãos). Foram elas
que levaram a base nova de 18×31 para 10×4. As duas primeiras **foram medidas**
antes/depois — servem de regressão.

| # | Pergunta | Medido |
|---|---|---|
| 7 | O que é topping e quando devo fazer? | contexto 5.726 → 9.742 chars |
| 8 | Como prevenir e tratar oídio? | contexto 6.947 → 11.192 chars |
| 9 | Quais os passos para instalar as lâmpadas na sala de cultivo? | expansão por página: os passos irmãos devem vir juntos |

⚠️ **Na 8 não espere imagem** — o acervo não tem nenhuma figura de oídio. Julgue
só o texto.

- 7 ▸
- 8 ▸
- 9 ▸

## Bloco 4 — Os NÚMEROS que a re-atomização perdeu

O mais afiado da lista. O 8B fatiou mais fino e **separou o número do contexto**;
nos testes cegos a base nova perdeu exatamente estes fatos. Se voltarem, a fusão
+ expansão por página consertaram. Se não, achamos o alvo do próximo trabalho.

| # | Pergunta | Esperado (estava na base antiga) |
|---|---|---|
| 10 | Quanto tempo leva a secagem dos buds? | 5 a 14 dias |
| 11 | Quanto o pH pode variar sem prejudicar a planta? | ±0,5 ponto |
| 12 | Depois de corrigir a deficiência, quanto tempo a folha leva para reverdecer? | 3 a 5 dias |
| 13 | Quantas semanas de floração até a colheita? | 6 a 9 semanas |

Veredicto = **o número apareceu ou não**. Resposta correta mas vaga ("alguns
dias") conta como RUIM — é justamente o defeito sob investigação.

- 10 ▸
- 11 ▸
- 12 ▸
- 13 ▸

## Bloco 5 — Anti-alucinação e escalada para a web

| # | Pergunta | O que deve acontecer |
|---|---|---|
| 14 | Qual o preço do bitcoin hoje? | vault não tem → filler falado + busca web, resposta com fonte |
| 15 | O que o livro diz sobre cultivo de tomate? | deve **admitir que não tem**, não extrapolar de cannabis |
| 16 | *(logo depois da 7)* E isso atrasa a colheita? | o "isso" deve virar "topping" — testa o QueryOptimizer |

- 14 ▸
- 15 ▸
- 16 ▸

## Bloco 6 — Comportamentos do pipeline

| # | Pergunta | O que deve acontecer |
|---|---|---|
| 17 | O que eu sei sobre irrigação? | síntese sob demanda (map-reduce): panorâmica, não um átomo só |
| 18 | Qual o pH ideal para cannabis em solo? | governador de verbosidade → **1 frase** |
| 19 | Por que o pH afeta a absorção de nutrientes? | resposta **cheia**, explicativa |
| 20 | Me explica fotossíntese como se eu fosse uma criança | analogia do dia a dia, sem jargão |

- 17 ▸
- 18 ▸
- 19 ▸
- 20 ▸

## Bloco 7 — Palavra-mestre (opcional)

| # | Comando | O que deve acontecer |
|---|---|---|
| 21 | mestre, adiciona leite, farinha e ovos na lista de compras | os 3 itens numa lista só — o "e" **interno** não pode virar corte de comando |
| 22 | mestre, lê a lista de compras | os 3 itens de volta |
| 23 | mestre, me lembra de regar as plantas daqui a 10 minutos | lembrete criado com horário certo |
| 24 | mestre, desfaça | desfaz o lembrete da 23 |

- 21 ▸
- 22 ▸
- 23 ▸
- 24 ▸

---

## Avisos — para não confundir defeito velho com regressão

- **Tricomas**: só existe **1** figura no acervo. Imagem errada aí é bug já
  conhecido e ABERTO, não novidade da PR #69.
- **Acervo magro**: germinação (2 figuras), hidroponia (3), mofo (4), perlita (5),
  cura (6). Ausência de imagem nesses temas é esperada.
- Uma das 12 legendas geradas por LLM saiu fraca ("Pellets de clay expandido…
  uma substrato"). Sobra conhecida, reversível pelo campo `legenda_original`.

## Se só der para fazer 8

**1, 2, 4, 7, 10, 11, 13, 15** — cobre inline + as duas correções de recuperação
+ a fraqueza dos números + anti-alucinação.
