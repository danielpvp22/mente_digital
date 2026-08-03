# Mente Digital — app Android

**Um clone do `app.py`.** Mesma anatomia, na mesma ordem: uma tela de boot nativa
com progresso real, e depois a **mesma SPA** (`templates/index.html`) num WebView.

Este app **não desenha interface nenhuma**. Foi a correção de rumo de
2026-08-02: a primeira versão recriou o chat em Compose e nunca ficaria igual —
porque era outra coisa. O `index.html` diz no próprio comentário que serve TRÊS
cascas (aba de navegador, janela nativa, container) e que é *"a base do port
Android"*. A casca aqui é isto: uma janela, uma tela de boot e uma ponte.

O que vem de graça por ser o mesmo arquivo: barra lateral de conversas, chips de
VRAM/RAM, "Consolidar", painel avançado (fontes, navegador do vault com filtros,
grafo da malha), palavra-mestre, modo live com o orbe, tema claro/escuro — e
tudo o que for construído amanhã, sem tocar em Kotlin.

## Abrir no Android Studio

1. **File → Open** na pasta **`android/`** (não na raiz — o `settings.gradle.kts`
   está aqui).
2. Espere o *Gradle sync*. O `local.properties` é criado pelo Studio; não é
   versionado, porque o caminho do SDK é da máquina.
3. **Run ▶**. Não há passo de geração.

Requisitos: Android SDK **platform 35** e JDK **21** (o `jbr` do Studio serve).
Versões fixadas e compiladas de verdade: Gradle 9.4.1 · AGP 9.2.1 · Kotlin 2.0.0
· Compose BOM 2024.02.01 · OkHttp 4.12.0 · compileSdk 35 · minSdk 24.

> ⚠ Com **AGP 9** não se aplica `org.jetbrains.kotlin.android` — já vem
> embutido, e aplicá-lo por fora falha com *"Cannot add extension with name
> 'kotlin'"*. `kotlinOptions` some junto.

```powershell
cd android
$env:JAVA_HOME="C:\Program Files\Android\Android Studio\jbr"
.\gradlew.bat :app:assembleDebug
```

## Usar

Com o Mente Digital aberto no PC — ou apenas o vigia de plantão, ver abaixo —, na
tela de configuração:

| Onde o app roda | Endereço |
|---|---|
| **Emulador** | `http://10.0.2.2:8000` — é como ele enxerga o PC |
| **Celular na Wi-Fi** | `http://<ip-do-pc>:8000` (o servidor escuta em `0.0.0.0`) |

O token é o `MENTE_ACCESS_TOKEN` do `.env`. **Testar conexão** bate em
`/api/health` (única rota sem gate) e mostra os serviços — é o que separa
"servidor inalcançável" de "token errado".

### O PC dormindo, e o PC em zero

São **dois** estados do outro lado, e a tela de boot atende os dois.

**Modelos soltos (standby).** Se o `/api/health` disser `descansando`, o app manda
`/api/energia {ligar}` e a espera acontece na tela de boot, com o mesmo anel e
os mesmos pontinhos do desktop. É o "watcher" pelo avesso: em vez de o PC vigiar
a rede esperando o celular, o **celular avisa o PC**.

> ⚠ `descansando` é um campo, não uma dedução. O app deduzia standby de
> `llm == false`, e isso é ambíguo no caso mais comum de todos — durante um boot
> normal o LLM também está em `false`, e o app mandava um `ligar` por cima de um
> carregamento já em curso. A dedução antiga ficou só como compatibilidade com
> servidor velho.

Do outro lado, o PC também dorme **sozinho** depois de 20 min sem uso
(`mente_digital/standby.py`) e avisa as sessões abertas — então o chip de energia
aqui vira "Descansando" sem ninguém tocar em nada.

**Assistente encerrado (o vigia).** Descansar libera a VRAM, mas o processo Python
segue com ~7,7 GB de RAM comprometidos: "de plantão" não era barato o bastante.
Então o `app.py` também **se encerra** depois de 45 min sem uso, e o PC volta a
zero. Quem fica no logon é o **vigia** (`mente_digital/vigia.py`) — um
`http.server` de stdlib pura, sem torch e sem FastAPI, medido em **61 MB**.

O laço de boot pergunta ao vigia exatamente quando o `/api/health` fica
**inalcançável** — que é o estado em que o app antes ficava em "procurando o
servidor…" para sempre, porque não há servidor a procurar:

| o que o vigia responde | o que o app faz |
|---|---|
| servidor já de pé | segue direto para o `/api/health` de sempre |
| `POST /vigia/acordar` aceito | mostra "Acordando o PC…" e fica na tela de carregamento |
| já está subindo | só espera, sem pedir de novo |
| **HTTP 401** | vai para a tela de configuração — token errado é problema de credencial, e mandar esperar seria mentir |
| nada (inalcançável) | o erro honesto de sempre: o PC está fora da rede |

⚠ `acordar` é a **única** rota do vigia que faz algo, e por isso é a única com
gate — foi o pedido, com todas as letras: *só abra o servidor quando for
autenticado*. Um aparelho qualquer da LAN não levanta o assistente de ninguém. O
token continua guardado no aparelho: ninguém digita nada.

O endereço do vigia é **derivado** do que você já configurou: mesmo host, porta
**8765** (`Endereco.vigia`, o default de `vigia_port` no servidor). Não há um
segundo campo na tela de configuração — seria pedir duas vezes a mesma
informação, e um campo a mais para errar. Em compensação, essa porta precisa
estar alcançável: se só a 8000 estiver liberada, o app funciona com o PC de pé e
nunca consegue levantá-lo.

Para deixar o vigia de plantão a cada logon: `python app.py --instalar-inicio` no
PC. (Até 2026-08-02 esse comando instalava o `--standby`; hoje instala o vigia,
que é a camada barata.)

### As utilidades da casca (o menu da bandeja, num telefone sem bandeja)

Aperte **voltar** quando não houver mais para onde voltar dentro da página:

| item | o que faz |
|---|---|
| **Modo economia** | Solta os modelos e devolve a máquina — o PC fica livre. Vai para a tela de repouso; acordar é um toque. |
| **Consolidar agora / Parar** | `/api/idle` — a destilação de fundo, igual ao item da bandeja. |
| **Recarregar a interface** | Recarrega a SPA sem reiniciar o app. |
| **Servidor…** | Trocar endereço ou token (antes não havia caminho de volta). |
| **Sair** | Fecha o app; o servidor fica como está. |

⚠ **O modo economia pode ser RECUSADO, e isso é o certo.** Quem aperta este botão
está longe do PC e não vê o que acontece nele; soltar os modelos no meio de um
turno não derruba a resposta, deixa-a **pior em silêncio** (medido: o
`liberar_vram` levou o embedding junto e a busca de figuras caiu no para-quedas,
entregando resposta degradada sem nada dizer por quê). Agora o servidor **cede a
vez**: espera o turno terminar até 20 s e, se não terminar, devolve
`{adiado:true}`. O app diz *"O PC está respondendo agora — tente de novo em
instantes"* e **não** vai para a tela de repouso, porque o PC não dormiu — ir
seria mentir.

⚠ **O que NÃO está aí, de propósito:** chat, histórico, avançado, tema e o
próprio chip de energia continuam sendo da SPA. Duplicar aqui um botão que o
`index.html` já desenha criaria duas verdades sobre o mesmo estado — o erro que
este app inteiro foi refeito para não cometer.

## Os arquivos que importam

| arquivo | papel |
|---|---|
| `MainActivity.kt` | config → vigia → boot → WebView → repouso. Nada além disso. |
| `Servidor.kt` | `/api/health`, `/api/energia`/`/api/idle` e as duas rotas do vigia, mais os marcos da tela de boot (puros). |
| `ui/FolhaUtilidades.kt` | as utilidades da casca, no botão voltar. |
| `ui/TelaDormindo.kt` | a tela de repouso — o espelho da que o `app.py` mostra. |
| `PonteAndroid.kt` | `window.MenteAndroid`: abre o microfone nativo e entrega o quadro cru à SPA. |
| `Gravador.kt` | `AudioRecord` 16 kHz mono, fatiado em **1024 amostras**. |

### Por que existe uma ponte de microfone

`getUserMedia` só funciona em contexto seguro e o servidor sobe em HTTP — a
própria SPA barra o botão de voz por isso (`index.html:1025`). No app nativo a
restrição não vale: quem grava é o `AudioRecord`, que só depende de
`RECORD_AUDIO`.

⚠ **A ponte entrega só o quadro cru.** RMS, barge-in, mudo e envio continuam
sendo o mesmo JavaScript que o navegador e a janela do PC rodam
(`processarQuadroMic`). Reimplementar isso em Kotlin daria duas noções de "voz
alta" com o mesmo nome, envelhecendo separadas.

⚠ **Um quadro por chamada, nunca um lote.** `vad_min_frames` no servidor conta
MENSAGENS do WebSocket (`ws.py:379, 393-394`), e a página faz um `ws.send` por
chamada. Agrupar mudaria o VAD do servidor sem nada falhar.

## Testar

```powershell
.\gradlew.bat :app:testDebugUnitTest
```

30 testes puros: os marcos da tela de boot (os mesmos de `tests/test_app_boot.py`),
a detecção de standby, a leitura de `/api/energia`, montagem de endereço, e o
**contrato da ponte** — que inclui o decodificador JS do `index.html` portado
linha a linha, para provar que a página remonta exatamente as amostras que o
microfone capturou.

⚠ **Duas coisas puras que ainda não têm teste próprio:** `Endereco.vigia` (a
derivação `host + 8765` a partir do endereço configurado) e a leitura do
`adiado` de `/api/energia`. As duas foram exercitadas rodando, não pela suíte.

## Verificado rodando (emulador Pixel 6, API 35)

Tela de configuração, boot, a SPA idêntica ao desktop ("Olá", cards, chips de
VRAM), o modo live abrindo com o orbe, e o microfone nativo alimentando a página
(`microfone aberto pela SPA` + `16000Hz mono PCM16, quadros de 1024 amostras`).

Reconexão derrubada no meio e observada voltando: backoff de 1 s × 1,6 com teto
de 15 s — **os mesmos números do front** (`index.html:906-910`), de propósito, e
o `set_conversa` do reconnect levando o **mesmo id** da conversa. Enquanto isso a
tela disse a verdade: ponto cinza, "Sem conexão. Tentando de novo…" e o Enviar
desabilitado.

### O ciclo de energia, medido do zero

Assistente encerrado, GPU em 1,7 GB (só o desktop), vigia de plantão. Abrir o app
disparou `[VIGIA] pedido autenticado — subindo o assistente`, a tela de
carregamento mostrou progresso real e a **conversa estava de pé em 96 s**. A
trava também foi exercitada: `POST /vigia/acordar` sem token = HTTP 401, com
token errado = HTTP 401, os dois registrados no log do vigia.

Um defeito que só apareceu com o app rodando: a tela de boot travou em **85%**,
"faltando: Escuta", até o botão de escape dos 150 s. Não era do app — era o
servidor. Dormir **duas vezes seguidas** sobrescrevia a lista do que restaurar
(`=` onde devia ser `|=`), e o Whisper ficava fora **para sempre**, sem erro e
sem log, com a voz simplesmente não funcionando. Corrigido em `state.py`, com o
teste de regressão rodado contra o código *antes* do conserto para provar que ele
pega o defeito.

## Ainda NÃO verificado

- **Aparelho físico** e **fala humana** — o emulador não capta áudio, então o
  barge-in e o cancelamento de eco não foram exercitados com voz.
- **A folha de utilidades e a tela de repouso num CELULAR de verdade.** O ciclo
  inteiro (dormir sozinho, encerrar, o vigia levantar, religar) foi medido ao
  vivo em 2026-08-02 pelo app — mas no emulador, contra o servidor real.
- **Doze**: o serviço em primeiro plano sobe, mas nunca enfrentou a tela apagada
  por horas.
