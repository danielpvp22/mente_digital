# HANDOFF — 2026-08-05 · convidar alguém virou um arquivo, e o que ainda trava

> Documento autônomo: quem ler isto não participou da sessão.
> Repo: `danielpvp22/mente_digital`. `master` = **`5a5013a0`**. Suíte: **2123** (Python).
> PRs **[#106](https://github.com/danielpvp22/mente_digital/pull/106)**,
> **[#107](https://github.com/danielpvp22/mente_digital/pull/107)** e
> **[#108](https://github.com/danielpvp22/mente_digital/pull/108)** mergeados, CI verde.
> **Supersede** o `HANDOFF_2026-08-04_celular_e_janela.md` (apagado neste commit) — o
> que sobreviveu dele está aqui.

---

## 1. ▶ O QUE FALTA, em ordem

### Passo 1 — Autostart do Tailscale na MIUI *(ato do dono, no celular)*

**É o mais sério, e não é cosmético.** A MIUI mata o Tailscale em segundo plano: ele
exibe **"Connected" com processo NENHUM rodando** (`ps -A | grep tailscale` vazio, sem
`tun`), e só reconecta quando o app é trazido à frente. Sem *Autostart* + isenção de
bateria, o acesso remoto morre sozinho — e o sintoma é o app dizendo corretamente
"sem resposta do PC", mandando procurar defeito onde não há.

### Passo 2 — provar a credencial da janela ANTES de matar o token legado

Estado medido em 2026-08-05:

| | |
|---|---|
| `MENTE_JANELA_CREDENCIAL` | ✅ definida |
| aparelho `62aa5b894c3e28ce` ("janela do PC") | pareado, **`ultimo_uso = None`** |
| `MENTE_APARELHOS_TOKEN_LEGADO` | ⚠ ainda `true` |

`ultimo_uso = None` significa que **a janela nunca autenticou por essa credencial** —
ela pode estar entrando pelo token legado, e ninguém notaria enquanto ele viver.
Desligar o legado nessa condição **tranca o dono fora do próprio app de mesa**.

A prova é barata: subir o `app.py` e conferir se aquele `ultimo_uso` deixa de ser nulo.
Só então `MENTE_APARELHOS_TOKEN_LEGADO=false`.

> ⚠ E **o token legado não serve para "adicionar usuários"** — foi o mal-entendido que
> o manteve vivo. Quem entra com o `MENTE_ACCESS_TOKEN` entra **COMO O DONO**: sem
> identidade própria, lendo a memória dele, sem revogação individual. Ele **clona**,
> não convida. Convidar é o Passo 3.

### Passo 3 — convidar alguém *(pronto, é só usar)*

```bash
python scripts/gerar_convite.py "celular da ana" ana
```

Sai `dados/convites/convite_ana.html`, autocontido. **Mande ele + o
`android/app/build/outputs/apk/debug/app-debug.apk` na mesma conversa do WhatsApp.**
A pessoa abre o convite no celular e segue cinco passos até estar conversando.

⚠ O que **nenhuma página resolve** e o convite avisa como passo 2: conta Tailscale não
basta — **o dono precisa COMPARTILHAR a máquina** com ela no admin console. Sem isso o
Tailscale conecta, o assistente não aparece, e a pessoa conclui que o app quebrou.

O código dura `MENTE_APARELHOS_CODIGO_VALIDADE_MINUTOS` (10) e serve **uma vez** —
gere na hora de mandar, não antes.

---

## 2. ESTADO — medido em 2026-08-05

| item | estado |
|---|---|
| Tailscale + TLS | ✅ cert `sechex-blrzc2v.tail412b37.ts.net`, válido até **2 nov 2026** |
| Renovação do cert | ✅ Tarefa Agendada rodou; próxima 06/08 03:30 |
| `MULTIUSUARIO` / `APARELHOS` | ✅ os dois **ligados** em produção |
| `TOKEN_LEGADO` | ⚠ `true` — ver Passo 2 |
| Aparelhos | 3 ativos (celular do dono, navegador duckduckgo, janela do PC), 2 revogados |
| Releases públicas | **nenhuma** — ver §4 |
| Wattímetro de plantão | ✅ instalado na Inicializar; **cobertura de 100%** em 05/08 |
| Suspensão do Windows | ✅ `STANDBYIDLE=0x0` = nunca — ⚠ **tem de continuar** |
| Desligar tela | 5 min no Windows **e** `MENTE_TELA_TIMEOUT_MINUTOS=5` |

⚠ **`STANDBYIDLE=0x0` não é descuido.** Suspensa ou desligada, a máquina não é
alcançável pelo Tailscale e o vigia não pode se acordar — ele roda nela. O "desligar
após 1 h ocioso" que o dono chegou a pedir **já existe e é melhor**:
`idle_standby_minutos=20` solta a VRAM e `idle_encerrar_minutos=45` encerra o app
deixando o vigia de plantão — reversível pelo celular, ao contrário de um shutdown.

---

## 3. O que mudou nesta sessão

**[#107](https://github.com/danielpvp22/mente_digital/pull/107) — o convite.**
Adicionar usuário era: estar no PC, abrir terminal, e a pessoa **digitar** um código de
10 minutos. Agora é mandar dois arquivos.

- ⚑ **A página do tutorial NÃO pode ser servida pelo assistente.** Ela ensinaria a
  instalar o Tailscale e só seria alcançável *depois* dele — o tutorial atrás da porta
  que ensina a abrir. E o endereço de LAN não salva: o cert cobre só o nome do
  Tailscale. Arquivo offline escapa disso por construção.
- ⚑ **QR não serve aqui.** A página chega por WhatsApp e é aberta no **mesmo** celular;
  ninguém escaneia QR na própria tela. Link tocável resolve — e economizou uma
  dependência (`segno`/`qrcode` não estão na env).
- O endereço sai do **certificado** (`Path(ssl_cert).stem`), não de campo próprio: bate
  com o nome emitido por construção. Sem cert, o convite **admite que não sabe** em vez
  de chutar o IP da LAN, que o TLS rejeitaria por nome.

**[#106](https://github.com/danielpvp22/mente_digital/pull/106)** — o `Cenario` da
`tomada.py` passou a descrever a placa que está na máquina (5080), não a antiga.

---

## 4. ⚑ A release pública: publicada e RETIRADA no mesmo dia

O dono autorizou publicar o APK (`app-2026.08.05`), e minutos depois recuou: *"pera, só
whatsapp e link convite"*. **Release e tag apagadas, 0 downloads** — nada ficou exposto.
Hoje o repo não tem release nenhuma, e é assim que ele quer.

> ⚑ **Retirar a release QUEBROU o convite em silêncio.** O botão apontava para
> `/releases/latest`, e com a release apagada essa URL **não dá 404**: responde **HTTP
> 200** e leva à página de releases VAZIA. A pessoa toca, vê que "deu certo" e não tem
> arquivo. Pior que link morto, porque não denuncia. Consertado no #108, com teste
> travando que o convite não volte a citar `releases`/`github.com`.

⚠ **Se um dia se publicar de novo:** o repositório é **PÚBLICO**. Antes da release o APK
foi varrido pelos valores CONCRETOS desta máquina — `MENTE_ACCESS_TOKEN`, credencial
`mdk1.`, nome MagicDNS, IPs `100.x`/`192.168.x`, arquivos suspeitos nas 151 entradas —
e estava limpo. Essa varredura é a régua, não o opcional.

---

## 5. ⚑ ARMADILHAS QUE CONTINUAM MORDENDO

- **`dados/` é ignorado PASTA POR PASTA.** Uma pasta nova nasce **rastreável**. O aviso
  está escrito no próprio `.gitignore` — *"ao criar qualquer pasta em dados/, pergunte
  se ela tem segredo dentro"* — e mesmo assim `dados/convites/` nasceu versionável, com
  um código de pareamento vivo dentro, a um `git add -A` de ir para um repo público.
  **Aviso escrito não substitui a pergunta na hora.**
- **Teste que crava convenção de plataforma testa a plataforma.** Um `r"C:\certs\..."`
  num teste quebrou **só no CI**: no POSIX o `Path.stem` não separa por barra invertida.
  Verde no Windows o tempo todo. Terceira vez desta família (`os.name`→`pathlib`,
  `ctypes.wintypes`). Monte caminho com `Path`.
- **`vigia.subir_app` NÃO sobe o vigia** — apesar do nome, levanta o *assistente*
  (`app.py --oculto`).
- **O endereço de LAN não serve mais.** O cert cobre o nome do Tailscale, então
  `https://192.168.15.13:8000` é rejeitado por nome. VPN ligada é obrigatória **até
  dentro de casa**.
- **A MIUI barra `adb shell input text`** (`INJECT_EVENTS`). Funciona: `am start`,
  `am force-stop`, `pm list`, `uiautomator dump`, `adb install`.
- **Com `MENTE_ACCESS_TOKEN` configurado, loopback NÃO isenta** — o token é exigido
  venha de onde vier. Para conferir rota gateada sem vazar o segredo no log do uvicorn,
  mande o header `x-mente-token`, não `?token=` na URL.
- **"Espera ≠ travado".** Quem compra o tempo do boot é o VIGIA, não o relógio: com o
  assistente encerrado ele o levanta e o `/api/health` fica mudo por ~45 s legítimos.

---

## 6. 🔧 AMBIENTE

- Env conda **`llama-omni`** (Python 3.10.20). O `python` do PATH é o atalho falso da
  Microsoft Store — use `C:\ProgramData\miniconda3\envs\llama-omni\python.exe`.
- **PowerShell 5.1 mutila aspas**: para mensagem de commit ou corpo de PR, escreva um
  arquivo e use `-F` / `--body-file`.
- CI = `ruff` + `pytest --cov-fail-under=77` + `bandit` + `pip-audit`, com
  `requirements-ci.txt` (sem torch/llama-cpp/chromadb).
- GPU **RTX 5080 16 GB**. O jogo (Tarkov/Arena) é preso em `0xFFFF` por Tarefa Agendada;
  não rodar trabalho pesado de GPU com ele aberto.

## 7. MÉTODO QUE O DONO COBRA

- **Medir antes de afirmar**, e dizer o que **não** foi medido. Ele confere.
- **Não aceitar resultado de agente sem conferir no código** — vale para os próprios.
- Chamá-lo de **"mister"** ou "mister Daniel". Ele quer o porquê, não só o resultado.
