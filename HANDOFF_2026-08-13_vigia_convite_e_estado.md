# HANDOFF — 2026-08-13

Para quem abrir o próximo chat. Escrito no fim da sessão, com o estado **conferido**,
não lembrado.

---

## 1. ONDE ESTAMOS

| item | estado |
|---|---|
| `master` | **`af5fc536`** |
| suíte Python | **2370 passed** (~60 s), `ruff check .` limpo |
| Android | `assembleDebug` + `testDebugUnitTest` verdes |
| PRs abertos | **nenhum** |
| árvore de trabalho | limpa |
| assistente | de pé, oculto (`app.py --oculto`) |
| vigia | de plantão na 8765 |
| APK | `Downloads\mente-digital-2026-08-13.apk` — varrido e instalado no celular do dono |

⚠ **Datei os commits de hoje como "2026-08-08" por engano.** A data real é 13-08; o
git carrega a certa, o texto das mensagens não. Ao ler os commits desta leva,
**desconfie de datas escritas no corpo**.

---

## 2. O QUE ENTROU HOJE (5 PRs)

| PR | assunto |
|---|---|
| #111 | validade própria por código de pareamento (`--minutos`) |
| #112 | mensageiro usuário→mestre, identidade de app no Windows, alças de redimensionar, porta de socorro, sanitização do repo |
| #113 | o plantão respeita o jogo (`vez.py`) |
| #114 | a tela de boot do celular que ignorava o recado |
| #115 | `--sem-prazo`: código que vale até ser usado |

**Por que havia trabalho parado** (a causa, não o sintoma):
- **O #111 era um RASCUNHO.** Ficou 2 dias sem merge porque o botão não existia — não
  por falta de revisão. ⚑ Antes de investigar por que um PR "não anda":
  `gh pr view N --json isDraft`.
- **4 commits ficaram órfãos** por serem empurrados **depois** do merge do PR que usava
  a mesma branch. ⚑ `git log origin/master..HEAD` acha isso em 1 s. Um PR mergeado
  **não** garante que a branch está vazia.

---

## 3. O QUE FALTA — em ordem de quem destrava o quê

### 3.1 ⚠ A medição que GATEIA uma feature inteira

**O Assistente de Foco engole o toast em tela cheia?** Não medido, e é o único item que
decide se qualquer notificação para o dono durante o jogo é viável.

- script pronto: `<scratchpad>/medir_toast_foco.py`, **6 fases, ~12 min**, exige o Tarkov
  aberto (fase do jogo no menu/hideout, não em raid)
- o caminho de leitura do perfil foi **ACHADO E PROVADO**: blob binário em
  `HKCU\...\CloudStore\...quiethourssettings`, string UTF-16LE começando em offset
  **ÍMPAR** (decodificar do 0 desalinha e vira ideograma)
- a config desta máquina já diz `quietmomentfullscreen = AlarmsOnly [ligada]` — o modo
  mais duro. **Previsão, não conclusão:** o toast comum provavelmente morre. Por isso o
  script mede 4 canais na mesma passada
- ⚠ **A medição de 05-08 está EM DÚVIDA**: a chave por app do AUMID
  `MenteDigital.Assistente.Local.1` **não existe** no registro (Discord e VS Code têm a
  delas). Aquele toast pode ter saído sob o AUMID do PowerShell

### 3.2 Atos do DONO (nenhum código envolvido)

- **Convidar o Felipe para o tailnet** — painel do Tailscale. Sem isso ele instala o
  app, digita o código e não alcança o PC. O convite (`dados/convites/convite_felipe.html`)
  e o APK já foram entregues.
- **Autostart do Tailscale na MIUI** — a MIUI mata o Tailscale em segundo plano
  ("Connected" sem processo). Pendente desde 04-08.
- **⚠ 02-11-2026**: as três credenciais de aparelho **e** o certificado Tailscale expiram
  no **mesmo dia**. Com o legado desligado, esse dia derruba credenciais e TLS juntos.
- **O histórico do git ainda tem o IP de LAN e o nome MagicDNS.** A árvore foi
  sanitizada no #112; limpar o histórico é reescrita pública (quebra todo clone) —
  decisão do dono, **não tomada**.

### 3.3 O flip `MENTE_APARELHOS_TOKEN_LEGADO=false`

Mais perto do que a memória antiga dizia. **Falta só:**
1. reiniciar o **vigia** depois de editar o `.env` (processo separado, lê settings no
   próprio boot; sem isso o plantão fica mais permissivo que o servidor)
2. decidir o que fazer com o `MENTE_ACCESS_TOKEN` morto no `.env`

**Já resolvido:** o `scripts/parear_janela.py` (porta de socorro) estava quebrado e foi
consertado no #112. O app Android **não precisa de nada** — o slot único já guarda a
credencial de aparelho.

⚠ Enquanto o legado viver, quem tem o token antigo entra **como o dono** — mestre, com
acesso à caixa de mensagens. Isso vale para as rotas do mensageiro também.

### 3.4 Arestas conhecidas e NÃO consertadas

- **`daniel -> daniel`**: quando o pedinte é o próprio dono, o dreno gera as duas
  mensagens para ele mesmo. Não é caminho de erro; é ruído.
- **A tela "PC em uso" não oferece botão** para falar com o dono. O recado já é
  registrado sozinho, mas quem quiser explicar urgência precisa entrar no app depois.
- **Sem push (FCM).** Se o app do pedinte estiver fechado, o aviso espera ele abrir.
- **Sem limite de taxa em `POST /api/mensagens`** — cada entrega sintetiza TTS, que com
  XTTS é trabalho na mesma GPU do LLM.
- **Sem retenção da tabela `mensagens`**; a listagem tem teto de 200.
- **A entrega de mensagem não deixa rastro em `auditoria`.**

### 3.5 Branches velhas (auditadas hoje)

7 branches remotas não-mergeadas. As de julho são artefatos de merge commit.
Duas merecem atenção:
- `origin/fix/contador-boot-android` (1 commit, 04-08) — o conteúdo **já está no master**
  por outro caminho (PR #103). Provável duplicata.
- `origin/claude/xp-investimentos-profile-kqotrf` (6 commits, 23-07) — **material de
  candidatura e LinkedIn** num repositório PÚBLICO. Vale o dono decidir se fica.

---

## 4. ⚑ ARMADILHAS QUE ESTA SESSÃO PAGOU

Não repetir.

**Sobre o processo rodando:**
- **mtime NÃO distingue "conteúdo mudou" de "git tocou o arquivo".** Depois de um `pull`,
  todo processo parece desatualizado. Reiniciar custa menos que deduzir.
- **Importar o módulo num processo separado não prova nada sobre o que está rodando.**
  Para isso: mtime do arquivo **<** instante de criação do processo.
- **Todo merge que toca módulo puro exige reiniciar assistente E vigia.** O `SEM_PRAZO`
  num servidor antigo viraria `emitido_em - 1 min` → o convite nasce vencido, com o
  sintoma longe da causa.

**Sobre medir:**
- **Constante trocada reprova o que está certo**: `IDC_SIZEWE`=32644, `IDC_SIZEALL`=32646.
- **A própria sonda contamina a próxima**: o teste de foco (que manda a janela para
  `HWND_BOTTOM`) fazia o teste de arrasto seguinte "não mexer". Duas rodadas limpas
  passam idênticas.

**Sobre código:**
- **`WM_NCCALCSIZE` governa GEOMETRIA, não PINTURA.** São mensagens diferentes.
- **Um valor especial precisa de FRASE em TODO lugar que o imprime** — e "todo lugar" é o
  que o `grep` acha, não o que a memória lembra. O `SEM_PRAZO=-1` vazou como
  "válido por -1 min" num log, depois de o HTML já estar consertado.
- **Setar variável não é mostrar na tela.** `TelaBoot.detalhe()` cravava o texto e
  ignorava o aviso — o conserto inteiro era invisível.
- **Bloqueado não é "esperando"**: anel, barra e lista de serviços são PROGRESSO. Deixá-los
  rodando sob um recado de "não vai acontecer" faz os três contradizerem a frase.

**Sobre o ambiente:**
- O `python` do PATH é o atalho falso da Microsoft Store. Use
  `C:\ProgramData\miniconda3\envs\llama-omni\python.exe`.
- O `java` do PATH é 1.8 e não serve para o Android. Use
  `JAVA_HOME="C:/Program Files/Android/Android Studio/jbr"`.
- PowerShell corrompe binário no `>` (BOM). Para `adb screencap`, use
  `shell screencap` + `pull`.
- Git Bash converte `/data/...` em `C:/Program Files/Git/data/...`. Use `MSYS_NO_PATHCONV=1`.

---

## 5. COMO TESTAR O BLOQUEIO-POR-JOGO SEM UM JOGO

Feito hoje e funciona. **Não toca o `.env`** — a variável vai só no ambiente do processo:

1. encerrar assistente e vigia
2. abrir o Bloco de Notas
3. subir o vigia com `MENTE_VIGIA_JOGOS_EXTRAS=notepad.exe`
4. abrir o app no celular → deve mostrar **"PC em uso"**
5. conferir `dados/pedidos_de_acesso.jsonl`
6. fechar o Bloco de Notas → o vigia levanta o assistente em ~20 s
7. conferir as duas mensagens na tabela `mensagens` (tipo `acesso`)
8. **restaurar**: matar o vigia de teste e subir o normal, sem a variável

Medido hoje: recusa → tela certa → bilhete com o usuário vindo da **credencial** →
`[VIGIA] notepad.exe fechou e havia gente esperando` → assistente de pé em **15 s** →
dreno criou as duas mensagens.

---

## 6. SUGESTÃO DE PRÓXIMO ASSUNTO

Na ordem em que eu atacaria:

1. **A medição do Assistente de Foco** (§3.1) — 12 min, e destrava ou mata um desenho
   inteiro. É o único item cujo resultado muda o que se constrói.
2. **O flip do token legado** (§3.3) — dois passos, e fecha a última porta que ignora
   identidade. Com gente de fora entrando, isso deixou de ser hipotético.
3. **A decisão sobre o histórico público** (§3.2) — não urgente, mas não some sozinha.
4. **As arestas do §3.4** — a mais barata e visível é o `daniel -> daniel`.
