# Mente Digital — cliente Android (Fase 1)

Cliente **magro** do mesmo servidor de sempre. Nenhum modelo roda no telefone: o
vault tem ~27 GB e o LLM é um Qwen3 local na 3080. O app fala o **mesmo**
WebSocket `/ws/chat_live` e as mesmas rotas `/api` que o navegador e a janela
nativa — não há protocolo próprio, nem "endpoint mobile".

Plano completo (com `arquivo:linha` de cada afirmação sobre o servidor):
[`docs/PLANO_APP_ANDROID.md`](../docs/PLANO_APP_ANDROID.md).

---

## Abrir no Android Studio

1. **File → Open** e escolha a pasta **`android/`** (não a raiz do repositório —
   é aqui que está o `settings.gradle.kts`).
2. Espere o *Gradle sync*. O Android Studio cria o `local.properties` com o
   caminho do seu SDK sozinho; ele **não** é versionado de propósito, porque o
   caminho é da máquina.
3. **Run ▶**. Não há passo de geração, nem script para rodar antes: os ícones já
   estão no repositório.

**Requisitos** (o Android Studio instala pelo *SDK Manager* se faltar):
Android SDK **platform 35** · JDK **21** (o `jbr` embutido no Studio serve).

Versões fixadas e **compiladas de verdade** nesta máquina — não são chute:
Gradle 9.4.1 · AGP 9.2.1 · Kotlin 2.0.0 · Compose BOM 2024.02.01 · OkHttp 4.12.0
· compileSdk 35 · minSdk 24.

> ⚠ Com **AGP 9** *não* se aplica o plugin `org.jetbrains.kotlin.android`: ele já
> vem embutido, e aplicá-lo por fora falha com *"Cannot add extension with name
> 'kotlin'"*. `kotlinOptions` também deixou de existir.

### Pela linha de comando

```powershell
cd android
$env:JAVA_HOME="C:\Program Files\Android\Android Studio\jbr"
.\gradlew.bat :app:assembleDebug
```

---

## Testar na prática

1. Abra o Mente Digital no PC (`python app.py`, ou `python main.py` só para o
   servidor).
2. Instale e abra o app.
3. Na tela de configuração, preencha o **endereço** e o **token**
   (`MENTE_ACCESS_TOKEN` do seu `.env`) e toque em **Testar conexão** — ele bate
   em `/api/health`, que não tem gate, e mostra quais serviços subiram.

| Onde o app roda | Endereço a usar |
|---|---|
| **Emulador** | `http://10.0.2.2:8000` — é como ele enxerga este PC. `localhost` seria o próprio Android. |
| **Celular na mesma Wi-Fi** | `http://<ip-do-pc>:8000` (o servidor já escuta em `0.0.0.0`) |

Depois é **Salvar e entrar**. O token fica no `EncryptedSharedPreferences` e não
é pedido de novo.

### Emulador pela linha de comando

```powershell
& "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator.exe" -avd <nome> -no-snapshot
& "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe" install -r app\build\outputs\apk\debug\app-debug.apk
```

---

## O que a Fase 1 entrega

- Configuração com teste de conexão e mapa de serviços.
- Chat por texto ponta a ponta, resposta streamando token a token, fontes por
  baixo da bolha.
- Lista de conversas (`/api/conversas`) e reabertura de uma conversa — o mesmo
  histórico do desktop.
- Reconexão com backoff (1 s × 1,6, teto 15 s), reenviando `set_conversa`.
- Token em `EncryptedSharedPreferences`, com aviso na tela se o cofre do
  aparelho não estiver disponível.

**Não entrega áudio**, e isso não é lacuna: turno digitado é mudo por default no
servidor (`falar_turno_digitado=False`), então um app só de texto não recebe
mensagem `audio` nenhuma. Voz é a Fase 2.

---

## Testar (código)

```powershell
.\gradlew.bat :app:testDebugUnitTest
```

25 testes puros (parser do protocolo, montagem de endereço, backoff) que rodam na
JVM, sem emulador.

Mais 4 de **conformidade contra o servidor de verdade**, que se pulam sozinhos
sem as variáveis de ambiente:

```powershell
$env:MENTE_BASE="http://127.0.0.1:8000"
$env:MENTE_TOKEN="<o token do .env>"
.\gradlew.bat :app:testDebugUnitTest --tests "*ConformidadeServidorTest*"
```

Eles existem porque o servidor **ignora quadro desconhecido em silêncio**
(`ws.py:426-502` não tem `else`): um campo com o nome errado passa em todo teste
de unidade e simplesmente não funciona. Dois defeitos reais saíram daí — ver
abaixo.

---

## Defeitos que só apareceram RODANDO (e o que ensinaram)

1. **Token errado devolve HTTP 403, não close 1008.** O gate roda antes do
   `accept()`, então o uvicorn nem faz o upgrade. O plano dizia 1008; o app que
   só tratasse isso reconectaria em laço para sempre. Corrigido em
   `ClienteMente.onFailure`.
2. **Conversa reabria VAZIA.** Os campos de `/api/conversa/{id}` são `q`/`a`/`t`,
   e eu havia escrito `pergunta`/`resposta` por dedução. Como `optString`
   devolve `""` para chave ausente, não havia exceção nem log — só tela em
   branco. O teste de integração passava porque só checava que a chamada era
   *aceita*: **teste de integração que não olha o conteúdo prova só que o
   servidor atendeu o telefone.**
3. **A barra do app ficava por baixo da barra de status.** Com `targetSdk 35` o
   Android 15 força *edge-to-edge*, e sem `statusBarsPadding()` os botões do topo
   não recebiam toque nenhum.

## O que ainda não foi verificado

- Aparelho **físico** (só emulador Pixel 6, API 35).
- **Reconexão** de verdade (derrubar o servidor no meio e ver o backoff agir).
- Tema **escuro** — o app segue o do sistema, e o emulador estava no claro.
- Push `proativo` chegando com o app aberto.
