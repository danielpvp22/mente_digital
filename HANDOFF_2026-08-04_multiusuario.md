# HANDOFF — 2026-08-04 · multiusuário LIGADO, vault dividido, acesso remoto

> Documento autônomo: quem ler isto não participou da sessão.
> Repo: `danielpvp22/mente_digital`. Branch: `master` = **`1c878f54`**, suíte **2082 passed**.
> **Supersede** os handoffs anteriores (todos apagados; o que sobreviveu deles está na
> memória do agente).

> ## ✅ ATUALIZAÇÃO DE 2026-08-04 — o multiusuário ESTÁ NO AR
>
> O que a sessão de 08-03 deixou como "só falta ato do dono" tinha um degrau escondido.
> Feito hoje, nesta ordem:
>
> 1. **Backup fresco** em `backups/pre_multiusuario/mente_2026-08-04.zip` (356 MB,
>    29.904 entradas, integridade OK). Necessário porque o `mente_2026-08-04.zip` das
>    00:02 tinha **zero** das 7 notas derivadas — e o `--fazer-backup` o aceitava.
> 2. **Migração aplicada** — 7 notas movidas, 0 wikilinks quebrados.
>    8.904 + 14.499 + 1.817 = **25.220**, nada perdido.
> 3. **[PR #93](https://github.com/danielpvp22/mente_digital/pull/93) mergeado** — o
>    defeito bloqueante (abaixo).
> 4. **`MENTE_MULTIUSUARIO_HABILITADO=true`** no `.env`, com o porquê escrito ao lado.
> 5. **Reindex com a flag ligada**, 55 s: `acervo` 11.103 + `pessoal_daniel` 14.569 =
>    **25.672**, idêntico à coleção legada. A `langchain` fica no disco INTACTA — é o
>    caminho de volta, ao custo de dobrar o índice (~936 MB).
> 6. **Provado ao vivo**: três buscas com contexto real (13.070 / 9.699 / 12.570 chars),
>    fontes das DUAS coleções, e `anexos` trazendo figura de `Figuras/`. No boot:
>    nenhum aviso de `sobras` e `VectorDB já sincronizado (nada novo)`.
>
> ### ⚠ O DEGRAU ESCONDIDO (o que este handoff não sabia)
>
> "Reindex do Chroma: rodado" era verdade — mas para a coleção **legada**.
> `rag._colecao_base()` devolve `acervo` com a flag ligada e `langchain` com ela
> desligada. Como o sync do boot é `track_boot_task` (**não awaitado**), subir a flag sem
> reindexar antes faria o servidor atender com o vault CEGO.
>
> E ao reindexar apareceu o defeito de verdade: `_escopos_de_indexacao` cobria só
> `Acervo/` e `Pessoal/<dono>/`, e as **1.817 notas de figura** moram em `Figuras/` na
> RAIZ. Caíam em `sobras` — avisadas e NÃO indexadas. Como `montar_indice_figuras` lê do
> store, ligar a flag **apagaria a busca de figuras inteira**. Consertado no #93: uma
> coleção passa a cobrir VÁRIAS raízes (o acervo cobre as duas pastas). Não dois escopos
> para a mesma coleção — a purga de órfãos os faria se apagar mutuamente.
>
> ### Fechamento do dia — `master` = **`1c878f54`**, suíte **2082 passed**
>
> | PR | o quê |
> |---|---|
> | [#93](https://github.com/danielpvp22/mente_digital/pull/93) | `Figuras/` fora de todo escopo — apagaria a busca de figuras |
> | [#94](https://github.com/danielpvp22/mente_digital/pull/94) | alerta de segurança morria com `no running event loop` |
> | [#95](https://github.com/danielpvp22/mente_digital/pull/95) | wattímetro avulso (mede com o assistente fechado) |
> | [#96](https://github.com/danielpvp22/mente_digital/pull/96) | wattímetro vira **plantão** e sobe no logon |
> | [#97](https://github.com/danielpvp22/mente_digital/pull/97) | a suíte herdava a flag do `.env` do dono (134 falhas) |
> | [#98](https://github.com/danielpvp22/mente_digital/pull/98) | a tela apagada entra na conta da parede (−88 W de incerteza) |
> | [#99](https://github.com/danielpvp22/mente_digital/pull/99) | automatiza o passo depois do login no Tailscale |
>
> ### ⚠ ENERGIA: o que NÃO mudar
>
> A máquina está com **`STANDBYIDLE=0x0` (nunca suspender)** e tem de continuar: suspensa
> ou desligada, o Tailscale não a alcança e o vigia não acorda — o dono perderia o "codar
> pelo celular de longe", que é o ponto de três sessões de trabalho. O "desligar depois de
> 1 h ocioso" que ele pediu **já existe e é melhor**: `idle_standby_minutos=20` solta a
> VRAM e `idle_encerrar_minutos=45` encerra o app deixando o vigia de plantão — reversível
> pelo celular, ao contrário de um shutdown do Windows.
>
> ✅ **FEITO em 2026-08-04:** `powercfg /change monitor-timeout-ac 5` (VIDEOIDLE = `0x12c`)
> **e** `MENTE_TELA_TIMEOUT_MINUTOS=5` no `.env`, os dois no mesmo momento.
>
> ⚠ **Se um dia mudar um, mude o outro.** Com o `.env` MENOR que o Windows, o modelo
> afirma "tela apagada" com ela acesa — o erro na direção que finge economia. Conferir com
> `powercfg /query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE`.
>
> ✅ O **plantão do wattímetro** está instalado na Inicializar
> (`Mente Digital - Wattimetro.vbs`) **e já rodando** — foi disparado à mão pelo próprio
> `.vbs`, desgrudado, então não depende de reiniciar. Se um dia precisar subir sem logon:
>
> ```bash
> python scripts/registrar_consumo.py --plantao --silencioso
> ```
>
> **O que ainda falta é só o dono, e presencialmente:** instalar o Tailscale, parear os
> aparelhos, e o token legado por último (passos 1, 3 e 4 abaixo — o passo 2 está FEITO).

---

## 1. ▶ O QUE FALTA — e é tudo ato do DONO, nesta ordem

Nada está em aberto no código. O que segue não é programação; é instalador, conta e
certificado. **A ordem importa** — o passo 4 antes do 3 tranca o dono para fora.

> ### ⚡ ATALHO (2026-08-04): os passos 3 e 4 abaixo viraram UM comando
>
> ```bash
> python scripts/configurar_tailscale.py --aplicar
> ```
>
> Ele lê o nome MagicDNS do `tailscale status --json`, emite o certificado e escreve o
> `.env` preservando o arquivo byte a byte. **Torna impossível o erro mais provável do
> roteiro** (usar o IP `100.x`): se o campo vier com IP — o que significa MagicDNS
> desligado — ele PARA e diz o que ligar, em vez de emitir um certificado inútil.
> Sobram para você só instalar, logar e ligar os dois botões no admin console.
>
> ✅ **O caminho HTTPS já foi provado nesta máquina** (2026-08-04): cert autoassinado,
> servidor com `MENTE_SSL_CERT/KEY`, `GET https://…/api/health` → 200, HTTP puro
> recusado. E o `cryptography` **não** é necessário — o TLS sai do `ssl` da stdlib.

### Passo 1 — Tailscale (sem isto, o celular só alcança o PC pelo WiFi de casa)

Medido em 2026-08-03: **não está instalado**. Roteiro completo em
[docs/ACESSO_REMOTO.md](docs/ACESSO_REMOTO.md). Resumo:

1. Instalar no **PC e no celular**, mesma conta.
2. No admin console, ligar **MagicDNS** e **HTTPS Certificates** (sem os dois, o passo
   seguinte não funciona).
3. `tailscale cert maquina.SUA-TAILNET.ts.net`
4. Apontar `MENTE_SSL_CERT` / `MENTE_SSL_KEY` no `.env` para os arquivos gerados.

> ⚠ **Use o NOME MagicDNS, nunca o IP `100.x`.** O certificado é emitido para o nome; com
> IP a verificação falha e o app reporta como falha de conexão genérica — a tela diz "o PC
> está desligado" e você procura o problema no lugar errado. É o erro mais provável de
> todo o roteiro.
>
> ⚠ **Sem HTTPS o microfone não funciona fora de casa.** `getUserMedia` exige contexto
> seguro; o navegador só considera seguro `localhost` ou HTTPS. Sem isso o assistente vira
> só texto quando você não está em casa.
>
> ⚠ O certificado **expira em 90 dias**, renovação manual.

### Passo 2 — parear SÓ o próprio aparelho e testar sozinho

```bash
python scripts/aparelhos.py convidar "celular do dono"
```

Depois, no `.env`, ligar **os dois juntos**:

```
MENTE_APARELHOS_HABILITADO=true
MENTE_MULTIUSUARIO_HABILITADO=true
```

> ⚠ **Marcar o dono e ligar a flag viajam JUNTOS.** Se o WebSocket passar a marcar `ana`
> antes de a flag subir, o turno grava sob `ana` e o trabalho de fundo lê sob `daniel` —
> e não acha nada. Há teste explícito (`test_desligado_ainda_honra_o_dono_marcado`) para
> ninguém "consertar" isso por engano.

> ### ✅ FEITO em 2026-08-04 — a deriva acabou
>
> Enquanto a flag esteve desligada o ETL escrevia no caminho ANTIGO (comportamento correto
> da flag desligada), e o vault **se redividia**: 7 notas apareceram em
> `Conhecimento_Novo/` na raiz numa única noite. A migração foi reaplicada — ela é
> idempotente — e a flag subiu logo em seguida, com o servidor fechado. Com ela ligada o
> problema não volta.
>
> Se algum dia a flag for desligada de novo, a deriva recomeça e o conserto é o mesmo:
>
> ```bash
> python scripts/migrar_vault_multiusuario.py            # confira o que ele acha
> python scripts/migrar_vault_multiusuario.py --aplicar
> ```
>
> ⚠ E **reindexe depois**, com a flag no estado final — a coleção lida muda com ela.

### Passo 3 — os outros três usuários

```bash
python scripts/aparelhos.py convidar "celular da ana" ana
```

O **último argumento é o usuário** quando ele cabe na regra de nome (`a-z0-9_-`, sem
espaço). Ele decide de qual memória o aparelho lê. É **fixado no pareamento e imutável**:
trocar o dono de um aparelho pareado seria entregar a memória de alguém a outra pessoa —
para mudar, revogue e pareie de novo, que deixa rastro na trilha.

### Passo 4 — POR ÚLTIMO, matar o token legado

```
MENTE_APARELHOS_TOKEN_LEGADO=false
```

> ⚠ Enquanto ele vive, **quem tiver o `MENTE_ACCESS_TOKEN` entra COMO O DONO** — sem
> identidade, sem revogação individual, indistinguível de um aparelho legítimo. É o maior
> downgrade do sistema e ele está ligado agora. Só desligue **depois** que os quatro
> celulares estiverem pareados e testados, senão você se tranca do lado de fora.

---

## 2. ESTADO — o que já foi feito e conferido

| item | estado |
|---|---|
| [PR #92](https://github.com/danielpvp22/mente_digital/pull/92) | **mergeado**, CI verde |
| [PR #93](https://github.com/danielpvp22/mente_digital/pull/93) | **mergeado** (2026-08-04), CI verde — o defeito das figuras |
| Migração do vault | **aplicada e reaplicada**; a deriva de 7 notas foi absorvida |
| Migração do SQLite | **rodou em produção** — 12 tabelas com `dono`, 508 turnos carimbados `daniel` |
| Reindex do Chroma | **refeito com a flag LIGADA** — `acervo` 11.103 + `pessoal_daniel` 14.569 = 25.672 |
| `MENTE_MULTIUSUARIO_HABILITADO` | ✅ **true** |
| `MENTE_APARELHOS_HABILITADO` | desligado (de propósito — depende do pareamento) |
| `MENTE_APARELHOS_TOKEN_LEGADO` | vivo (de propósito — só morre no passo 4) |
| Lista de compras | limpa (266 "- pão" de teste removidos) |
| Wattímetro de plantão | ✅ instalado na Inicializar **e rodando** (mede com o app fechado) |
| Tela apagada na conta | ✅ Windows **5 min** e `MENTE_TELA_TIMEOUT_MINUTOS=5`, casados |
| Suspensão do Windows | ✅ `STANDBYIDLE=0x0` = **nunca** — ⚠ tem de continuar assim |
| TLS/HTTPS | ✅ **provado** nesta máquina (cert local → 200; `cryptography` dispensável) |
| Tailscale | ⛔ **não instalado** — o único bloqueio do acesso de fora |
| Aparelhos pareados | ⛔ **nenhum** (tabela vazia) |

**Duas coisas vistas no boot de 2026-08-04** (o servidor subiu e serviu normalmente
com as duas):

1. ✅ **NÃO era anomalia — era o gate certo, e minha primeira leitura estava errada.**
   O `[APARELHOS] 127.0.0.1: N falhas` aparecia porque a aba de teste abriu
   `http://localhost:8000` SEM token. Eu supus "recusa por `Origin`"; o código diz
   outra coisa (`acesso.cliente_autorizado`): **com `MENTE_ACCESS_TOKEN` configurado,
   o token é exigido "venha de onde vier" — loopback NÃO isenta.** Só sem token é que
   o loopback passa. O `/api/health` respondia 200 porque não tem gate. Para conferir
   uma rota gateada sem vazar o segredo no log do uvicorn, mande o header
   `x-mente-token` em vez de `?token=` na URL.
2. ✅ **CONSERTADO** ([PR #94](https://github.com/danielpvp22/mente_digital/pull/94),
   mergeado): `scheduler.py alertar_seguranca → RuntimeError: no running event loop`.
   O alerta de acesso recusado falhava justamente quando era preciso.

**Estrutura do vault agora:**

```
dados/Cerebro_Digital/
  Acervo/            8.904  átomos de obra (comum aos 4)
  Pessoal/daniel/   14.492  memória privada do dono
  Figuras/           1.817  figuras de obra (escopo do acervo)
                    ──────
                    25.213  ✓ nenhuma nota perdida
```

`Figuras/` **fica na raiz de propósito**: 3.663 embeds são por CAMINHO
(`![[Figuras/...]]`) e `/api/imagem` resolve com `is_file()` — movê-la daria 404 silencioso
em 3.663 imagens. Links por NOME são neutros (25.213 nomes distintos, zero colisão).

**Backups:** `backups/mente_2026-08-03.zip` (355 MB, verificado: 29.817 entradas,
integridade OK, estrutura pré-migração, com `.db` e `.env`). A lista de compras original
está em `dados/backups/limpeza_lista_20260803/`.

---

## 3. COMO A COISA FUNCIONA (o mínimo para não quebrar)

**A fronteira de privacidade é a PASTA/COLEÇÃO, não um filtro de metadado.** Isto não é
preferência de estilo — foi imposto por três medições:

1. `rag._buscar_texto` é **deliberadamente fail-open**: filtro que erra *ou vem vazio* vira
   busca **sem filtro nenhum**. Com `dono` em metadado, quem não casasse nada leria o vault
   inteiro.
2. Dos 25.671 chunks, **5 não têm a chave `origem`** — e são justamente `Inbox_Captura.md`,
   `Lista_compras.md` e as notas salvas à mão. Chave ausente não casa `where` em sentido
   nenhum.
3. O único metadado em **100%** dos chunks é `source`, o caminho — e caminho não se esquece
   de gravar, porque escrever o arquivo obriga a escolher a pasta.

**`mente_digital/identidade.py` é o contrato.** ContextVar (mesma razão já documentada em
`state.turno_falado`), e o **default é `None` de propósito**: um caminho que esqueça de
marcar o dono **falha alto** (`DonoIndefinido`) em vez de herdar o anterior. Um default que
"funciona" mostraria a nota da pessoa errada.

**O MESTRE administra, não lê.** `identidade.MESTRE` parea, revoga, lê a trilha inteira e
recebe os alertas de segurança — e **não** enxerga a memória dos outros. Se um dia isso
mudar, tem de ser decisão explícita do dono e campo próprio, nunca efeito colateral de "o
admin precisava depurar".

---

## 4. ⚑ ARMADILHAS QUE ESTA SESSÃO PAGOU

- **A suíte escrevia no vault REAL há meses.** `Lista_compras.md` tinha 272 linhas: 4 do
  dono e **266 "- pão"** de cinco arquivos de teste. Passou despercebido porque *teste que
  escreve no banco QUEBRA outro teste; teste que escreve no vault só engorda um arquivo*.
  **Poluição não aparece em suíte verde.** Corrigido: o `conftest` redireciona
  `MENTE_CAMINHO_OBSIDIAN` para tmp, junto com SQLite, log, transcrição e chat dump.
- **`scripts/reindexar.py` usa a GPU, não a CPU** — `preparar_embedding_offline()` ignora o
  `MENTE_EMBEDDING_DEVICE=cpu` do `.env` porque foi feito para rodar com o servidor fechado.
  **Não rode com o jogo aberto**: disputaria exatamente a GPU que o jogo precisa.
- **`--fazer-backup` aceita o backup DO DIA, não exige um FRESCO.** O que ele aceitou era de
  16h antes e tinha 76 notas a menos que o vault. Se o que vem é irreversível, exija fresco.
- **Classificar nota por PASTA erra nos dois sentidos.** Perde as 1.817 figuras de obra (que
  moram em `Figuras/`, não em `Conhecimento_Novo/`) e leva para o acervo comum as 51 imagens
  de `Figuras/_web/`, que são pessoais. O campo `origem` do frontmatter é a única prova.
- **8 tabelas do SQLite precisaram de RECONSTRUÇÃO, não `ALTER`** — PK/UNIQUE sobre texto do
  usuário faria `ON CONFLICT(chave)` casar **entre pessoas**, e o `INSERT OR IGNORE` de
  `habitos` descartaria em silêncio o dia do segundo dono. A solução óbvia teria "funcionado"
  e corrompido dado sem um erro sequer.
- **`split("\r\n")` em arquivo de fins de linha MISTOS** deixa pedaços com `\n` embutido.
  Use `splitlines()`. E **`write_text` no Windows traduz `\n`→`\r\n`** — juntar com `\r\n`
  antes gera `\r\r\n`; use `write_bytes`.
- **Delegar em paralelo se divide por ARQUIVO, não por feature.** As fatias por feature
  colidiam todas em `main.py`/`ws.py`/`tools.py`. Com ownership disjunto e o contrato escrito
  ANTES do fan-out, seis agentes trabalharam sem um conflito. A espinha (onde tudo se
  encontra) não paralelizava.
- **Peça validação por MUTAÇÃO.** Dois agentes provaram que seus testes não eram decorativos
  quebrando o código de propósito (9 de 17 e 5 ficaram vermelhos). Teste que não quebra
  quando o código quebra é decoração.

### Acrescentadas em 2026-08-04

- **"Reindexado" é meia informação — reindexado EM QUAL COLEÇÃO?** O handoff de 08-03
  registrou "reindex rodado, 25.672 chunks" e estava certo; só que a flag ligada troca a
  coleção lida (`_colecao_base()`: `acervo` ligada, `langchain` desligada). O número certo
  descrevia o lugar errado. **Ao registrar um trabalho de índice, registre o NOME do
  destino, não só o tamanho.**
- **Feature testada e verde pode estar casada com um layout que a produção não tem.** O
  multiusuário tinha teste de figura — com a figura em `Acervo/f1.md`. No vault real ela
  mora em `Figuras/` na raiz, e ali o escopo não ia. Suíte inteira verde, 1.817 notas
  prestes a sumir. **Quando o teste inventa o caminho, ele não prova nada sobre o caminho
  real: monte o layout de produção ao menos uma vez.**
- **Duas raízes no mesmo store se apagam.** A purga de órfãos do `_sync_escopo` remove
  todo chunk fora da raiz daquele escopo — dois escopos sobre a mesma coleção se
  declarariam órfãos mutuamente. Por isso o conserto é "várias raízes por escopo", não
  "vários escopos". E o teste disso **só dá veredicto na SEGUNDA passada**: na primeira o
  store está vazio e a purga não tem o que apagar.
- **Não itere um cursor de SQLite reusando-o dentro do laço.** Meu primeiro diagnóstico do
  Chroma listou só UMA coleção de três — o `cur.execute` interno reseta a iteração externa.
  O sistema estava certo; a ferramenta de medir é que mentia. `fetchall()` antes do laço.
- **Antes de dizer "está vazio", confira o NOME do campo.** Reportei `contexto: 0 chars` em
  três buscas boas: o campo do `LocalResult` é `texto`, e meu `getattr(r, "contexto", "")`
  devolvia o default. Um `getattr` com default transforma erro de digitação em resultado
  plausível — que é a pior forma de errar uma medição.

---

## 5. 🔧 AMBIENTE

- Env conda **`llama-omni`** (Python 3.10.20). O `python` do PATH é o atalho falso da
  Microsoft Store — use o caminho absoluto:
  `C:\ProgramData\miniconda3\envs\llama-omni\python.exe`
- **PowerShell 5.1 mutila aspas** em `python -c`. Escreva um `.py` no scratchpad e execute o
  arquivo.
- CI = `ruff check .` + `pytest --cov-fail-under=77` + `bandit --severity-level medium` +
  `pip-audit`, com `requirements-ci.txt` (sem torch/llama-cpp/chromadb).
- GPU: **RTX 5080 16 GB**. O jogo (Tarkov/Arena) é preso em `0xFFFF` (CPUs 0-15 = o CCD com
  V-Cache, medido) por Tarefa Agendada no logon. Não rodar trabalho pesado de GPU com o jogo
  aberto.

---

## 6. MÉTODO QUE O DONO COBRA

- **Medir antes de afirmar**, e dizer o que **não** foi medido. Ele confere.
- **Comentário que justifica decisão de segurança precisa ser conferido como código** —
  nesta sessão um deles estava errado (dizia que a URL do WS não é logada; verdade só para
  `python main.py`, e o CLAUDE.md documenta o comando que cai no nível default e grava o
  token em claro).
- **Não aceitar resultado de agente sem conferir no código.** Vale para os próprios: dois
  defeitos meus apareceram relendo o que eu tinha acabado de escrever.
- Chamá-lo de **"mister"** ou "mister Daniel", não "senhor". Ele quer o porquê, não só o
  resultado. Autoriza delegar agentes livremente.
