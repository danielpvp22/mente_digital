# Mente Digital — cliente Android (Fase 1)

Cliente **magro** do mesmo servidor de sempre. Nenhum modelo roda no telefone: o
vault tem ~27 GB e o LLM é um Qwen3 local na 3080. O app fala o **mesmo**
WebSocket `/ws/chat_live` e as mesmas rotas `/api` que o navegador e a janela
nativa — não há protocolo próprio, nem "endpoint mobile".

Plano completo (com `arquivo:linha` de cada afirmação sobre o servidor):
[`docs/PLANO_APP_ANDROID.md`](../docs/PLANO_APP_ANDROID.md).

## O que a Fase 1 entrega

- Tela de configuração: endereço + token, com **testar** batendo em `/api/health`
  (a única rota sem gate) e mostrando o mapa de serviços prontos.
- Chat por texto ponta a ponta, com a resposta streamando token a token e as
  fontes por baixo da bolha.
- Lista de conversas (`/api/conversas`) e reabertura de uma conversa.
- Reconexão com backoff (1 s × 1,6, teto 15 s), reenviando `set_conversa`.
- Token em `EncryptedSharedPreferences` — e um aviso na tela se o cofre do
  aparelho não estiver disponível.

**Não entrega áudio**, e isso não é lacuna: turno digitado é mudo por default no
servidor (`falar_turno_digitado=False`), então um app só de texto não recebe
mensagem `audio` nenhuma. Voz é a Fase 2.

## Compilar

Precisa do Android SDK (platform 35) e de um JDK 21 — o do Android Studio serve.

```powershell
python scripts\gerar_mipmaps.py    # 1x depois de clonar: desenha os ícones
cd android
$env:JAVA_HOME="C:\Program Files\Android\Android Studio\jbr"
.\gradlew.bat :app:assembleDebug
```

O `local.properties` (caminho do SDK) **não é versionado** — o Android Studio o
cria ao abrir o projeto, ou escreva à mão:
`sdk.dir=C\:\\Users\\<voce>\\AppData\\Local\\Android\\Sdk`.

## Testar

```powershell
.\gradlew.bat :app:testDebugUnitTest
```

25 testes puros (parser do protocolo, montagem de endereço, backoff) que rodam
na JVM, sem emulador.

Mais 4 testes de **conformidade contra o servidor de verdade**, que se pulam
sozinhos sem as variáveis de ambiente:

```powershell
$env:MENTE_BASE="http://127.0.0.1:8000"
$env:MENTE_TOKEN="<o token do .env>"
.\gradlew.bat :app:testDebugUnitTest --tests "*ConformidadeServidorTest*"
```

Eles existem porque o servidor **ignora quadro desconhecido em silêncio**
(ws.py:426-502 não tem `else`): um campo com o nome errado passaria em todo teste
de unidade e simplesmente não funcionaria. Foi um deles que descobriu que token
errado devolve **HTTP 403 no handshake**, e não o close 1008 que o plano previa.

## O que NÃO foi verificado

⚠ **O app nunca rodou.** Não há imagem de sistema nem AVD nesta máquina, e nenhum
aparelho conectado — instalar exigiria baixar ~1,5 GB de imagem do emulador. O que
está provado é: compila (APK de 9,1 MB), os 29 testes passam, e a camada de
protocolo conversa com o servidor real. A **interface** (Compose) não foi vista
por olho nenhum.

Primeiro passo para provar o resto:

```powershell
adb install app\build\outputs\apk\debug\app-debug.apk
```

No emulador o endereço do PC é `http://10.0.2.2:8000`; num aparelho na LAN, o IP
da máquina.
