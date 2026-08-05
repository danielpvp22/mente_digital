# HANDOFF — 2026-08-04 (9ª sessão) · o celular parou de mentir, e o token legado pode morrer

> Documento autônomo: quem ler isto não participou da sessão.
> Repo: `danielpvp22/mente_digital`. `master` = **`0b2e324e`**.
> PRs **[#103](https://github.com/danielpvp22/mente_digital/pull/103)** e
> **[#104](https://github.com/danielpvp22/mente_digital/pull/104)** mergeados, CI verde.
> Suíte: **2112** (Python) + **57** (Android, `:app:testDebugUnitTest`).
> **Supersede** o `HANDOFF_2026-08-04_tailscale_e_tela.md` (apagado neste commit).

## O pedido

*"Corrigir um problema que faz a contagem de segundos de load travar — dá a sensação
de que o programa inteiro travou, quando é só o contador."*

Estava certo no diagnóstico e curto no alcance: o mesmo defeito trancava o dono
fora do app.

---

## 1. O contador não era um relógio

`MainActivity` fazia `segundos += 1` a cada **volta** do laço de boot e a tela
exibia `× 0,7 s` — o valor do `delay`, como se a volta custasse só ele. Mas cada
volta faz de 1 a 3 chamadas de rede **bloqueantes** antes do delay. Com o servidor
inalcançável a volta custa ~5,8 s, então o número mentia duas vezes ao mesmo tempo:

- **congelava** durante a rede;
- **contava uma fração** do tempo real (medido no Redmi: "7s" parados por 2 min).

**Conserto:** `Boot.segundosDecorridos` (puro) + relógio em **coroutine própria**,
a partir de `elapsedRealtime` (e não `currentTimeMillis`, que salta com NTP/fuso).
E a sonda de saúde saiu do cliente de **200 s de leitura** — que existe para o
`ligar` carregar modelo — para o `httpCurto` com `callTimeout`.

> **Régua:** um número que só anda quando a rede responde descreve a REDE, não o
> tempo. Quem olha a tela não lê "a rede está lenta", lê "travou".

## 2. A tela de boot não tinha saída — pelo MESMO defeito

A saída de emergência pendia do contador quebrado: eram **215 voltas** para marcar
"150 s", quase meia hora de parede. O `uiautomator dump` de ontem mostrou **zero
elementos clicáveis** nessa tela.

**Conserto:** `Boot.oferta` (puro, três entradas) decide, e a tela só pinta:

| situação | oferta |
|---|---|
| alcançou, faltam serviços | `ENTRAR_ASSIM_MESMO` aos 150 s — **nunca** `CONFIGURAR` |
| não alcançou, **vigia respondeu** | nada até 150 s — a espera é legítima |
| não alcançou, **ninguém respondeu** | `CONFIGURAR` aos **20 s** |

⚠ **Quem compra o tempo é o VIGIA, não o relógio.** Com o assistente encerrado, ele
o levanta e o `/api/health` fica mudo por ~45 s legítimos; oferecer "reveja a
configuração" aí é mandar mexer no que está certo. É o *"recusa ≠ queda de rede"*
do projeto, na forma **"espera ≠ travado"**.

---

## 3. ⚠ A medição que eu quase vendi como prova — e não era

A primeira medição do contador deu 1:1 e eu a apresentei como prova do conserto.
**Não era.** Ela foi feita com o endereço não resolvendo, e nesse caso a sonda
falha em ~1,5 ms (DNS): com sonda instantânea o laço roda a 700 ms por volta e o
**código antigo também daria ~1:1**. Ela não discriminava as duas versões.

Refeita com a porta **engolida no firewall** (drop silencioso), condição conferida
ANTES de medir — a mesma requisição passou de **147 ms para 12 s**:

| | valor |
|---|---|
| parede | 39,1 s |
| visor | 2 s → 42 s |
| razão | **1,02** |
| amostras sem avanço | **zero** em 14 leituras |

E para a saída: com 8000 **e** 8765 bloqueadas, aos 28 s a tela mostrou a frase e o
botão, e o dump passou de zero para **exatamente um** nó clicável, `[308,1802][773,1934]`,
contendo o rótulo `[374,1841][707,1896]`.

> **Régua:** medir no caminho FÁCIL não prova conserto de caminho DIFÍCIL. Antes de
> medir, prove que a condição patológica existe — aqui, que a sonda realmente
> bloqueia. Sem isso o teste concorda com qualquer hipótese.

⚠ **Avisar antes de bloquear.** O dono estava com o aparelho na mão e viu o celular
"travado" sem saber que era teste. Mexer no firewall e deixar o aparelho dele sem
conexão se anuncia antes, não se explica depois.

---

## 4. Celular pareado DE VERDADE (passo 2 do handoff anterior, fechado)

`f760959a8709f70a` · usuário `daniel` (mestre) · expira **2026-11-02** — os 90 dias
corretos da política (a armadilha dos 365 dias não se repetiu, porque o pareamento
foi pela ROTA e não pela camada de baixo).

**2 de 4 vagas**: o celular e `bf95a3351c0a5baa` (navegador do PC, atalho
`ENTRAR_Mente_Digital.html` na área de trabalho — revogar se não for usado).

### ⚠ Fatos do ambiente descobertos aqui

- **A MIUI mata o Tailscale em segundo plano.** Ele aparecia "Connected" na tela
  com **processo nenhum** rodando (`ps -A | grep tailscale` vazio, sem `tun`), e só
  reconectou quando o app foi trazido à frente. Sem *Autostart* + isenção de
  bateria, o acesso remoto morre sozinho. **Ainda pendente — ato do dono.**
- **A MIUI segue barrando `input text`** (`SecurityException: INJECT_EVENTS`).
  Funciona: `am start`, `am force-stop`, `pm list`, `uiautomator dump`, `adb install`.
- **Os ajustes do app se perderam** (endereço e token vazios após a reinstalação).
  A gravação é `apply()` — assíncrona —, e a MIUI provavelmente matou o app antes do
  disco. Depois do pareamento, `force-stop` + reabrir confirmou que persistiu.
- **O endereço de LAN não serve mais.** O cert cobre o nome do Tailscale, então
  `https://192.168.15.13:8000` é rejeitado por nome inválido. **Tailscale ligado é
  obrigatório até dentro de casa.**

---

## 5. O token legado: estava BLOQUEADO por código, não por decisão

Desligar `MENTE_APARELHOS_TOKEN_LEGADO` trancava o dono fora do próprio aplicativo:

- [app.py](app.py) abria a janela sempre em `?token={access_token}` — o **legado**;
- [index.html:940](templates/index.html:940) **sobrescreve** o `localStorage` com o
  `?token=` da URL a cada abertura.

Logo, nem parear a janela resolvia: a credencial boa era apagada pelo token morto na
abertura seguinte.

**Conserto:** `credencial_da_janela` (pura) prefere `MENTE_JANELA_CREDENCIAL` e CAI
no `access_token` — quem não configurou nada continua byte a byte como estava.
O app **não** emite credencial para si mesmo (parear é ato do dono):
`scripts/parear_janela.py`, que vai pela ROTA e grava direto no `.env` sem imprimir.

## 6. O certificado se renova sozinho

`scripts/renovar_cert.py` + Tarefa Agendada **"MenteDigital - Renovar cert Tailscale"**
(diária, 03:30, `StartWhenAvailable`), registrada e **executada uma vez de verdade**:
`LastTaskResult=0`, log em `dados/logs/renovacao_cert.log`.

- **O gatilho é a VALIDADE, não o calendário** — roda todo dia, só age abaixo de 30
  dias restantes. Uma execução perdida não vira cert vencido.
- **O vigia reinicia junto**, e não é zelo: ele serve TLS com o mesmo par e o lê no
  *start*. Foi o bug de ontem.
- Validade **ilegível renova** em vez de assumir folga.

⚠ **`vigia.subir_app` NÃO sobe o vigia** — apesar do nome, levanta o *assistente*
(`app.py --oculto`). Aviso deixado no código.

---

## ▷ Próximos passos, em ordem

1. **Tailscale em Autostart + sem restrição de bateria na MIUI** — ato do dono. Sem
   isto os dois consertos de hoje não salvam: o app dirá corretamente "sem resposta
   do PC" porque a VPN caiu sozinha.
2. `python scripts/parear_janela.py` → reiniciar o app → **só então**
   `MENTE_APARELHOS_TOKEN_LEGADO=false`. Testar antes de confiar.
3. Revogar `bf95a3351c0a5baa` se o navegador do PC não for usado (libera 1 de 4).
4. O cert se cuida sozinho; conferir o log em novembro para ver a renovação real.
