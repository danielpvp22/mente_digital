# Plano — App Android nativo (Kotlin)

**Data:** 2026-08-02 · **Estado:** **Fase 1 CONSTRUÍDA** (`android/`) · **Branch de referência:** `feat/ocr-livro-escaneado-fase3`

> ## ⚠ CORREÇÃO MEDIDA NA EXECUÇÃO DA FASE 1 (2026-08-02)
>
> **Este plano está ERRADO sobre o que o cliente vê quando o token não confere.**
> §1.1 e o Risco R5 afirmam que o cliente "verá apenas um close 1008". Sondado o
> servidor de verdade com um handshake cru:
>
> ```
> token certo  -> HTTP/1.1 101 Switching Protocols
> token errado -> HTTP/1.1 403 Forbidden
> sem token    -> HTTP/1.1 403 Forbidden
> ```
>
> O gate roda **antes** do `accept()` (main.py:538-548), então o uvicorn nunca faz
> o upgrade: o `websocket.close(1008)` do lado do servidor vira uma **recusa de
> handshake HTTP**. No OkHttp isso chega em `onFailure` com `response.code == 403`
> — `onClosed` **jamais** é chamado, e um app que só tratasse 1008 ficaria
> reconectando em laço eterno sem nunca dizer que o problema é o token.
>
> Corrigido em `ClienteMente.onFailure`, com teste de integração que roda contra o
> servidor real (`ConformidadeServidorTest`). A recomendação de usar `/api/health`
> para desambiguar (R5) continua válida e foi implementada.
>
> **Outra correção, de versões:** §7 dizia "NÃO VERIFIQUEI versões". Fixadas e
> compiladas nesta máquina: Gradle 9.4.1, **AGP 9.2.1**, Kotlin 2.0.0, Compose BOM
> 2024.02.01, OkHttp 4.12.0, compileSdk 35, minSdk 24, JDK 21 (o JBR do Studio).
> ⚠ Com AGP 9 **não se aplica o plugin `org.jetbrains.kotlin.android`** — ele já
> vem embutido, e aplicá-lo por fora falha com *"Cannot add extension with name
> 'kotlin'"*. `kotlinOptions` também sai junto.

Este documento levanta o protocolo REAL do servidor (com arquivo:linha) e propõe o
plano de execução do cliente Android. Nada aqui altera código do servidor: o app
nativo é a versão Kotlin de um cliente magro que **já existe e funciona**
(`python app.py --remoto http://host:8000`, app.py:5 e app.py:711-713).

## Premissas já decididas com o dono (não se rediscutem aqui)

1. **O celular é cliente magro.** Nenhum modelo roda no telefone. O vault tem
   26,94 GB e o LLM é um Qwen3 local na 3080; isso não vai para o aparelho, hoje
   nem nunca.
2. **O protocolo é o que a web já usa**: WebSocket `/ws/chat_live` + rotas
   `/api/*`. O app não inventa protocolo, não ganha rota própria, não pede
   "endpoint mobile".
3. **A referência de comportamento é `templates/index.html`.** Quando este
   documento diz "replicar o navegador", quer dizer literalmente: fazer o que
   aquele arquivo faz, porque é contra ele que todo o servidor foi calibrado.

---

# Parte I — O que foi levantado no código

## 1. Protocolo do WebSocket `/ws/chat_live`

Endpoint: `main.py:535-549`. Máquina de estados: `mente_digital/ws.py`, classe
`LiveSession` (ws.py:49).

### 1.1 Handshake

O gate roda **ANTES do `accept()`** (main.py:538-548). Falha fecha com
**código 1008** (policy violation) e nada mais é enviado — não há corpo de erro,
não há mensagem JSON de recusa. O cliente Android verá apenas um close 1008 e
precisa traduzir isso para "token errado ou aparelho não autorizado" na tela.

Duas checagens, nesta ordem:

| Ordem | Checagem | Código | Origem |
|---|---|---|---|
| 1 | `acesso.cliente_autorizado(host, token, settings.access_token)` | 1008 | main.py:541-545 |
| 2 | `acesso.origin_confere(origin, host_header)` | 1008 | main.py:546-548 |

A segunda só morde se o cliente **mandar** header `Origin` (acesso.py:37-44:
`if not origin: return True`). Um cliente nativo não manda Origin, então passa.
**Consequência prática para o app: não mande `Origin`.** Se o OkHttp for
configurado para mandar um Origin qualquer, ele terá de bater exatamente com o
header `Host` — caso contrário o servidor fecha em 1008 e a causa é invisível.

Após o accept (ws.py:171-188), o servidor:
- adiciona a sessão em `ctx.sessoes` (ws.py:173);
- entrega em background os avisos que dispararam com ninguém conectado
  (ws.py:180-181) — ou seja, **um lembrete de ontem chega segundos depois do
  connect**, antes de qualquer mensagem do usuário;
- se o LLM estiver descarregado, dispara `ensure_loaded()` e envia
  `{"tipo":"status","texto":"Modelo religando..."}` (ws.py:184-188).

### 1.2 Cliente → Servidor: quadros BINÁRIOS (áudio)

Consumidos em `ws._on_audio` (ws.py:311-379), via `run()` em ws.py:202-203.

```python
pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0   # ws.py:312
```

Formato exato, e por que cada item é obrigatório:

| Propriedade | Valor | Onde se confirma |
|---|---|---|
| Codificação | PCM linear com sinal, **16 bits** | `dtype=np.int16` — ws.py:312 |
| Endianness | **little-endian** | `np.int16` é o tipo nativo; o servidor roda x86-64 (Windows/RTX 3080). Não há `dtype='>i2'` em lugar nenhum. |
| Canais | **1 (mono)** | O buffer é tratado como vetor plano; não há deinterleave em ws.py nem em audio.py |
| Taxa | **16.000 Hz** | Não é negociada nem validada pelo servidor. Vem do cliente: `AudioContext({sampleRate:16000})` — index.html:961 |
| Container | **nenhum** — bytes crus, sem cabeçalho WAV | `np.frombuffer(raw, ...)` sobre o payload inteiro |
| Tamanho do quadro | **1024 amostras = 2048 bytes = 64 ms** | `createScriptProcessor(1024,1,1)` — index.html:964; a aritmética está no comentário de index.html:974 |

**A taxa de 16 kHz é um contrato tácito, sem validação e sem reamostragem em
nenhum lado.** `SttService.transcribe` (audio.py:266-284) entrega o array direto
ao `faster-whisper`, que assume a taxa nativa do modelo. Mandar 44,1 kHz não dá
erro: dá transcrição errada, com a fala acelerada. É uma falha silenciosa, que é
exatamente o tipo de defeito que este projeto mais combate — então o app tem de
gravar em 16 kHz na origem, não reamostrar depois.

**⚠ O achado mais importante desta seção: `vad_min_frames` e `barge_min_frames`
contam MENSAGENS, não tempo.**

`_on_audio` faz um `append` por mensagem recebida (ws.py:379), e `_check_silence`
descarta o turno com `if len(buffer) < settings.vad_min_frames` (ws.py:393-394).
Com os defaults (`vad_min_frames = 15`, config.py:997) e o quadro de 64 ms do
navegador, isso é **960 ms de fala mínima**. O mesmo vale para o barge-in do
servidor: `barge_min_frames = 8` (config.py:1008) × 64 ms ≈ **512 ms de fala alta
sustentada**.

Se o app Android usar o `AudioRecord.getMinBufferSize()` como tamanho de leitura
(que varia por aparelho), esses dois limiares mudam de significado sem que nada
falhe visivelmente: um buffer de 4096 amostras faria `vad_min_frames` exigir
**3,8 s** de fala para o turno existir, e perguntas curtas seriam silenciosamente
descartadas. **O app deve fatiar em blocos fixos de 1024 amostras (2048 bytes)
antes de enviar**, independente do buffer interno do `AudioRecord`.

O RMS também é calculado por mensagem (ws.py:313), então o tamanho do quadro
altera a suavização do detector de início de fala. Mais uma razão para fixar 1024.

### 1.3 Cliente → Servidor: quadros de TEXTO (JSON)

Roteados em `ws._on_text` (ws.py:426-502). JSON inválido gera um warn e é
ignorado (ws.py:429-431). **`tipo` desconhecido é ignorado em silêncio** — não há
`else` na cadeia; o app não recebe erro se escrever um tipo errado.

| Mensagem | Campos | Efeito | Linha |
|---|---|---|---|
| `{"tipo":"texto","payload":"<str>"}` | `payload`: string, trim; vazio = no-op | Ecoa `transcricao`, grava no dump, religa o LLM, abre o pipeline | ws.py:485-502 |
| `{"tipo":"barge_in"}` | — | Zera gravação e buffer, corta TTS **e** pipeline | ws.py:434-438 |
| `{"tipo":"ack_proativo","id":"<str>"}` | `id`: o `ack_id` recebido | Só isto conclui/reprograma o lembrete no scheduler | ws.py:440-446 |
| `{"tipo":"end_session"}` | — | Dispara o ETL idle (com carência/debounce) | ws.py:448-449 |
| `{"tipo":"set_conversa","id":"<str>"}` | `id` | Reassocia a conversa (usado no reconnect) | ws.py:451-459 |
| `{"tipo":"nova_conversa","id":"<str>"}` | `id` | Id novo + contexto limpo + corta TTS/pipeline | ws.py:461-472 |
| `{"tipo":"carregar_conversa","id":"<str>"}` | `id` | Recarrega os turnos do SQLite na RAM da sessão | ws.py:474-483 |

O `id` de conversa é gerado **pelo cliente** (index.html:598, 1221 — função
`novoId()`), não pelo servidor. O app Android gera o seu (UUID serve) e é dono
do ciclo de vida.

Sequência que o navegador usa e que o app deve copiar:
- no `onopen`: `set_conversa` com o id corrente (index.html:825) — é o que impede
  que a reconexão jogue turnos na conversa errada (ver o comentário em ws.py:74-78,
  que descreve o bug histórico);
- em "novo chat": `end_session` e depois `nova_conversa` (index.html:597-599);
- ao fechar o modo voz com o botão de encerrar: `end_session` (index.html:945).

### 1.4 Servidor → Cliente

Todas saem por `LiveSession.safe_send` (ws.py:89-100), que é a única porta de
saída — inclusive para o gravador de turnos.

| Mensagem | Formato | Significado | Emissores |
|---|---|---|---|
| `transcricao` | `{"tipo","texto":str}` | O que o servidor entendeu que o usuário disse. **Também é emitido para texto digitado** (eco). | ws.py:419 (voz), ws.py:496 (digitado) |
| `token` | `{"tipo","texto":str}` | Pedaço da resposta, para concatenar. Pode ser palavra ou frase (§1.5). | respostas.py:242, 247, 262, 293, 304, 313, 345, 472, 510, 596, 606, 611, 649, 704, 707, 714; agent.py:799, 807 |
| `audio` | `{"tipo","base64":str}` | **Um arquivo WAV completo, por frase** (§1.6) | agent.py:119, 802; respostas.py:356, 475, 513; scheduler.py:619 |
| `status` | `{"tipo","texto":str}` | Aviso efêmero (toast). Ex.: "Modelo religando...", "Ouvindo…" | ws.py:187, 291 |
| `erro` | `{"tipo","texto":str}` | Exceção no pipeline. Vem seguido de `token`+`audio` com uma frase falada de desculpa. | agent.py:608-611 |
| `fontes` | `{"tipo","rota":str,"itens":[str]}` | Proveniência da resposta que acabou de sair | agent.py:584-586 |
| `proativo` | `{"tipo","texto":str,"ack_id":str?}` | Push iniciado pelo servidor (lembrete/watcher/briefing) | scheduler.py:612-614 |
| `navegar` | `{"tipo","acao":str,"id":str?}` | O servidor manda operar a UI | ws.py:287, 308; comandos_mestre.py:240, 244 |
| `barge_in` | `{"tipo"}` | **Descarte o áudio já enfileirado** | ws.py:332, 412 |

Detalhes que não são óbvios e que o app precisa acertar:

- **`fontes.itens` é uma lista de strings com prefixo**, não de objetos:
  `"memoria"` (agent.py:392), `"nota:<arquivo>.md"` (agent.py:515) e
  `"web:<dominio>"` (respostas.py:505-507). `rota` é a cascata usada, com `"+"`
  entre as fontes (agent.py:572). Não é emitido em sessão confidencial
  (agent.py:583).
- **`navegar.acao`** aceita: `nova_conversa`, `abrir_historico`,
  `fechar_historico`, `carregar_conversa` (traz `id`, comandos_mestre.py:240),
  `ativar_live` e `dormir_live` (ws.py:287, 308 — wake-word). O app pode ignorar
  as de histórico numa Fase 1 e ainda funcionar; ignorar `carregar_conversa`
  quebra a navegação por voz.
- **`barge_in` tem o MESMO nome nos dois sentidos e semântica diferente.** Do
  servidor para o cliente significa "esvazie sua fila de áudio"; do cliente para
  o servidor significa "cancele o pipeline". O comentário em ws.py:328-332 e
  index.html:884-885 é explícito: **o cliente NÃO pode reenviar `barge_in` ao
  recebê-lo**, senão fecha um laço cliente↔servidor. Este é um erro fácil de
  cometer em Kotlin, onde é tentador ter um único `onBargeIn()`.
- **`ack_proativo` não é opcional.** Sem o ack, o scheduler não conclui nem
  reprograma o agendamento (ws.py:441-446 e o comentário do scheduler em
  config.py:1202): "send em TCP meio-aberto não prova nada". Um app que exibe o
  lembrete e não confirma faz o servidor reentregá-lo.
- **`erro` não substitui a fala**: o servidor manda `erro` **e** uma frase falada
  logo atrás (agent.py:608-611). O app deve tratar `erro` como reset de estado da
  UI, não como fim do turno.

### 1.5 Como o texto chega: por palavra ou por frase

`texto_stream_por_palavra` é `True` por default (config.py:112), mas **só vale
para turno NÃO falado**: `por_palavra = settings.texto_stream_por_palavra and not
turno_falado.get()` (respostas.py:236 e 694).

Ou seja: turno digitado streama palavra a palavra; turno de voz streama frase a
frase, sincronizado com o áudio. O app não precisa saber qual é qual — basta
concatenar `token.texto` na ordem. Mas o comportamento visual difere, e isso é
intencional (config.py:105-112).

### 1.6 Como o áudio chega, e o que o app precisa tocar

`{"tipo":"audio","base64":...}` carrega **um arquivo WAV RIFF completo, mono,
16 bits**, gerado por frase:

- Piper: `wave.open` com `setnchannels(1)`, `setsampwidth(2)` e
  `setframerate(self._voice.config.sample_rate)` — audio.py:433-436.
- XTTS: idem, com `sample_rate` de 24000 por default (tts_xtts.py:136, 255-256,
  412-415).

**A taxa do áudio de saída NÃO é a mesma da entrada e NÃO é fixa.** A voz Piper
configurada é `pt_BR-cadu-medium.onnx` (config.py:53) e a taxa vem do arquivo do
modelo em tempo de execução — **NÃO VERIFIQUEI** o valor numérico dessa voz (não
abri o `.onnx`/`.json` do modelo). O ponto de engenharia é o mesmo de qualquer
forma: **o app deve ler a taxa do cabeçalho do WAV recebido, nunca assumir um
número.** Trocar `MENTE_TTS_ENGINE` de `piper` para `xtts` muda a taxa em tempo
de execução, sem aviso, e um player com taxa fixa passaria a falar com voz de
esquilo ou de trator.

O TTS emite **uma mensagem `audio` por frase** (agent.py:112-119, dentro do laço
de frases), conforme o `SentenceChunker` fecha sentenças. Isso é o mecanismo que
segura o TTFA e o app não deve tentar "juntar tudo antes de tocar" — ver Risco R3.

### 1.7 Turno digitado é MUDO por default

`turno_falado.set(stt_ms is not None or settings.falar_turno_digitado)`
(agent.py:188), com `falar_turno_digitado: bool = False` (config.py:82). O portão
único da fala está em agent.py:103: `if not turno_falado.get(): return`.

**Consequência para a Fase 1: um app só de texto não recebe NENHUMA mensagem
`audio`.** Isso é ótimo (a Fase 1 não precisa de player nenhum), mas é preciso
saber que é assim de propósito, não uma falha.

Nota de discrepância encontrada de passagem: o comentário em index.html:848-852
afirma que "fora do live o servidor ainda sintetiza e envia" — isso **não é mais
verdade** desde o portão de `turno_falado`. Comentário do front desatualizado,
não defeito funcional. Não corrigi (a tarefa proíbe editar código).

---

## 2. Autenticação

Regra pura em `mente_digital/acesso.py:28-34`:

```python
if token_esperado:
    return bool(token_recebido) and secrets.compare_digest(token_recebido, token_esperado)
return (host or "") in _LOOPBACK
```

- **Com `MENTE_ACCESS_TOKEN` configurado** (config.py:1275, `.env.example:47`):
  exige o token exato, venha de onde vier. Comparação em tempo constante.
- **Sem token**: só loopback (`127.0.0.1`, `::1`, `localhost`, `testclient` —
  acesso.py:25). **A LAN não alcança nada.**

Como o token entra, por superfície:

| Superfície | Aceita | Linha |
|---|---|---|
| Rotas `/api/*` | header `X-Mente-Token` **OU** query `?token=` | main.py:243 |
| WebSocket | **só query** `?token=` | main.py:541 |
| `/api/health` | **sem gate nenhum** | main.py:254-291 (não tem `Depends(exigir_acesso)`) |

### 2.1 O que o app Android deve fazer

1. **Guardar o token em `EncryptedSharedPreferences`** (androidx.security-crypto),
   não em `SharedPreferences` cru. É um segredo de longa duração que dá acesso
   de escrita ao vault (`POST /api/nota/texto`, main.py:512) — o docstring de
   acesso.py:4-6 é explícito: sem gate, "qualquer aparelho da LAN lê e ENVENENA a
   base".
2. **Nas rotas `/api`, usar SEMPRE o header `X-Mente-Token`**, nunca a query.
   O app nativo não tem a limitação do `<img>` que forçou a query no front
   (main.py:396, index.html:809-811).
3. **No WebSocket, não há escolha: é query string** (main.py:541). Isso é o
   problema tratado abaixo.
4. **Usar `/api/health` como teste de conexão na tela de configuração** — ela
   responde sem token (main.py:256-266, cujo docstring diz explicitamente que
   existe para "a tela de boot do app nativo apontada para um servidor remoto").
   É como o cliente magro atual já faz (`_prontos_remotos`, app.py:457-472).
   Assim o app distingue "servidor inalcançável" de "token errado", em vez de
   mostrar um 1008 genérico.

### 2.2 O problema de segurança do token na query, e o que fazer

Mandar segredo em query string é ruim por razões que **não desaparecem numa rede
doméstica**:

- **Fica no histórico do servidor web e em qualquer proxy no caminho.** Aqui o
  uvicorn sobe com `log_level="error"` (main.py:571), o que é justamente a
  mitigação citada no comentário de main.py:539-540. Mas essa mitigação é frágil:
  quem subir com `--log-level info` para depurar passa a gravar o token em texto
  claro no console e no arquivo de log.
- **Em HTTP simples, a URL vai em texto claro no fio.** Qualquer aparelho na
  mesma Wi-Fi (incluindo a TV, a lâmpada e o aparelho de visita) que consiga
  capturar tráfego lê o token uma vez e o tem para sempre — o token não expira e
  não é rotacionado por nada no código.
- **A rede doméstica não é uma fronteira de confiança.** É a rede com mais
  dispositivos sem manutenção da casa.

**O que fazer, em ordem de custo:**

1. **TLS (§3) resolve o vazamento no fio**, que é o vetor real numa LAN. Com WSS,
   a query string está dentro do túnel; sobra só o risco de log no servidor.
2. **Manter `log_level="error"`** e documentar que depurar com log verboso
   vaza o token.
3. **Token longo e exclusivo.** O `.env.example:44-47` já orienta: "gere um token
   longo […] NUNCA reuse senhas aqui". O app deve aceitar colar/escanear um token
   longo sem digitação manual (ver §5, QR code).
4. **O que eu NÃO recomendo para este plano:** mudar o servidor para aceitar
   header no handshake do WS. É tecnicamente possível num cliente nativo (OkHttp
   manda header no handshake sem problema, ao contrário do `WebSocket` do
   navegador) e seria mais limpo — mas a tarefa é construir o cliente sem
   inventar protocolo, e um caminho de auth só-para-Android é exatamente a
   divergência que se quer evitar. **Fica registrado como melhoria futura do
   servidor**, com o ganho real (tirar o segredo da URL para todos os clientes
   não-browser, incluindo o `app.py --remoto`) e o custo (uma linha no gate de
   main.py:541 lendo `websocket.headers.get("x-mente-token")` antes da query, mais
   testes em `tests/test_acesso.py`). Decisão do dono, não deste plano.

---

## 3. HTTPS e a política de cleartext do Android

### 3.1 O que existe hoje no servidor

TLS é **opcional e desligado por default** (config.py:1269-1270:
`ssl_cert: str = ""`, `ssl_key: str = ""`). Quando os dois caminhos existem, o
uvicorn sobe em HTTPS/WSS (main.py:558-568); se apontarem para arquivo
inexistente, o servidor **loga erro e sobe em HTTP mesmo assim** (main.py:563-568)
— o app não pode assumir que "configurei TLS" significa "está em TLS", tem de
tentar e tratar.

Já existe gerador de certificado: `scripts/gerar_cert.py`. Ele prefere **mkcert**
(gerar_cert.py:55-68), instala uma CA local e inclui **os IPs da LAN no SAN**
(gerar_cert.py:42-52, 75-83). O fallback é openssl auto-assinado, com o aviso de
que WSS com cert cru "falha de forma errática" em iOS/Android (gerar_cert.py:11-15).

### 3.2 O que muda no Android nativo

A restrição que motivou o TLS no projeto — `getUserMedia` exigir *secure context*
fora de localhost (main.py:556-557, index.html:943-946) — **não existe no app
nativo**. O `AudioRecord` só depende da permissão `RECORD_AUDIO`. Ou seja: **a voz
funciona no app Android sobre HTTP puro**, o que já é uma vantagem concreta sobre
a web no celular.

Mas entra outra restrição, do lado do Android: desde o **Android 9 (API 28)**,
`android:usesCleartextTraffic` é **`false` por default**. Tráfego `http://` e
`ws://` é bloqueado pela plataforma, com uma exceção de rede que o OkHttp
reporta como falha de conexão — sem explicação útil na tela.

Como o servidor sobe em HTTP por default (config.py:1269-1270), **o app não
conecta em nada out-of-the-box** a menos que isto seja tratado explicitamente.

### 3.3 As duas saídas

**Saída A — Network Security Config permitindo cleartext só na LAN (recomendada
para as Fases 1–3).**

Um `res/xml/network_security_config.xml` declarando cleartext apenas para os
domínios/IPs do servidor doméstico, e `android:networkSecurityConfig` no
manifesto. Custo: ~1 hora. Limitação séria e que precisa ser dita: **o Network
Security Config é estático — os domínios são compilados no APK.** Não dá para
"adicionar o IP que o usuário digitou" em tempo de execução. As opções reais são:

- declarar a faixa privada como domínio (`192.168.0.1` … um por linha; não há
  wildcard de CIDR), o que só funciona se o IP do PC for estável; ou
- declarar `cleartextTrafficPermitted="true"` no `base-config` — o que permite
  HTTP para **qualquer** destino, e portanto joga fora a proteção. Se este for o
  caminho escolhido, que seja uma decisão consciente e escrita, não o default
  silencioso.
- **NÃO VERIFIQUEI** o comportamento exato de `<domain>` com IP literal em cada
  versão do Android; a documentação aceita IPs, mas isso precisa de teste no
  aparelho do dono antes de virar a estratégia final.

**Saída B — TLS com a CA própria que o projeto já gera (recomendada como estado
final).**

`scripts/gerar_cert.py` com mkcert já produz um cert válido para os IPs da LAN.
No Android, duas variantes:

- **B1 — instalar a CA do mkcert no aparelho** (Configurações → Segurança →
  Credenciais). Funciona, mas desde o Android 7 (API 24) **CAs instaladas pelo
  usuário não são confiadas por apps por default** — o app precisa de um
  `network_security_config.xml` com
  `<trust-anchors><certificates src="user"/></trust-anchors>`. Custo: ~2 horas +
  um passo manual de instalação por aparelho.
- **B2 — empacotar a CA do mkcert como recurso do app**
  (`<certificates src="@raw/mente_ca"/>`). Sem passo manual no aparelho, mas
  amarra o APK àquela CA: se o dono rodar `mkcert -install` de novo em outra
  máquina, o APK precisa ser reconstruído. Custo: ~2 horas.

**Recomendação:** Saída A para destravar as Fases 1–2 rapidamente (com o
`base-config` restrito ao IP do servidor, e o teste no aparelho antes de fechar),
e Saída B2 na Fase 5 como estado final — ela é a única que dá confidencialidade
real ao token na query string (§2.2).

O que **não** fazer: um `TrustManager` que aceita tudo. É o atalho clássico, mata
a autenticação do servidor por completo, e o Google Play recusa o APK.

---

## 4. VAD e barge-in — o que replicar, o que delegar

Hoje há detecção nos **dois lados**, com papéis diferentes.

### 4.1 O que o servidor faz (e o app pode delegar inteiro)

| Função | Onde | Parâmetros (defaults) |
|---|---|---|
| Início de fala (RMS) | ws.py:338 | `vad_rms_threshold = 0.005` (config.py:995) |
| Fim de fala (endpointing adaptativo) | ws.py:381-391, `janela_endpoint` ws.py:35-46 | `vad_silence_seconds = 1.2`; fala ≤ `vad_fala_curta_seconds = 3.0` s encerra em `vad_silence_curta_seconds = 0.7` s (config.py:1031-1033) |
| Descarte de ruído curto | ws.py:393-394 | `vad_min_frames = 15` (config.py:997) |
| Barge-in do servidor | ws.py:318-337 | `barge_rms_threshold = 0.02`, `barge_min_frames = 8` (config.py:1006-1008) |
| Meia-duplex anti-eco + parada por palavra | ws.py:401-415 | `eco_guarda_seconds = 2.5` (config.py:1022), `parada_habilitada = True` (config.py:1016) |
| Wake-word "mestre" | ws.py:272-308 | `mestre_wake` (off por default, ws.py:66) |

**Tudo isso o app delega.** Não há um único parâmetro de VAD que precise ser
reimplementado em Kotlin para o fluxo funcionar. É a decisão certa: esses
números foram calibrados contra fala real (ver os comentários de calibração em
config.py:998-1005 e o detector de corte-precoce em ws.py:354-367) e duplicá-los
no cliente criaria duas verdades que envelheceriam separadas.

### 4.2 O que o app PRECISA replicar

Três coisas, e só três:

**(a) O tamanho do quadro de 1024 amostras.** Já explicado em §1.2: sem isso,
`vad_min_frames` e `barge_min_frames` mudam de significado silenciosamente.

**(b) O barge-in do cliente, com os mesmos limiares.** No navegador
(index.html:975): `BARGE_RMS = 0.12`, `BARGE_FRAMES = 4`. Note que são
**diferentes** dos do servidor (0.02 / 8) de propósito — o comentário em
index.html:969-975 explica: o limiar do cliente é maior porque o microfone do
cliente capta a própria voz do dono diretamente, e o do servidor precisa tolerar
eco atenuado.

Ao receber `{"tipo":"barge_in"}` do servidor, o app faz o mesmo que
index.html:886-887: **esvazia a fila de áudio, para o player, volta ao estado de
escuta — e não responde nada**.

**(c) A regra de "não enviar PCM enquanto a IA fala".** Esta é a mais sutil e a
mais fácil de errar. O navegador, em index.html:988-1003, tem um `if/else`:
enquanto `iaFalando` é verdadeiro ele **para de enviar áudio** e só roda o
detector local de barge-in; caso contrário, envia.

Isso significa que o servidor, na prática, quase nunca exerce o próprio barge-in
com um cliente-navegador — ele fica valendo para a janela entre "pipeline
começou" e "primeiro áudio tocou", em que `iaFalando` ainda é falso. Por
tabela, o **comando de parada falado** ("pare", "chega" — `mestre.e_comando_parada`,
mestre.py:93-104, acionado em ws.py:407-412) também só funciona nessa janela.

O app Android tem duas posturas possíveis, e a escolha é de produto:

- **Copiar o navegador** (parar de enviar durante o playback): comportamento
  idêntico ao que o dono já conhece, sem risco de auto-corte por eco, e economiza
  banda. **Recomendado para a Fase 2.**
- **Continuar enviando durante o playback**: destrava o barge-in do servidor e o
  comando de parada falado durante a fala inteira — mas exige cancelamento de eco
  de verdade, senão a própria voz do TTS corta a resposta. O Android tem
  `AcousticEchoCanceler` (`MediaRecorder.AudioSource.VOICE_COMMUNICATION`), o que
  torna isso mais viável que no navegador. **Fica para a Fase 5, como experimento
  medido**, não como default.

---

## 5. Descoberta do servidor

**Não existe nada disso hoje.** Busca por `zeroconf|mdns|NSD|_http._tcp|qrcode`
em `mente_digital/*.py`, `main.py`, `app.py` e `scripts/*.py` não retorna nada.
O cliente magro atual exige a URL na linha de comando (`--remoto`, app.py:689).

Três opções:

| Opção | Custo no app | Custo no servidor | Falha quando |
|---|---|---|---|
| **IP digitado** | ~2 h (uma tela de config + validação via `/api/health`) | zero | O DHCP muda o IP do PC. Mitigação: reserva de DHCP no roteador (config do roteador, não do projeto). |
| **mDNS via `NsdManager`** | ~1,5 dia (o `NsdManager` é notoriamente chato: resolve assíncrono, callbacks que vazam, bugs por fabricante) | anúncio de `_mente._tcp` no lifespan: dependência nova (`zeroconf`) + código novo | Wi-Fi com isolamento de cliente ligado (comum em roteador de operadora); redes 5 GHz/2,4 GHz segmentadas; economia de bateria desligando multicast. |
| **QR code** | ~4 h (uma lib de scanner; ML Kit ou ZXing) | ~2 h (uma rota que renderiza o QR com URL+token) | Nunca, além de precisar do PC à mão uma vez. |

**Recomendação: IP digitado na Fase 1, QR code na Fase 3.**

O raciocínio: o QR resolve **dois** problemas de uma vez — a descoberta e a
entrada do token longo, que é o pior momento de UX do app inteiro (digitar 40+
caracteres aleatórios no teclado do celular, sem errar). O mDNS resolve só a
descoberta, custa 4× mais, e falha exatamente nas redes domésticas mais chatas,
que são as que o dono não controla. **O mDNS é a opção que parece mais elegante e
é a que menos entrega aqui.**

O QR deve conter a URL completa com o token — `https://192.168.0.10:8000/?token=...`
— que é exatamente o formato que o front já usa para provisionar
(index.html:806-808: abre a página uma vez com `?token=`, guarda no
`localStorage`). O app faz o mesmo: escaneia uma vez, guarda no
`EncryptedSharedPreferences`, nunca mais pergunta.

---

# Parte II — O plano

## 6. Fases

O critério de cada fase: **entrega algo que roda e que o dono pode usar**. Não há
"fase de setup".

### Fase 1 — Chat por texto, ponta a ponta (o app já é útil aqui)

**Entrega:** um APK que conecta ao servidor de casa, mostra o histórico de
conversas, abre uma conversa, digita, e vê a resposta streamando palavra a
palavra com as fontes.

- Tela de configuração: URL + token, com botão "testar" que bate em
  `/api/health` (main.py:254) e mostra o mapa de serviços prontos — igual ao que
  o `app.py` já faz em `_prontos_remotos` (app.py:457-472).
- `network_security_config.xml` para destravar o cleartext (§3.3, Saída A).
- WebSocket com OkHttp: connect com `?token=`, `set_conversa` no `onOpen`
  (espelhando index.html:825).
- Envio: `{"tipo":"texto","payload":...}`.
- Recepção e tratamento de: `transcricao` (desenha a bolha do usuário — **o
  servidor é a fonte da verdade**, ver o comentário em index.html:779-784 sobre a
  regressão de mensagem duplicada), `token`, `status`, `erro`, `fontes`.
- Reconexão com backoff (índice: index.html:906-910 usa 1000 ms × 1,6, teto de
  15 s) e reenvio de `set_conversa`.
- Lista de conversas via `GET /api/conversas` (main.py:475) e reabertura via
  `carregar_conversa` no WS.
- Markdown básico na bolha; figuras (`![[...]]` → `GET /api/imagem/<path>?token=`,
  main.py:390 e index.html:663-672) podem ficar para a Fase 3.

**Não entrega:** áudio (nem precisa — §1.7: turno digitado é mudo por default).

**Esforço: 5–7 dias.** A maior parte é UI de chat (streaming numa `LazyColumn` sem
recompor a lista inteira a cada token é o ponto que costuma custar mais do que
parece).

---

### Fase 2 — Voz: falar e ouvir

**Entrega:** o modo live do celular. Segura para falar (ou modo mãos-livres),
o servidor transcreve, responde, e a resposta sai pelo alto-falante frase a frase.

- Permissão `RECORD_AUDIO` com o fluxo de runtime permission.
- `AudioRecord` em 16 kHz / mono / `ENCODING_PCM_16BIT`, fatiado em blocos de
  **1024 amostras** (§1.2, §4.2a), enviado como quadro binário do WS.
- Player: fila de WAVs, um por mensagem `audio`, tocados em sequência sem gap.
  **Lendo a taxa do cabeçalho do WAV** (§1.6).
- Barge-in do cliente com `BARGE_RMS = 0.12` / `BARGE_FRAMES = 4`
  (index.html:975), enviando `{"tipo":"barge_in"}`.
- Tratamento de `barge_in` recebido: esvazia a fila, **sem responder** (§4.2b).
- Parar de enviar PCM durante o playback (§4.2c, postura "copiar o navegador").
- `end_session` ao sair do modo voz (index.html:945).
- Serviço em primeiro plano (foreground service) enquanto a voz está ativa — ver
  Risco R1.

**Esforço: 6–9 dias.** A fila de áudio sem gap e o barge-in são onde o tempo
vai; o `AudioRecord` em si é meio dia.

---

### Fase 3 — Provisionamento por QR + push proativo

**Entrega:** instalar o app e apontá-lo para o servidor com uma foto; e receber os
lembretes que o servidor dispara.

- Scanner de QR (ML Kit ou ZXing) lendo `URL + token` (§5). **Depende de uma rota
  nova no servidor que renderize o QR** — é a única mudança de servidor que este
  plano propõe, e ela é aditiva (uma rota `GET`, atrás do gate de acesso, servida
  na máquina do dono).
- `proativo`: bolha própria com 🔔 (index.html:856-867) + **`ack_proativo`
  obrigatório** (§1.4). Sem o ack o lembrete é reentregue.
- Notificação do Android para o `proativo` quando o app não está em primeiro
  plano.
- Figuras do vault na bolha (`![[...]]` → `/api/imagem/`).

**Esforço: 4–6 dias** (dos quais ~0,5 dia é a rota de QR no servidor).

---

### Fase 4 — Sobrevivência: reconexão, Doze e entrega garantida

**Entrega:** o app que não some sozinho. É a fase que separa demo de ferramenta.

- Foreground service com notificação persistente enquanto houver sessão de voz.
- Reconexão que reassocia a conversa (`set_conversa`) e **não** perde o turno em
  voo.
- Backoff que respeita mudança de rede (`ConnectivityManager.NetworkCallback`):
  reconectar na hora quando o Wi-Fi volta, em vez de esperar o backoff.
- Fila de `ack_proativo` persistida: se o app morreu entre exibir e confirmar, o
  servidor reentrega (ws.py:180-181 chama `entregar_pendentes` no accept) — o app
  precisa não duplicar a bolha.
- Comportamento explícito ao sair do app: mandar `end_session`? A resposta correta
  é **sim** (dispara a consolidação do conhecimento, ws.py:222-246) — mas com a
  ressalva de que o disconnect já é rede de segurança (ws.py:218-220), então o
  pior caso não perde nada.

**Esforço: 5–8 dias.** É a fase com mais variação por fabricante (Xiaomi, Samsung
e Huawei têm políticas de background próprias e mais agressivas que o Doze
padrão). O teto alto é honesto.

---

### Fase 5 — Refinamentos medidos (opcional, só com número na mão)

Nada aqui entra sem antes/depois medido, seguindo a régua da casa
(docs/CONSULTORIA_TTFT.md:24-25: "nada é aceito sem um antes/depois medível").

- TLS com CA empacotada (§3.3, Saída B2).
- Áudio full-duplex com `AcousticEchoCanceler` (§4.2c) — destrava o comando de
  parada falado durante toda a resposta.
- VAD no cliente para não subir PCM em silêncio (economia de bateria/banda),
  **com o cuidado** de que isso muda o que `vad_min_frames` observa.
- Painel de latência lendo o bloco `waterfall` de `/api/metrics`
  (telemetry.py:844-882, exposto em main.py:489) para o dono ver o custo real da
  rede.

**Esforço: 4–10 dias**, conforme o que for escolhido.

**Total das Fases 1–4: 20–30 dias de trabalho.**

---

## 7. Stack recomendada

| Camada | Escolha | Por quê |
|---|---|---|
| UI | **Jetpack Compose** + Material 3 | Chat com streaming é estado que muda muitas vezes por segundo; o modelo declarativo evita o `notifyItemChanged` manual do RecyclerView. Compose é o caminho suportado. |
| WebSocket | **OkHttp** (`okhttp3.WebSocket`) | Já é a base do Retrofit (que o app usa para `/api`), então é uma dependência, não duas. Trata binário (`ByteString`) e texto no mesmo listener, tem ping/pong configurável (`pingInterval` — relevante para o Risco R1) e o `Interceptor` para o header `X-Mente-Token`. Ktor Client é alternativa legítima; não recomendo Scarlet (abandonado). |
| HTTP `/api` | **Retrofit + kotlinx.serialization** | `/api/conversas`, `/api/conversa/{id}`, `/api/health`, `/api/metrics` são REST simples. |
| Captura de áudio | **`AudioRecord`** — `SAMPLE_RATE=16000`, `CHANNEL_IN_MONO`, `ENCODING_PCM_16BIT` | **Confirmado que bate com o servidor** (§1.2): PCM16 LE mono é exatamente o que `np.frombuffer(raw, dtype=np.int16)` lê em ws.py:312, e 16 kHz é o que o Whisper espera sem reamostragem. `MediaRecorder` **não serve** — ele entrega arquivo comprimido (AAC/AMR), não PCM cru. |
| Fatiamento | Buffer próprio de **1024 amostras / 2048 bytes** | §1.2 — o `getMinBufferSize()` varia por aparelho e mudaria o significado de `vad_min_frames`. |
| Reprodução | **`AudioTrack` em modo `STREAM`**, alimentado com o PCM extraído do WAV | Menor latência do que `MediaPlayer`. `MediaPlayer` precisa de `prepare()` por arquivo e insere um gap audível entre as frases — e o servidor manda **uma frase por vez** (§1.6), então o gap apareceria a cada frase. `AudioTrack` permite escrever os samples da frase seguinte antes de a anterior acabar, dando a fala contínua. Custo: parsear o cabeçalho RIFF na mão (~40 linhas) para descobrir a taxa e pular até o chunk `data`. **ExoPlayer/Media3 é a alternativa** se o parse manual incomodar (aceita `ByteArrayDataSource` e uma playlist concatenada), mas traz uma dependência grande para um problema pequeno. |
| Segredo | **`EncryptedSharedPreferences`** | §2.1. |
| Concorrência | **Coroutines + Flow**; o WS vira um `Flow<MensagemServidor>` | Um `sealed interface MensagemServidor` com uma subclasse por `tipo` (§1.4) dá exaustividade no `when` — o compilador passa a cobrar o tratamento quando um tipo novo for adicionado ao servidor. |

Sobre versões: **NÃO VERIFIQUEI** versões específicas de nenhuma dessas
bibliotecas (o ambiente aqui é Python; não há projeto Gradle no repo). Fixar
versões é trabalho da Fase 1.

---

## 8. O que NÃO fazer

**Não reimplementar nada do pipeline.** Nem o VAD, nem o gate de relevância, nem
a cascata RAM→Banco→Web, nem o chunker de frases. Os números do VAD foram
calibrados contra fala real e têm o histórico do porquê escrito no código
(config.py:998-1033). Uma segunda cópia em Kotlin envelheceria em separado e
produziria dois comportamentos com o mesmo nome.

**Não guardar o vault no celular.** 26,94 GB, e o RAG depende do ChromaDB + do
embedding e5 na GPU. O que o app pode cachear é o **histórico de conversas** já
lido (`/api/conversas`), para a tela abrir sem rede. Nada mais.

**Não usar o STT do Android** (`SpeechRecognizer` / Google). Três razões, e a
terceira é a que decide:
1. Ele manda áudio para servidores do Google — contradiz o pilar "100% local" do
   projeto inteiro.
2. O servidor já tem Whisper `large-v3-turbo` (docs/CONSULTORIA_TTFT.md:16), que
   é melhor em PT-BR do que o reconhecedor genérico do aparelho.
3. **O texto transcrito não é o produto.** O turno inteiro depende de estado do
   servidor: `stt_ms` decide se o turno é falado ou digitado (agent.py:188), a
   janela adaptativa depende da duração medida no servidor (ws.py:386), o
   anti-eco depende de o servidor saber quando o TTS saiu (ws.py:93-94). Enviar
   só o texto final pularia tudo isso e daria um turno de segunda classe.

**Não usar o TTS do Android.** O timbre é o do servidor (Piper ou XTTS com voz
clonada — config.py:53, CLAUDE.md sobre `tts_xtts.py`). Sintetizar no aparelho
trocaria a voz do assistente por outra, no meio da mesma conversa, dependendo do
caminho.

**Não reenviar `barge_in` ao recebê-lo** (§1.4). Fecha laço.

**Não assumir taxa de amostragem fixa na reprodução** (§1.6). Trocar
`MENTE_TTS_ENGINE` mudaria a voz de velocidade.

**Não mandar header `Origin`** (§1.1). Se mandar, tem de bater com o `Host`, ou é
1008 sem explicação.

**Não criar rotas novas no servidor** além da rota de QR da Fase 3. Se alguma
coisa parecer precisar de uma rota nova, quase certamente é o app querendo mover
lógica para o lado errado.

**Não implementar STT parcial/streaming.** É não-feature intencional, documentada
no README e reafirmada com veto de produto na consultoria
(docs/CONSULTORIA_TTFT.md:285: "não se reabre decisão de produto por ms").

---

## 9. Riscos concretos e mitigação

### R1 — Doze mode e a morte do WebSocket em background — **ALTO**

O Android suspende sockets de apps em background; sob Doze, os rádios são
desligados em janelas. Um WebSocket ocioso morre sem aviso, e o app descobre
tarde.

Três coisas compõem a mitigação, e nenhuma sozinha resolve:

1. **Foreground service** enquanto houver sessão de voz ativa, com notificação
   persistente. É o único jeito suportado de manter socket vivo de forma
   confiável, e é justificável para o usuário ("Mente Digital está ouvindo").
2. **`pingInterval` do OkHttp** (30 s é um ponto de partida razoável). Isso
   detecta o socket morto — não o impede.
3. **Aceitar que o socket vai cair com o app em background sem voz ativa**, e
   projetar em torno disso: o servidor já reentrega o que ficou pendente no
   próximo accept (`entregar_pendentes`, ws.py:180-181), e um agendamento
   perdido não se perde — vira `pendente_entrega` (CLAUDE.md sobre scheduler.py).
   **Ou seja: o caminho de lembretes já tolera o app desconectado.** Não force
   uma conexão permanente para resolver um problema que o servidor já resolveu.

Para o push com o app fechado, a resposta correta a longo prazo **não é** manter
o WS vivo, e sim uma notificação — mas FCM exige serviço em nuvem, o que
contradiz o pilar local. **NÃO VERIFIQUEI** alternativas locais de push
(ntfy/UnifiedPush self-hosted); é uma investigação para depois da Fase 4, não
uma promessa.

### R2 — Reconexão que corrompe a conversa — **MÉDIO, com precedente**

O comentário em ws.py:74-78 documenta o bug já vivido: o cliente reconecta sozinho
com backoff e reenvia `set_conversa` no `onopen`; quando a memória era única no
`AppContext`, o último a conectar sobrescrevia o `conversa_id` de todos e os
turnos iam para a conversa errada no SQLite. Isso foi corrigido no servidor
(memória por conexão, ws.py:78), mas o app tem de fazer a sua parte:

- **sempre** reenviar `set_conversa` no `onOpen` (index.html:825);
- não presumir que o turno em voo sobreviveu à queda — a resposta parcial se
  perde e a UI deve mostrar isso, não fingir que a mensagem foi entregue;
- backoff com teto (o front usa 1 s × 1,6, teto 15 s — index.html:906-910), mais
  reconexão imediata no callback de rede restaurada.

### R3 — O áudio chega frase a frase — **MÉDIO**

O servidor manda **um WAV completo por frase** (§1.6), conforme o chunker fecha
sentenças. Dois erros possíveis, opostos:

- **Esperar tudo para tocar**: joga fora o mecanismo inteiro de TTFA. A anatomia
  medida do TTFA (docs/CONSULTORIA_TTFT.md:22) tem "~0,3s (1ª frase + Piper)"
  como último item — bufferizar a resposta inteira transformaria isso no tempo de
  gerar a resposta toda. O trabalho da consultoria #8 (1º chunk agressivo,
  `tts_chunk_primeiro_max_chars = 60`) existe justamente para essa frase sair
  antes.
- **Tocar cada frase com um `MediaPlayer` novo**: gap audível entre frases, fala
  robótica e picotada.

Mitigação: `AudioTrack` em modo stream, com a frase N+1 escrita antes de a N
terminar (§7).

### R4 — A latência da rede somada ao TTFA — **MÉDIO, e não medido**

O que está medido no projeto (docs/CONSULTORIA_TTFT.md:18-23), **tudo com o
cliente na mesma máquina**:

- decode ~120 tok/s; TTFT 15 ms (prompt curto) / 441 ms (RAG ~2k tokens);
- STT ≈ 0,8× a duração da fala;
- turno de voz com fala de 5 s: `~1,2s VAD + ~4,0s STT + 0–0,9s extrator + ~0,1s
  busca + ~0,4s TTFT RAG + ~0,3s (1ª frase + Piper) ≈ 6–7s`;
- turno digitado com resposta local: sub-segundo.

**Nenhum desses números inclui rede.** Não há medição de LAN no projeto —
**NÃO VERIFIQUEI** o RTT Wi-Fi da casa do dono, e não vou inventar um.

O que dá para afirmar sem medir:

- **O turno de VOZ é o menos sensível.** Sobre 6–7 s, um RTT de Wi-Fi doméstico é
  ruído. O que pesa é o **upload contínuo de 32 KB/s** (16000 × 2 bytes) durante
  a fala — trivial em Wi-Fi, relevante em rede móvel.
- **O turno DIGITADO é o mais sensível**, e é a Fase 1. Uma resposta local
  sub-segundo com rede lenta ou instável deixa de parecer instantânea. É aqui que
  a percepção pode piorar.
- **A ordem de grandeza do payload de áudio de volta merece cuidado**: WAV
  PCM16 sem compressão, em base64 (+33%). Uma frase de 3 s a 22 kHz ≈ 132 KB
  crus ≈ 176 KB em base64. Numa resposta de 6 frases, ~1 MB por turno.
  **NÃO VERIFIQUEI** se isso já é um problema na LAN do dono. Se for, a correção
  certa é do lado do servidor (comprimir para Opus), não do app — e exigiria
  medição antes, conforme a régua da casa.

**Mitigação primária: instrumentar antes de otimizar.** O app deve medir e
mostrar o próprio waterfall (tempo até `transcricao`, até o 1º `token`, até o 1º
`audio`) e compará-lo com o bloco `waterfall` de `/api/metrics`
(telemetry.py:844-882). A diferença entre os dois **é** o custo da rede. Sem essa
subtração, qualquer conversa sobre "o app está lento" é astrologia — que é
exatamente o lema da Leila na consultoria (docs/CONSULTORIA_TTFT.md:39).

### R5 — O gate de acesso fecha em 1008 sem dizer por quê — **BAIXO, mas irritante**

Token errado, Origin divergente e "sem token e não é loopback" produzem o mesmo
1008 (main.py:544, 548). Mitigação: usar `/api/health` (sem gate) para separar
"servidor inalcançável" de "servidor recusou", e escrever isso na tela.

### R6 — Fragmentação de fabricante — **MÉDIO**

Xiaomi/MIUI, Samsung e outros matam serviços em background por políticas próprias,
mais agressivas que o Doze padrão. Mitigação: testar no aparelho real do dono
cedo (na Fase 2, não na 4), e incluir o atalho para "desativar otimização de
bateria para este app" na tela de configuração.

---

## 10. Resumo de esforço

| Fase | Entrega | Dias |
|---|---|---|
| 1 | Chat por texto ponta a ponta | 5–7 |
| 2 | Voz (captura, VAD delegado, playback, barge-in) | 6–9 |
| 3 | QR de provisionamento + push proativo com ack | 4–6 |
| 4 | Sobrevivência: foreground service, reconexão, Doze | 5–8 |
| **1–4** | **App utilizável no dia a dia** | **20–30** |
| 5 | Refinamentos medidos (TLS empacotado, AEC, VAD cliente) | 4–10 |

Estes números pressupõem alguém com experiência prévia em Android/Kotlin. Sem
isso, some 50–100% na Fase 1 (a curva do Compose) e na Fase 2 (a API de áudio do
Android é antiga e mal documentada).

---

## Apêndice — checklist de conformidade do cliente

Para revisar o app contra o servidor sem reler este documento inteiro:

- [ ] PCM16 **little-endian**, mono, **16000 Hz** (ws.py:312, index.html:961)
- [ ] Quadros de **1024 amostras / 2048 bytes** (index.html:964; §1.2)
- [ ] Token na **query** do WS, no **header** das `/api` (main.py:541, 243)
- [ ] **Sem header `Origin`** (acesso.py:37-44)
- [ ] `set_conversa` em **todo** `onOpen` (index.html:825, ws.py:451)
- [ ] `ack_proativo` para **todo** `proativo` que trouxer `ack_id` (ws.py:441)
- [ ] `barge_in` recebido **não** gera `barge_in` enviado (index.html:884-885)
- [ ] Taxa do player lida do **cabeçalho do WAV** (audio.py:436, tts_xtts.py:415)
- [ ] Bolha do usuário desenhada a partir de `transcricao`, **não** localmente
      (index.html:779-784)
- [ ] `end_session` ao encerrar conversa/modo voz (index.html:597, 630, 945)
- [ ] 1008 no handshake traduzido para linguagem humana, com `/api/health` para
      desambiguar (main.py:254)
