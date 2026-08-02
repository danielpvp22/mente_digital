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

Com o Mente Digital aberto no PC, na tela de configuração:

| Onde o app roda | Endereço |
|---|---|
| **Emulador** | `http://10.0.2.2:8000` — é como ele enxerga o PC |
| **Celular na Wi-Fi** | `http://<ip-do-pc>:8000` (o servidor escuta em `0.0.0.0`) |

O token é o `MENTE_ACCESS_TOKEN` do `.env`. **Testar conexão** bate em
`/api/health` (única rota sem gate) e mostra os serviços — é o que separa
"servidor inalcançável" de "token errado".

### O PC em standby

Se o servidor responder com os modelos soltos, o app manda
`/api/energia {ligar}` e a espera acontece na tela de boot, com o mesmo anel e
os mesmos pontinhos do desktop. É o "watcher" pelo avesso: em vez de o PC vigiar
a rede esperando o celular, o **celular avisa o PC** — sem porta extra, sem
descoberta, sem processo vigiando.

## Os quatro arquivos que importam

| arquivo | papel |
|---|---|
| `MainActivity.kt` | config → boot → WebView. Nada além disso. |
| `Servidor.kt` | `/api/health` e `/api/energia`, mais os marcos da tela de boot (puros). |
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

22 testes puros: os marcos da tela de boot (os mesmos de `tests/test_app_boot.py`),
a detecção de standby, montagem de endereço, e o **contrato da ponte** — que
inclui o decodificador JS do `index.html` portado linha a linha, para provar que
a página remonta exatamente as amostras que o microfone capturou.

## Verificado rodando (emulador Pixel 6, API 35)

Tela de configuração, boot, a SPA idêntica ao desktop ("Olá", cards, chips de
VRAM), o modo live abrindo com o orbe, e o microfone nativo alimentando a página
(`microfone aberto pela SPA` + `16000Hz mono PCM16, quadros de 1024 amostras`).

## Ainda NÃO verificado

- **Aparelho físico** e **fala humana** — o emulador não capta áudio, então o
  barge-in e o cancelamento de eco não foram exercitados com voz.
- **O caminho de standby ao vivo**: a lógica tem teste, mas nunca foi observada
  acordando um PC realmente descansando.
- **Doze**: o serviço em primeiro plano sobe, mas nunca enfrentou a tela apagada
  por horas.
