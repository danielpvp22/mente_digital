# HANDOFF — 2026-08-03 (fim do dia) · acesso remoto

> Documento autônomo: quem ler isto não participou da sessão. Repo:
> `danielpvp22/mente_digital`. **Supersede os dois handoffs anteriores do dia**
> (`_ci_verde` e `_aparelhos_e_tls`), cujos itens estão TODOS fechados — os dois
> arquivos foram apagados.

---

## 1. ESTADO QUE VOCÊ HERDA

`origin/master` = **`76e0e05d`**, **CI VERDE** nos dois jobs (run
[`30817837272`](https://github.com/danielpvp22/mente_digital/actions/runs/30817837272)).
**Nada em aberto no código.**

Os dois PRs do dia estão mergeados:
[#86](https://github.com/danielpvp22/mente_digital/pull/86) (a fiação da identidade
por aparelho, inerte com a flag `false`) e
[#87](https://github.com/danielpvp22/mente_digital/pull/87), que fechou os **quatro**
itens do handoff anterior mais dois consertos de segurança que uma revisão
adversária do próprio PR achou (§2.6). Números no master: **1.793 passed**,
cobertura **82,32%** (piso 77%), `ruff check .` limpo, bandit limpo, zero testes
pulados, Kotlin **45 testes** (eram 30).

**Ramos locais que sobraram** (nenhum é trabalho pendente): `feat/ligar-aparelhos-e-tls`
já está no master (#86) e pode sair com `git branch -d`; `perf/modo-live-correcoes`
está no master por SQUASH, então `--no-merged` o lista por ancestralidade e apagá-lo
exige `-D` (ver a armadilha no handoff de 03/08 anterior: `git diff A...B` não
responde "o que falta em A").

Nada mais em aberto no código. O que falta para o dono usar de fora de casa é
**ato dele**, não código — ver §3.

---

## 2. O QUE FOI FEITO, E O QUE VALE GUARDAR DE CADA UM

### 2.1 O achado que mudou o desenho do conserto

O handoff anterior pedia: *"`index.html`: close **1008** = REVOGADO"*. Antes de
escrever a linha, medi o que o navegador **realmente** recebe (uvicorn + Chrome,
`close(1008)` nas duas posições possíveis do gate):

| recusa | o que o JS recebe |
|---|---|
| **antes** do `accept` | `code=1006`, `reason=""` — byte a byte o WiFi caído |
| **depois** do `accept` | `code=1008`, com motivo |

O uvicorn responde **HTTP 403 ao handshake** quando a app fecha antes de aceitar,
e o código de fechamento **não chega ao JS**. Como o caso mudo é justamente o do
**aparelho revogado reabrindo o app**, tratar só o 1008 consertaria a metade rara.

> **Régua:** o código de fechamento do WebSocket só existe depois do `accept`.
> Recusa pré-accept é indistinguível de rede caída — quem responde "é recusa ou é
> rede?" é o HTTP, que tem corpo.

Daí `GET /api/acesso` (gateada, a rota mais barata do `main.py`): quando o socket
não abre, o front pergunta em HTTP e só então para o laço.

### 2.2 O plantão estava um degrau atrás — nos dois sentidos

Não era só "o revogado ainda acorda o PC". O celular **já migrado** (que apagou o
token antigo) **não acordava nada**: batia no plantão e ouvia 401. Um gate velho
erra nas duas direções, e a segunda passa despercebida porque ninguém a testa.

### 2.3 Ligar o TLS quebraria acordar o PC, em silêncio

O app Android **deriva** o endereço do vigia do endereço do assistente trocando só
a porta — **inclusive o esquema** (`Endereco.vigia`). Servidor em `https` + plantão
em `http` puro = o app fala TLS com um socket que não fala, e a tela diz "o PC está
desligado". Seria a ambiguidade que o vigia existe para matar, ressuscitada pelo
conserto de outra coisa. O plantão agora usa o mesmo `MENTE_SSL_CERT/KEY`.

> **Régua:** ao ligar TLS, procure quem DERIVA endereço de quem. O erro não
> aparece onde você mexeu.

### 2.4 Três suposições minhas sobre o servidor estavam erradas

Descobertas lendo o código antes de escrever o cliente Android — as três
quebrariam o app **em silêncio**:

1. A chave do corpo de erro do pareamento é **`erro`**, não `motivo`
   (`main.py`: `content={"erro": r.motivo}` — o nome do campo Python virou o
   VALOR). Ler `motivo` daria string vazia em toda recusa.
2. `MOTIVO_TETO` **vale** `"teto_aparelhos"`, mas a rota irmã
   `/api/aparelhos/convite` devolve `{"erro": "teto"}`. Duas grafias, mesmo
   arquivo, rotas diferentes — copie o VALOR, nunca o nome da constante.
3. **Toda** falha do pareamento é 401, inclusive teto e bloqueio. Não dá para
   discriminar pelo status.

### 2.6 A revisão adversária derrubou um argumento MEU — e achou um buraco velho

Rodei um revisor de segurança independente sobre o próprio PR. Dois achados, os
dois válidos:

**(a) Meu fail-soft de TLS no plantão era uma regressão silenciosa.** Eu havia
copiado o padrão do `main.py` — cert quebrado vira aviso e HTTP, "porque vigia mudo
é pior que vigia em claro". Não se sustenta: com o servidor em TLS, o celular
**também** deriva `https://` para o plantão, então o HTTP puro **já está mudo para
ele**. O fallback não salvava ninguém e deixava a credencial de acordar em claro
num socket que escuta em `0.0.0.0`. Pior, o aviso era um `print`, e o único caminho
de "sobe com o Windows" (`inicializacao.script_vbs` → `sh.Run …, 0, False`) não tem
console nem redirecionamento — conferido nos dois arquivos. Agora o plantão
**recusa subir** e deixa o motivo em `dados/vigia_erro.txt`.

> **Régua:** copiar um fail-soft de outro módulo copia junto a premissa dele. O do
> `main.py` vale porque o servidor tem console e um humano na frente; o do plantão
> não tinha nem um nem outro.

**(b) O motivo `revogado` vazava para quem só conhecia o `id`** — pré-existente no
`master`, mas era a minha rota nova que o herdava. O `id` é público (tela de
pareamento, `/api/aparelhos`) e a credencial é `mdk1.<id>.<segredo>`: bastava
montar `mdk1.<id conhecido>.<lixo>` e o servidor confirmava a revogação sem que
ninguém acertasse segredo algum. O comentário do código afirmava "quem recebe isto
já tinha a credencial" — e não era verdade. Consertado com `Veredito.provou_posse`,
**sem mexer na ordem das checagens** e **sem perder granularidade na auditoria**.

### 2.5 Dívida registrada de propósito

O motivo da recusa sai em **duas formas**: `{"detail": {"erro":…, "motivo":…}}` no
gate e `{"erro": "<motivo>"}` no pareamento. Cada cliente lê a sua. Unificar
quebraria o Kotlin recém-escrito sem ganho imediato — mas é a próxima coisa a
arrumar se alguém for mexer nesse contrato.

---

## 3. ▷ O QUE FALTA — e é ATO DO DONO, não código

**Usar de fora da rede.** Roteiro completo em
[docs/ACESSO_REMOTO.md](docs/ACESSO_REMOTO.md). Resumo:

1. Tailscale no PC e no celular, mesma conta (**medido em 2026-08-03: não está
   instalado nesta máquina**).
2. No admin console: **MagicDNS** e **HTTPS Certificates**.
3. `tailscale cert maquina.SUA-TAILNET.ts.net` → apontar `MENTE_SSL_CERT/KEY`.
4. No app, endereço `https://maquina.SUA-TAILNET.ts.net:8000`.

Os dois tropeços prováveis, nesta ordem: o certificado vale para o **nome
MagicDNS, não para o IP `100.x`** (usar o IP falha a verificação de nome e parece
o PC fora do ar), e ele **expira em 90 dias com renovação manual** — vencido, o
assistente para de atender em HTTPS **sem aviso**.

**Watts da CPU:** inalterado — a GPU funciona de graça (NVML); a CPU fica `null`
com motivo `ausente` até o dono seguir `docs/INSTALACAO_WATTS.md`. O agente **não
instala** o driver de kernel.

---

## 4. ⚑ ARMADILHAS QUE ESTA SESSÃO PAGOU

**`cryptography` NÃO está nesta env.** A 1ª versão do teste de TLS usava
`pytest.importorskip("cryptography.x509")` e se **pulava em silêncio** — eu quase
reportei como prova um teste que não rodou. Vale a régua geral: depois de rodar a
suíte, **olhe os `s` de skip** (`pytest -rs`) antes de dizer que algo está provado.
O cert agora vem do `openssl` do PATH (Git for Windows aqui, presente no runner
Linux), então a prova roda nos dois.

**`cfg` não é global no `app.py`.** Escrevi `cfg.ssl_cert` numa função onde ele não
existe (é local em dois outros lugares). O ruff não pega, e o caminho só roda no
encerramento por ociosidade — teria explodido semanas depois, sozinho, de
madrugada.

**Número que não foi conferido.** Escrevi "1789 passed" na mensagem de commit e a
suíte deu **1788**. O dono confere; corrigi antes de commitar. *Nunca reporte um
total sem ter olhado.*

**A régua da sessão anterior continuou valendo:** antes de concluir qualquer coisa
medida no navegador, **afirme a viewport** (`if (!innerWidth) return {ABORTA}`) —
aba que não compõe quadros devolve medida lixo.

---

## 5. 🔧 AMBIENTE

```bash
C:\ProgramData\miniconda3\envs\llama-omni\python.exe -m pytest
```
O `python` do PATH é o atalho falso da Microsoft Store. Suíte ~47 s, sem GPU/rede.

**PowerShell 5.1 mutila aspas** — mensagem de commit em arquivo + `git commit -F`.

**APK:** `$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"` (o `java`
do PATH é 1.8). O APK **não muda** com trabalho de servidor: o app é uma casca
sobre a mesma SPA, e as melhorias chegam ao celular **recarregando**.

---

## 6. MÉTODO QUE O DONO COBRA

Ele confere o que o agente afirma. **Medir antes, e dizer o que NÃO foi medido.**
Quer entender o **porquê** das decisões. Chamá-lo de **"mister"** / "mister
Daniel", não "senhor".

Nesta sessão, medir antes de escrever mudou o desenho **duas** vezes (o 1006 do
handshake, e o `cryptography` ausente) e evitou um defeito que só apareceria
semanas depois (o `cfg`).

> *Arquivo avulso e não versionado — apague quando não precisar mais.*
