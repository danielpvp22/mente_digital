# HANDOFF — 2026-08-04 (8ª sessão) · acesso remoto FECHADO, a tela virou leitura

> Documento autônomo: quem ler isto não participou da sessão.
> Repo: `danielpvp22/mente_digital`. Branch de trabalho: **`fix/tela-leitura-real`**
> ([PR #101](https://github.com/danielpvp22/mente_digital/pull/101), aberto). `master` = `397d91e7`.
> Suíte: **2094 passed**. **Supersede** o `HANDOFF_2026-08-04_multiusuario.md` (apagado
> neste commit; o que sobreviveu dele está aqui e na memória do agente).

## ✅ O objetivo do dia foi cumprido e PROVADO

**O dono usou o assistente pelo celular, na rede 5G, fora da rede de casa.** Era o
propósito de todo o trabalho de acesso remoto das últimas sessões.

Caminho que funciona hoje: app Android → `https://sechex-blrzc2v.tail412b37.ts.net:8000`
com o token legado no campo "Token de acesso".

## Como a sessão começou — e por que isso importa

O pedido foi "continuar de onde paramos, mas as telas estavam acesas quando cheguei".
Esse "detalhe" era um defeito de contabilidade, e foi o fio que puxou metade da sessão.

---

## 1. Tailscale — instalado, configurado e provado

| item | valor |
|---|---|
| nome MagicDNS | `sechex-blrzc2v.tail412b37.ts.net` |
| IP da tailnet (PC) | `100.84.109.14` |
| IP da tailnet (celular) | `100.67.212.122` (`redmi-note-11-pro-5g`) |
| assistente | `:8000` → **200**, `ssl_verify_result=0` |
| vigia | **`:8765`** → **200**, `ssl_verify_result=0` |
| cert emitido | 4861 B / 227 B, em `dados/certs/` |

⚠ **O certificado expira em ~2026-11-02 (90 dias).** Renovar = rodar
`python scripts/configurar_tailscale.py --aplicar` de novo.

⚠ **Ao renovar, REINICIE O VIGIA JUNTO.** Ele havia subido no logon, antes de o cert
existir, e servia `http` puro enquanto o assistente já estava em `https`. O
`Endereco.vigia` do app Android deriva o endereço trocando só a PORTA, **mantendo o
esquema** — então o celular diria "o PC está desligado" com tudo funcionando.

⚠ **A porta do vigia é 8765** (`config.vigia_port`), não 8001.

### Pré-requisitos que são ato do dono (feitos)

Instalar o MSI, logar (SSO), e no admin console ligar **MagicDNS** + **HTTPS
Certificates** — sem os dois, `tailscale cert` responde
`your Tailscale account does not support getting TLS certs`. O script para nesse ponto
e **não toca no `.env`**, que é o comportamento correto.

---

## 2. `tela.py` — a premissa que a ausência do dono refutou

As telas ficaram acesas com o timeout do Windows em 5 min. **Não era configuração**
(`VIDEOIDLE` = `0x12c` = 300 s, correto). Era isto:

```
DISPLAY: [PROCESS] ...\Claude_...\app\claude.exe   "Capturing"
```

**Uma power request de DISPLAY não disputa com o timeout — ela suspende o contador de
ocioso inteiro.** O `tela.py` inferia o estado dos monitores por ocioso × timeout, e o
cabeçalho *afirmava* que o erro só caía para o lado seguro. Falso: durante toda a
ausência ele disse APAGADA com as telas acesas.

| | parede (mesma medida de sensor) |
|---|---|
| o que **afirmou** (apagada) | 213,9 .. 289,6 W |
| o que **era** (acesa) | 249,3 .. 377,6 W |

**35,4 a 88,0 W subcontados** (0,074–0,185 kWh em ~2,1 h) — na direção que o próprio
módulo declara intolerável (*"fingir economia corrompe a conta"*).

**Correção:** duas fontes, nesta ordem — a LEITURA (`GUID_CONSOLE_DISPLAY_STATE` via
`PowerSettingRegisterNotification`/powrprof) e, só na falta dela, a inferência de
sempre. Registra em processo de **console** (sem HWND, sem message loop — é o que a
torna viável no plantão), **sem admin**, e o registro entrega o valor **atual**.

### O erro de medição que quase passou

A 1ª medição disse que o callback chegava em **0,0 ms**. Era `time.monotonic()`, cuja
resolução no Windows é ~15 ms, arredondando os **11,5 ms** reais para zero. Sem a espera
que isso obrigou a escrever, a **primeira amostra de todo processo** cairia na inferência
defeituosa, justo na largada do plantão.

> **Régua:** medir latência curta no Windows é `perf_counter`, nunca `monotonic`.
> Um relógio grosso não avisa que é grosso — ele só concorda com a hipótese.

---

## 3. Ligar o TLS quebrou o acesso local (2ª regressão do dia)

Minutos depois do cert, a janela nativa parou no Chrome com
`ERR_CERT_COMMON_NAME_INVALID`. Causa: `main()` cravava
`url = f"{esquema}://127.0.0.1:{porta}"` — e **não se emite certificado público para IP
de loopback**. A única saída era "Avançado → permitir" a cada abertura, guardando
exceção de certificado para usar o próprio app.

**Correção:** `_host_da_janela` (pura) usa o nome do **certificado** em `https` e mantém
o IP em `http`. O nome sai do cert LIDO (`ssl._ssl._test_decode_cert`, **API privada** —
vive num `try`, com queda para o nome do arquivo, e só se ele parecer host: um
`cert.crt` genérico viraria `https://cert:8000`, trocando um aviso por uma janela em
branco).

> **Régua:** ao ligar TLS, procure todo lugar que **CRAVA endereço**. Foi a segunda
> vítima do mesmo tipo no dia — a primeira foi o vigia em `http` puro.

---

## 4. Identidade por aparelho — LIGADA, mas o legado NÃO pode morrer ainda

`MENTE_APARELHOS_HABILITADO=true` no `.env`. O token legado
(`MENTE_APARELHOS_TOKEN_LEGADO`, default `true`) **segue ligado de propósito**.

⛔ **Desligá-lo está bloqueado por um BUILD, não por decisão.** O APK instalado no
celular é o de **03/08**, e sua tela de config tem só "Endereço do servidor" e "Token de
acesso" — **não existe a seção "Pareamento por código"**, que só está no FONTE
(`TelaConfig.kt:89`). Sem recompilar, o celular não tem como ganhar credencial própria,
e matar o legado o trancaria do lado de fora.

**Ordem correta:** recompilar o APK → instalar → parear de verdade → **só então**
`MENTE_APARELHOS_TOKEN_LEGADO=false`.

Registro: **0 de 4 vagas em uso**, 1 revogado no histórico. O navegador do PC também usa
o token legado.

⚠ **Pelo Tailscale o dono não é loopback**, então o gate (`acesso.py`) passa a exigir
credencial. O front lê `?token=` uma vez e guarda no `localStorage`
([index.html:938](templates/index.html:938)).

⚠ **Armadilha paga:** parear chutando o nome de um campo de `settings`
(`aparelhos_credencial_expira_dias`, que NÃO existe) fez o `.get(nome, default)` devolver
o fallback **em silêncio** — credencial com validade de 365 dias contra os 90 da política
(`aparelhos_expira_dias`). Revogada. **`model_dump().get()` com nome errado não falha.**

---

## 5. ⚠ FATO DO AMBIENTE: MIUI barra automação de celular por ADB

Redmi Note 11 Pro 5G (`veux`, Android 13). Com depuração USB autorizada e o aparelho
visível em `adb devices`, **três caminhos foram negados em sequência**:

| tentativa | erro |
|---|---|
| `adb shell run-as <pkg>` | `run-as: /mnt has wrong owner: 0/1000` |
| `adb shell pm clear` | `SecurityException: no CLEAR_APP_USER_DATA` |
| `adb shell input tap/text/keyevent` | `SecurityException: requires INJECT_EVENTS` |

**Não prometa ao dono resolver algo no celular dele por ADB.** O que funciona é
`adb push` (entregar valores num `.txt` em `/sdcard/Download/` para ele copiar) e
LEITURA — `uiautomator dump` foi como a causa raiz apareceu: o campo trazia
`http://192.168.15.13:8000`, IP de LAN e `http` puro, que só resolve dentro de casa.

⚠ Ler o `MENTE_ACCESS_TOKEN` para **exibir no chat** é bloqueado pelo classificador de
permissão do Claude Code. Não contornar.

---

## 6. Dois defeitos do app Android (sessão separada já trabalhando neles)

1. **Tela de boot SEM SAÍDA.** Com endereço inalcançável, ela trava e o
   `uiautomator dump` mostra **zero elementos clicáveis** — não há como chegar em
   `TelaConfig` e corrigir. O dono fica trancado fora do próprio app.
2. **Contador CONGELADO** — parou em "7s" por mais de 2 minutos. Sinal de laço de UI
   preso em chamada de rede **sem timeout de conexão**: endereço de LAN visto de fora
   não recusa, pendura até o TCP desistir. Ver `Servidor.kt` (OkHttp).

Mesma família do *"recusa ≠ queda de rede"* do `CLAUDE.md`, agora como
**"espera ≠ travado"** — a tela não distingue os dois.

---

## 7. Higiene: `dados/certs/` não estava no `.gitignore`

O `.gitignore` enumera as pastas de `dados/` uma a uma e **não tinha `dados/certs/`** —
a **chave privada** do TLS estava a um `git add -A` de ir para o GitHub. Corrigido neste
commit.

---

## Uma suspeita levantada e DERRUBADA

A cobertura do wattímetro marcava 25.170 s num dia com só 18.639 s desde o boot — cara
de contagem dobrada (o defeito que o `CLAUDE.md` prevê). **Não era:** o log de eventos
mostra desligamento às 08:56:52 e boot às 09:26:30, e a janela anterior do mesmo dia
fecha a diferença. **`consumo_diario` é por DIA, não por uptime — comparar com o último
boot inventa um defeito que não existe.**

---

## ▷ Próximos passos, em ordem

1. **Recompilar o APK** com o fonte atual (traz a tela de pareamento) e instalar no
   celular. A sessão que conserta a tela de boot é o lugar natural para isso sair.
2. **Parear o celular de verdade** — `python scripts/aparelhos.py convidar "celular do dono" daniel`
   (código vale 10 min, uso único).
3. **Só então** `MENTE_APARELHOS_TOKEN_LEGADO=false`, e testar antes de confiar.
4. Renovar o cert antes de **~2026-11-02**, reiniciando o vigia junto.
