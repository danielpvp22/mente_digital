# Relatório de Evolução — Mente Digital

**Período analisado:** 14 a 30 de julho de 2026 (17 dias)
**Base:** 259 commits (192 de conteúdo + 67 merges) e 70 pull requests, lidos integralmente — título e corpo.
**Gerado em:** 31 de julho de 2026

> Os números 32–38 e 42 da sequência de PRs são **issues**, não pull requests: são os defeitos numerados do "teste real do experimento 2507", citados como `closes #NN` nos corpos dos PRs #39–#41. Daí 70 PRs em vez de 78.

---

## Sumário executivo

Em 17 dias o projeto saiu de um script monolítico (`mvp_mente.py`) e chegou a um assistente de voz local com **51 módulos, ~19,3 mil linhas no pacote, 1.226 testes automatizados e CI com quatro portões de qualidade**. O crescimento é grande, mas não é a parte interessante: o que caracteriza a história é **o método**.

Três traços se repetem do primeiro ao último commit:

1. **Nada entra sem número.** Flash attention entrou com `+6-10% tok/s, -22% TTFT`. O embedding foi trocado com `MRR@10 0.20→0.375`. O modelo-base foi trocado porque o sentinela com contexto caiu de `33% para 8%`. E o inverso também vale: a **Malha por conceito foi construída, medida três vezes e desligada**; o **HyDE foi medido e reprovado** (melhorava a distância e piorava a resposta); o **EXL3 foi avaliado e descartado** (~67 tok/s contra ~120). São resultados negativos publicados no próprio histórico.

2. **O teste real é a autoridade final.** A suíte com fakes cresceu para mais de mil casos e mesmo assim quase todo bug grave veio do uso: os 46 segundos de congelamento por reindex no meio da conversa, os ~40 átomos que vazaram do modo confidencial, o "Obrigado" que o Whisper alucinava e virava nota, a IA respondendo ao eco da própria voz, as duas cópias do app segurando 10,76 GB numa placa de 10,24 GB.

3. **O erro de medição é tratado como bug de primeira classe.** Há commits inteiros dedicados a consertar a régua: o harness de eval que invertia certo e errado, o TTFT medido com ordem fixa que dava o resultado oposto ao real, o detector de inglês que acusou 463 falsos positivos, o relatório de auditoria que contava lista de compras como átomo fraco.

---

## A curva em números

| Data | LOC (produção) | LOC (testes) | Arquivos | Casos de teste | Módulos do pacote |
|---|---:|---:|---:|---:|---:|
| 14/jul | 1.761 | 0 | 17 | 0 | — |
| 15/jul | 2.455 | 608 | 33 | 56 | — |
| 16/jul | 3.291 | 893 | 34 | 80 | — |
| 17/jul | 7.560 | 3.131 | 55 | 269 | — |
| 19/jul | 12.313 | 6.882 | 111 | 560 | — |
| 21/jul | 14.545 | 8.821 | 151 | 674 | — |
| 22/jul | 15.596 | 9.954 | 163 | 758 | 35 |
| 25/jul | 19.473 | 12.297 | 201 | 895 | 42 |
| 28/jul | 25.138 | 14.267 | 231 | 1.090 | 47 |
| 30/jul | 27.654 | 16.086 | 253 | 1.224 | 51 |

Outros agregados da história completa:

- **53.940 linhas adicionadas** contra 6.171 removidas.
- **Média de 906 linhas tocadas por commit** — commits grandes, mas cada um com corpo explicando causa-raiz e medição.
- **Proporção teste/produção de ~0,83:1** (16.086 linhas de teste para 19.327 do pacote).
- **`config.py` é o arquivo mais tocado** (88 commits), seguido de `agent.py` (68) e `rag.py` (41) — coerente com um projeto cujo lema é "calibrar nunca exige editar código": os parâmetros saíram de 87 (18/jul) para **267 campos** (30/jul).
- **Dias de pico:** 17/jul (7.257 linhas), 19/jul (6.577), 20/jul (4.823), 25/jul (4.783).

---

## Era 0 — Fundação (14–17/jul, 29 commits)

> **Tema:** da refatoração à disciplina de medição.

O projeto nasce em `5919df3` quebrando o MVP monolítico em módulos, com `CLAUDE.md` de arquitetura já no commit inicial. O terceiro commit (`0d92a6b`) define o DNA da era: um bug real e sutil — tasks de background coletadas pelo GC porque o event loop só guarda referência fraca, matando `_prefetch`, o ETL idle e as syncs em silêncio — corrigido por um registrador central (`AppContext.track_task`) e acompanhado da **primeira suíte: 28 testes sem GPU nem rede**.

O bug fundador vem em `d62eddf`: o Chroma usava **distância L2 sobre embeddings não normalizados** (distâncias ~15) enquanto os thresholds do gate eram de escala cosseno. As duas escalas coexistiam em silêncio e o gate **rejeitava tudo** — o RAG local estava 100% inoperante. Com `hnsw:space=cosine` um bom match passa a ~0,26.

Em seguida: faster-whisper no lugar do `openai-whisper` (`e0c905a`); function calling **aditivo** com gate lexical, para que pergunta comum não pague o roteador (`3ec0c29`); tuning do llama.cpp com flash attention ON e speculative decoding **implementado e desligado por medição** (`aa1e003`); o RAG Zettelkasten com `top_k` 6→40 e fusão em cascata memória→banco→web (`7f3b144`); e o deep-fetch web com RAG efêmero mais o ciclo `#conhecimento_novo` (`1ee0b26`).

O dia 17 muda o método: deixa de ser construção e vira **diagnóstico sobre o vault real**. As descobertas são desconfortáveis e estão todas registradas:

- **`_consolidar_fontes` era no-op em 95% da base**: nenhum modelo entrega o formato do átomo de forma confiável — **169 de 177 átomos** não tinham a tag que a promoção procura. Conclusão adotada: *o LLM entrega a ideia, o Python impõe a estrutura*.
- **27% da base era lixo perecível**: 48 de 177 átomos auto-colhidos eram previsão do tempo e cotação, eternizados num Zettelkasten.
- **7.268 notas de import mecânico** envenenavam a recuperação; o import do Gemini **alucinava** ("a circunferência da roda de 700c é ~2,1 metros"), porque o prompt de síntese não tinha a regra anti-alucinação que o de resposta tinha desde sempre.
- **A confiança mentia**: 2.211 de 2.212 notas classificadas como fonte primária, porque a heurística olhava a subpasta errada.

E três resultados negativos publicados: a Malha por conceito, construída e medida (sentinela 10/19 → 10/19, TTFT 54ms → 264ms), foi **desligada**; o harness de eval foi consertado porque **estava invertendo certo e errado**; o HyDE melhorou a distância mediana (0,495 → 0,432) e **piorou** a resposta (sentinela 13/20 → 11/20), invalidando a métrica que vinha sendo otimizada.

**Testes:** 28 → 197.

---

## Era 1 — Os agentes e a palavra-mestre (17–19/jul, 38 commits)

> **Tema:** dar ao assistente a capacidade de agir, e blindar essa capacidade num canal separado.

O ponto de partida é um assistente que já *sabe responder*. Falta o primitivo de **responsabilidade contínua**: `e53044f` traz `agenda.py` (parser de tempo PT-BR puro, com o instante de referência injetado) e `scheduler.py` (loop de background sobre a tabela persistente `agendamentos`, com push falado às sessões vivas e reentrega na próxima conexão quando não há ouvinte). Sobre ele ligam-se os agentes tipo-Alexa: lembretes, watchers, briefing, listas.

`bc7292f` resolve *como acionar* isso sem contaminar o conhecimento: a **palavra-mestre**. Mensagem que começa por "mestre" vira comando por um caminho separado — regex determinístico primeiro, LLM só se necessário, e se nem o roteador achar uma ação o comando é **recusado** (isolamento rígido) e registrado como melhoria a cobrir.

Vêm então três **ondas numeradas**. A Onda 1 entrega o governador de verbosidade (puro, só léxico, zero custo de TTFA) e a síntese sob demanda em map-reduce. A Onda 2 é construída *sobre* o gatilho-mestre, e cada item reaproveita a fiação do anterior: desfazer, corta-e-corrige, cofre de confirmação, encadeamento falado, atalho de intenção frequente. Aqui aparece um princípio de design raro de ver escrito: **confirmação redundante é evitada de propósito** — só o que o undo *não* cobre é gateado.

A Onda 3 muda de assunto e volta à qualidade do retrieval, num arco de auto-correção em três iterações: IDF no aterramento léxico (`25429d8`, porque uma keyword comum bastava para validar o chunk errado) → pergunta definicional vai direto à web → pergunta definicional confia no vault **quando ele é forte** (`99b3f5c`, com botão numérico cujos extremos reproduzem os dois comportamentos anteriores).

A era fecha com a rodada mais pesada, que não é de features: `0d345c4` e `c09ccc4` trocam **a stack de modelos inteira**, cada peça medida por A/B — embedding MiniLM→e5-base (**~2x no ranqueamento, MRR@10 0.20→0.375**, com o gate recalibrado de 0.55 para 0.16), Whisper→`large-v3-turbo`, KV-cache q8_0 (**~metade da VRAM de KV**) e o modelo-base Qwen2.5-7B→**Qwen3-8B** (sentinela com contexto **33%→8%** por ~9% menos tok/s: num sistema cujo pilar é anti-alucinação, ler o contexto vale mais que 9% de decode). Adotá-lo exigiu um filtro de streaming — sem ele o TTS **falaria a tag `<think>`**.

**Bugs de uso real:** ponto duplo na fala da auditoria ("..à lista de compras.."), e o idle atomizando `"mestre, adiciona leite"` no átomo-lixo `"## Leite"` — o lado IA dos comandos já era gateado, o lado do usuário não.

**Testes:** 197 → 560.

---

## Era 2 — Publicável, modular, e o primeiro contato com o mundo real (20–22/jul, 59 commits)

> **Tema:** depois de se organizar para ser visto e se decompor em módulos governáveis, o sistema foi usado de verdade — e cada travada virou um conserto testado.

Três frentes em paralelo. A de **portfólio**: LICENSE Apache-2.0, CI no GitHub Actions rodando 624 testes por PR (viável porque os imports pesados são tardios e a suíte usa fakes — o job instala um `requirements-ci.txt` leve, sem torch, llama-cpp ou chromadb), README bilíngue e docker-compose.

A de **endurecimento** (`8db0fae`): 12 das 13 recomendações de um painel de especialistas — STT com threads e beam greedy, religar o LLM no primeiro frame de fala, exceções visíveis em tasks de fundo, token+Origin no WS, SQLite em WAL, fingerprint do índice, testes de propriedade. A 13ª virou um **achado registrado**: `dedup_dist_max=0.08` nunca fora recalibrado para o e5 — semente colhida no dia seguinte.

A de **refatoração**: `agent.py`, um deus-módulo de **2.472 linhas**, vira 6 módulos coesos. A disciplina é o marco: um commit por extração, código movido *verbatim* (funções puras, regexes e comentários-cicatriz preservados), e cada passo declarando `624 passed`. O núcleo fica em ~510 linhas. Uma lição registrada de passagem: os monkeypatches de namespace passam a mirar a casa nova do símbolo — *o rebind de nome só afeta o namespace onde o código lê o símbolo*.

No dia 21 vem a **Consultoria TTFT** (`2e4741c`): 12 otimizações de latência aceitas e implementadas de uma vez. A primeira delas é a que importa mais — **waterfall por estágio** (`vad/extrator/busca`, p50/p95 no `/api/metrics`), o árbitro de qualquer otimização futura. Junto vieram o endpointing adaptativo (fala curta encerra em 0,7s), o filler em paralelo com a busca, o primeiro chunk de TTS agressivo (60 chars), a recuperação vetorial em paralelo com o LLM do extrator, e — coerente com a cultura — o **preâmbulo comum de KV entregue desligado, com kill criterion explícito**: só cravar com A/B real ≥50ms/turno.

O dia 22 é dominado pelo **teste real**. O dono começou a usar o assistente de verdade e a realidade despejou uma fila numerada de defeitos que nenhum teste com fakes tinha visto:

- **#38, o mais caro:** escrita no vault por ferramenta disparava reindex + reconstrução da malha sobre ~13k átomos **na GPU serializada, durante a conversa**. Waterfall real: `total=46055ms`. *"O usuário repetia a pergunta 3x achando que travou."* A escrita saiu do caminho crítico.
- **#34, o mais grave:** `_finalizar_sessao` atomizava a sessão no disconnect sem checar o modo confidencial — **~40 átomos vazaram**.
- **#33, o mais divertido:** *"salva" é substring de "Salvador"*, então o gate lexical disparava e o roteador tentava criar um lembrete. Corrigido com veto declarativo.
- **#35:** o faster-whisper alucinava "Obrigado"/"Tchau" ao abrir o mic em não-fala, e isso virava átomo.
- Backchannel ("ok", "aham", "valeu") ativando o registro declarativo; barge-in que não cortava, resolvido com guard anti-eco no servidor (RMS 4× o VAD normal, porque sem AEC o próprio TTS captado pelo mic se auto-cortaria).
- Um átomo em vietnamita derrubando a busca inteira: `sys.stdout` em cp1252 no Windows estourava `UnicodeEncodeError` no meio do turno. Princípio adotado: *logging é instrumentação, não pode ter poder de quebrar o pipeline*.

A era fecha com a reorganização física do repo (462 imports em 122 arquivos reescritos) e a saga do XTTS-v2: quatro falhas encadeadas — coqui incompatível com transformers 5, `model.half()` quebrando o GPT-2 interno na 3080, e um crash em nível C (`Could not load symbol cudnnGetLibConfig`) porque o ctranslate2 carregava cuDNN 8 antes de o torch carregar cuDNN 9.

**Testes:** 560 → 767.

---

## Era 3 — Voz ao vivo e a ingestão de livros (23–25/jul, 45 commits)

> **Tema:** o sistema deixa de ser demonstrável e passa a ser usado; o uso real descobre o que fake nenhum acharia.

O **modo live** sai do papel: turno web de **~27s para ~10-12s**, web fetch de **~11s para ~3s** (`d4a09ce`). O motor foi o race-first-K no deep-fetch — dispara um pool, aceita os primeiros úteis, cancela o resto. Foi tão eficaz que criou o problema seguinte: a web voltando em ~3s fazia a ponte falada ("vou buscar...") **atropelar o próprio dado**, resolvido com uma carência de silêncio de 1,5s. E o desperdício do race virou feature: os fetches perdedores deixaram de ser abortados e passam a terminar em background durante a fala, virando conhecimento de graça.

O XTTS era simultaneamente o diferencial e a bomba-relógio. Dois achados quase idênticos e brutais: uma frase longa estourava o teto do GPT-2 interno e disparava **device-side assert que corrompia o contexto CUDA e derrubava o llama.cpp junto** — o TTS matava o LLM; depois, **duas sínteses concorrentes** faziam o mesmo, envenenando a GPU inteira (LLM, Whisper e processo).

E a contaminação mais memorável da era: **a IA respondendo aos próprios fantasmas** (`7fb3a9f`). O microfone captava o eco da própria fala, o Whisper alucinava "e aí", "obrigado", "buponte", e isso abria turnos novos. Solução: meia-duplex — enquanto a IA fala, o mic não abre turno; o único efeito permitido é o comando de parada por regex leve. No mesmo commit, o ETL pesado deixou de rodar no meio da conversa.

Em 24/jul um **painel de especialistas** auditou o projeto e produziu um backlog entregue em duas ondas. A semana 1 (`2910cea`) atacou o essencial: **backup diário** de vault + SQLite, porque *o vault era a única cópia do conhecimento destilado* — o dump bruto morre na atomização, logo disco morto = base morta; **anti-injeção na persistência do ETL** (a colheita enfileirava texto cru de página web, e um payload viraria átomo permanente); e UI 100% local, porque `fonts.googleapis.com` contradizia o "sem telemetria de terceiros". As semanas 2-3 trouxeram o **pacote sigilo**: em modo confidencial a escalada web passa a ser bloqueada — *a promessa "fica só nesta sessão" só agora é verdadeira* — validada por um teste-invariante que roda um turno sigiloso contra o DB real e afirma que nenhuma tabela de conteúdo cresce. E o CI ganhou ruff, cobertura com piso ratchet, bandit e pip-audit.

Sobre essa base veio o pedido "expert no livro", em cinco fases: PDF digital → capítulos → fila durável em disco → átomos com proveniência (Fase 1); consolidação de átomos quase-idênticos (Fase 2); colheita de PDFs acadêmicos e pasta vigiada (Fase 4); **OCR do livro escaneado** (Fase 3, a mais acidentada); e figuras em WebP vinculadas aos átomos (Fase 5).

O OCR rendeu a saga técnica mais dura do projeto: o `llama-mtmd-cli` fazia fail-fast com `0xC0000409` e **zero saída** em dois builds, migrado para `llama-server` (que carrega o modelo uma vez por lote em vez de 3 GB por página: **~2,8s/página**, 628 páginas em ~29min, depois ~20min com fila contínua por semáforo). No caminho, dois modelos errados adotados por inferência e corrigidos, um download silenciosamente incompleto, e um handle vazado do PyMuPDF que prendia um PDF inválido na fila **para sempre**.

E os achados do primeiro livro real, todos com número:

- **De 3 livros na fila, 2 já tinham camada de texto** — seriam ~4h de GPU para um resultado pior que o embutido.
- **"Figuras" que eram páginas inteiras:** num PDF escaneado a imagem embutida é a própria página. Amabis gerou 627 "figuras" para 628 páginas. **286 MB de retratos de página removidos do vault.**
- **GPU com picos e vales:** um capítulo processado em ~2s esperando ~18s pelo tick seguinte — 90% ocioso, com 82 capítulos na fila.
- **Fonte em inglês → átomo em inglês → RAG cego:** o gate exige interseção exata de tokens, e um átomo em inglês não casa nenhum token de pergunta em português. O prompt passou a exigir PT-BR com o termo técnico original entre parênteses, aterrando nos dois idiomas sem chamada extra de LLM.

Fecha com um validador de qualidade **determinístico, sem LLM-juiz** (reproduzível após mudar o prompt), que mediu malha canônica em 67% e, com o prompt corrigido, **97%**. E com uma auto-correção intelectual: uma afirmação anterior sobre a causa dos 99% de malha do import antigo foi verificada e **retratada** no próprio commit.

**Testes:** 767 → 895.

---

## Era 4 — Figuras, a enciclopédia e o custo de tudo isso (26–30/jul, 49 commits)

> **Tema:** fazer o conhecimento visual e o livro novo *chegarem* ao usuário — e pagar a conta em VRAM e boot.

A era abre abandonando uma heurística de pixel para detecção de figuras que já fora calibrada três vezes a olho e errava nos dois sentidos. O diagnóstico é honesto: *sem entender a página, nenhum limiar separa "diagrama de traço preto" de "coluna de texto"*. A saída foi reusar o DeepSeek-OCR com o token `<|grounding|>`, que devolve layout rotulado com a legenda já pareada: **1.736 figuras contra 777** da heurística.

Daí vem a cadeia inteira de "a figura existe mas não chega ao usuário", e cada elo foi um bug separado:

1. Indexá-las **estourou o SQLite** — `get()` sem limite vira um SQL com uma variável por registro. Dois pontos quebraram, um deles calado: o `sync()` parou de indexar **e ainda imprimia "Reindex OK"**.
2. Disputando as mesmas vagas do texto, a figura **perdia** (uma a 0,1239 ficava fora de um top-40 cujo pior era 0,1373) ou **vencia demais** ("como podar" gastava 16 das 40 vagas). Solução: espaço de busca próprio com filtro no Chroma, aprovado pela mesma régua do texto — *a figura ilustra, nunca ancora, mas promove*.
3. O embed literal ia parar **na fala** — a resposta sairia falada como "Figuras livro x p0288 f1 ponto webp".
4. Pedir ao LLM que copiasse o wikilink não funcionava sob teto de tokens; passou a ser o **servidor** que anexa a imagem: determinístico, custo zero de token, imune ao nível de verbosidade.
5. A legenda em inglês **não discriminava magnésio de manganês** numa legenda de ~5 palavras.
6. Traduzir as legendas deixou as figuras tão competitivas que ocuparam os 40 slots inteiros e uma pergunta bem coberta pelo vault **foi para a web**. Corrigido com corte relativo à melhor figura da própria pergunta.

A ingestão da **Cannabis Encyclopedia** (30 capítulos, 1.817 figuras, 5.907 átomos) trouxe a precedência entre obras — cuja primeira versão **aposentou 76 notas indevidamente em produção** e foi corrigida para exigir relação declarada nos dois sentidos, nunca inferida da semelhança. Seguiram-se passadas de saneamento: átomos que saíram em inglês (detector que acusou 477 com **463 falsos positivos** — o número real era 3,1%), o glossário de cultivo depois que o modelo 4B traduziu *bud* como "borda" e "borboleta", 2.991 corpos envoltos em `<...>`, e a troca para o **8B nas passadas offline** (o modelo do servidor foi escolhido por latência; a atomização não tem ninguém esperando).

O episódio mais instrutivo: três testes cegos deram **17 a 10 para a base antiga**. O que faltava na nova não era cobertura, e sim **dado duro** ("5 a 14 dias" de secagem, "±0,5 ponto" de pH) — ao fatiar mais fino, o 8B separou o número do seu contexto. A resposta não foi escolher uma edição, foi **fundir**: a nova como espinha dorsal, a antiga enriquecendo-a por dentro. E o teste cego seguinte deu 5 a 3 — com a nota registrada de que *"a troca está no ruído"* (n=8, juiz único).

Três correções de recuperação fecham o ciclo: teto de orçamento para nota de figura (ocupavam **40% dos 12.000 chars**, e numa pergunta o texto que respondia não coube e o modelo disse "não tenho informações suficientes" **com 18 átomos recuperados**); expansão por página, trazendo os irmãos do átomo que casou; e a escrita dos átomos que a atomização descartou, achados pela fonte comparando dados duros — *número não se traduz*.

E o commit mais elegante da era (`fd00a80`): a legenda em português **já estava no vault**, na síntese do capítulo, enquanto a nota da própria figura seguia em inglês. Bastou copiar pelo par de arquivo — sem gastar ~30 min de GPU nem pôr no vault um texto novo que ninguém conferiu. **Aterramento léxico: de 100 para 1.490.**

A frente final nasce de uma medição banal: em 19 turnos reais por texto, a síntese de voz consumia **~95% do relógio de cada turno** — o servidor sintetizava um áudio que ninguém ia ouvir. O sinal para decidir isso já existia e nunca fora ligado. Isso encadeou quatro entregas: turno digitado não fala → o XTTS não precisa subir no boot (−17s, −1,4 GB de VRAM) → mas então a primeira fala paga 20,3s, então monta-se o modelo em RAM e só o `.to(cuda)` (1,0s de 20,28s) fica para a hora da voz → e essa pré-montagem em background criou uma **corrida de import que matou o RAG em silêncio** (duas threads importando árvores que se cruzam; o CPython entregou um módulo pela metade). O app subia "saudável em 9,2s" **sem RAG nenhum** — e os 9,2s eram falsos.

No meio disso, a explicação do "vazamento de VRAM" que o dono via: o uvicorn só reserva a porta **depois** do lifespan, então uma segunda instância carregava tudo (~45s, ~4,7 GB), escrevia "Mente Digital online", só então descobria a porta ocupada e ficava zumbi. **Duas cópias × 4,67 GB + 1,42 GB de desktop = 10,76 GB numa placa de 10,24 GB.** Das três linhas "online" no log, duas eram de processos que nunca atenderam uma requisição. Uma checagem de porta de ~0,2ms no topo do lifespan resolveu.

**Boot: 31,7s → 17,8s → 12,4s. Testes:** 895 → 1.222.

---

## A história contada pelos pull requests

Os 70 PRs somam **+64.669 / −11.157** linhas brutas, em 998 arquivos e 238 commits — mas o número bruto superestima: três pares carregam diffs byte a byte idênticos (#14/#15, #20/#22, #27/#28) e o #62 re-entrega ao master o que os #58–#61 já contavam. Descontando as duplicatas, o volume real fica em torno de **+59.000 / −9.100**.

| Métrica | Valor |
|---|---|
| Mesclados / fechados sem merge | 69 / 1 |
| Média por PR | +924 / −159 |
| Mediana por PR | +452 |
| Maior em linhas | #21 (Onda 3 completa) — +6.461/−534, 69 arquivos, 36 commits |
| Maior em arquivos | #44 (reorganização em pacote) — 147 arquivos |
| Menor não-vazio | #75 — +17/−10 em 1 arquivo, com 2.746 caracteres de corpo |
| Vida mediana do PR | **2,5 minutos** (33 dos 70 fecharam em menos de 2 min) |

A vida mediana de 2,5 minutos diz o que o processo é: **não há revisão por terceiros**. O PR é um registro de entrega, não um portão. E é justamente por isso que os corpos ficaram tão densos — eles são o lugar onde o raciocínio é preservado.

### O corpo do PR muda de gênero três vezes

A média de caracteres por corpo sobe de **2.180** (até #30) para **3.345** (a partir de #58), *apesar* de os diffs ficarem menores. O formato acompanha:

- **#1–#13 — catálogo.** "## O que muda" com uma subseção por frente. Três PRs saem sem corpo nenhum e com título autogerado pelo GitHub a partir do nome da branch.
- **#14–#31 — tabela e contagem.** Surge a tabela de features (24 dos 70 corpos têm uma) e a **contagem de testes como assinatura de fim de PR**, que vira um fio contínuo: 56 → 80 → 269 → 542 → 624 → 698 → 803 → 885 → 1.120 → **1.222**. Aparecem também as declarações de ordem de merge: *"ORDEM DE MERGE: mergear o #28 ANTES deste"*.
- **#39–#57 — laudo de defeito.** O gatilho é o teste real. Os corpos passam a colar evidência: linha de log exata, efeito humano observado (*"o usuário repetiu a mesma pergunta 3x achando que travou"*), causa-raiz em nível de string (*"'salva' é substring de 'Salvador'"*).
- **#63–#78 — ensaio.** O título deixa de nomear o componente e narra o achado: *"a legenda em português já estava no vault — só não estava onde a busca procura"*, *"o 'topping' que virou cobertura de pizza"*. Três marcas novas: toda régua vem com uma medição; **as alternativas descartadas são documentadas com o motivo** (o #74 mata a ideia de RAMdisk que originou o próprio PR, com o perfil por fase que prova que ela atacaria 2,5% do problema); e a autocrítica é explícita — o #75 abre com *"O bug — introduzido por mim na #74"*.

Uma constante: **47 dos 70 corpos** nomeiam pelo menos um botão `MENTE_*` ou flag de desligar.

### O PR mais instrutivo

O **#66** (Cannabis Encyclopedia, +5.630) contradiz o próprio esforço no desfecho: onze commits para colocar um livro no vault, e *"cinco testes cegos deram a base ANTIGA vencendo por 31 a 18. Passei horas medindo a qualidade dos ÁTOMOS quando o gargalo estava em como o contexto era montado. Duas correções de recuperação, zero GPU e zero átomo reescrito, viraram para 10 a 4 a favor da base nova."* Fecha com a lição metodológica: **teste de qualidade de base tem de passar pelo caminho de produção** — os testes anteriores mediam recuperação crua em vez do que o usuário recebe.

### Duas falhas de entrega que o GitHub escondeu

Onze PRs têm base diferente de `master`, e o empilhamento produziu dois casos em que o "merged" do GitHub era falso — ambos confessados no corpo do PR corretivo:

- **#28:** o #26 mergeou às 22:26 e o #27 às 22:50, então a modularização entrou na branch de portfólio *depois* do merge dela na master — a master ficou 24 minutos sem a refatoração.
- **#62, mais grave:** *"As PRs #58–#61 aparecem como merged no GitHub, mas foram empilhadas… nenhum arquivo das Fases 1–5 está no master hoje."* Quatro PRs marcados como mesclados cujo código não existia no branch principal.

### Curiosidades

O **#16** tem zero arquivos e zero linhas — aberto e fechado em 40 segundos, puro artefato de gerenciamento de branch. O **#26**, que introduz o CI, observa no próprio corpo: *"este próprio PR é o primeiro teste do workflow"*. O **#55** aparece como 0/0 porque só troca binários — e o corpo revela um bug de vitrine: os vídeos de demo tinham 114 e 74 MB, *"provável causa de não carregarem"* no GitHub. E o único PR abandonado é o mais pessoal: o **#52**, um kit de candidatura, aberto e fechado sem merge no mesmo dia — mas a seção "o que este projeto demonstra de Engenharia de Dados" que ele propunha sobreviveu, entregue dez horas depois pelo #53.

---

## O que a história mostra sobre o método

**Instrumentação antes de otimização.** O waterfall por estágio (era 2) precede quase todo ganho das eras 3 e 4. A gravação do turno inteiro em JSONL (`40505e4`) foi criada para depurar figuras e imediatamente revelou outro problema: 7 de 19 respostas batendo no teto de tokens, uma delas escalando para a web e respondendo pior do que o vault sabia.

**Cada otimização gera o próximo bug, e isso é assumido.** O race do deep-fetch criou o filler que atropela a resposta. O ganho de boot ao não carregar o XTTS criou os 20s na primeira fala. A pré-montagem que resolveu isso criou a corrida de import. Não há tentativa de esconder essa cadeia — ela é o corpo dos commits.

**Reversibilidade e degradação são requisito, não enfeite.** Praticamente toda feature nasce atrás de um botão do `.env` com default seguro, e vários commits documentam o comportamento nos extremos do botão (`MENTE_DEFINICIONAL_MIN_ATOMOS`: 1 desliga, alto reproduz o comportamento anterior). Ausência de dependência opcional degrada em vez de quebrar — `verbalizar` sem `num2words` fala o dígito cru.

**Réguas refutadas ficam escritas no código.** Há vários casos em que a abordagem que *não* funcionou está documentada no ponto exato onde alguém a reescreveria: o corte relativo ao texto na busca de figuras, a detecção de resíduo por round-trip, o casamento átomo↔fonte por palavra, o revisor de tradução testado em 25 títulos (acertou 2, piorou 1) e descartado antes de entrar no repo.

---

## Pontos de atenção

Quatro observações factuais, não críticas ao mérito do trabalho:

1. **O default do principal botão de calibração ficou na escala antiga.** `rag_score_confident` vale `0.8` em `config.py` — o mesmo valor do commit inicial (`5919df3`, 14/jul), nunca alterado. Só que esse número é da escala do MiniLM: quando o embedding virou e5-base (`0d345c4`, 19/jul), o valor calibrado passou a ser **0.16**, derivado por `eval/calibrar_gate.py`. Esse 0.16 existe apenas no `.env.example`, e só desde 25/jul (`1f7cfed`). Consequência prática: **quem clona o repo e roda sem copiar o `.env` calibra o gate na escala errada** — com 0.8 na escala do e5, quase todo chunk passa como "confiante" e o Cache Hit falso volta pela porta que o projeto passou uma era inteira fechando. O `CLAUDE.md` também documenta `0.8` como default, tecnicamente correto e operacionalmente enganoso. É exatamente a classe de erro que o próprio projeto já catalogou duas vezes: o dedup a 0.08 na escala do e5 (consertado na Consultoria TTFT) e a lição registrada em código no #59, *"recalibrar junto com o embedding"*.

2. **A documentação está defasada em relação ao código.** O README (última atualização 25/jul) declara "~12.900 linhas em 34 módulos + 885 testes". O estado real em 30/jul é **19.327 linhas em 51 módulos e 1.226 testes**. A descrição do repositório no GitHub também cita 824 testes. É um sintoma do ritmo dos últimos cinco dias, não de descuido — o histórico mostra ressincronizações periódicas de contagem, só que a última ficou para trás.

3. **A branch de trabalho virou tronco de fato.** Os PRs **#61 a #78** saíram todos da mesma branch `feat/ocr-livro-escaneado-fase3`, muito depois de o assunto ter deixado de ser OCR — o nome já não descreve o conteúdo (figuras, boot, TTS, ponte de vocabulário). O mesmo aconteceu antes com `feat/function-calling-web-fallback`, tronco de 9 PRs. Funciona, mas foi esse padrão que produziu as duas falhas de entrega descritas acima.

4. **Verificação da suíte neste ambiente:** `1.193 de 1.226 testes passam`. As 33 falhas restantes concentram-se em cinco arquivos e são todas de **dependência opcional ausente** (`num2words` e `trafilatura`, que não instalam aqui porque `docopt` não compila nesta versão do Python) — não são regressões do projeto.

---

*Relatório produzido a partir da leitura integral dos 259 commits e dos 78 pull requests do repositório.*
