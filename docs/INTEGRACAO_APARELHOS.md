# O que falta ligar — identidade por aparelho

Este worktree traz a camada de identidade **pronta e testada**, mas **deliberadamente
desligada e desconectada** do servidor: `main.py` e `templates/index.html` estavam sendo
editados em paralelo, então nada foi tocado neles. Abaixo está o diff exato que falta.

Estado hoje: `MENTE_APARELHOS_HABILITADO=false` → o gate é, byte a byte, o de hoje.
Aplicar os diffs abaixo **sem** ligar a flag também não muda nada — a fiação é inerte
até a flag subir. Isso é de propósito: dá para integrar num commit e ligar noutro.

| Arquivo | Estado |
|---|---|
| `mente_digital/aparelhos.py` | ✅ novo, puro, 34 testes |
| `mente_digital/registro_aparelhos.py` | ✅ novo, 38 testes |
| `mente_digital/config.py` | ✅ 7 campos novos |
| `scripts/aparelhos.py` | ✅ painel em linha de comando, já funciona |
| `main.py` | ⬜ falta (diff abaixo) |
| `templates/index.html` | ⬜ falta (diff abaixo) |
| `android/` | ⬜ falta (diff abaixo, mínimo) |
| `mente_digital/vigia.py` | ⬜ opcional (diff abaixo) |

---

## 1. `main.py`

### 1.1 Import (junto dos outros `# noqa: E402`, ~linha 27)

```diff
 from mente_digital import acesso  # noqa: E402
 from mente_digital.agent import Agent, EtlProcessor  # noqa: E402
+from mente_digital.registro_aparelhos import RegistroAparelhos  # noqa: E402
```

### 1.2 Instância no lifespan (logo após `db.init`, ~linha 197)

`db.init()` tem de vir **antes**: a tabela `auditoria` é da telemetria, e sem ela toda a
trilha falha. Não é hipótese — foi medido rodando o CLI num banco novo
(`no such table: auditoria` em cada convite).

```diff
     settings.ensure_dirs()
     await asyncio.to_thread(db.init)
+    # Identidade por aparelho. `init` é idempotente; nasce inerte enquanto
+    # MENTE_APARELHOS_HABILITADO for false (o gate delega a acesso.cliente_autorizado).
+    registro = RegistroAparelhos(settings.db_telemetria)
+    await asyncio.to_thread(registro.init)
+    registro.configurar_bloqueio(settings.aparelhos_bloqueio_base_segundos,
+                                 settings.aparelhos_bloqueio_teto_segundos)
+    app.state.registro = registro
```

> Vai em `app.state`, não no `AppContext`: o `AppContext` é container de serviços de
> **conversa** (LLM, STT, vault) e o gate roda **antes** de existir conversa — inclusive
> em requisição que o `AppContext` recusaria.

### 1.3 `exigir_acesso` (substitui o corpo, ~linha 250)

```diff
 async def exigir_acesso(request: Request) -> None:
     """Gate das rotas /api (painel #7): token via header/query OU loopback.
     A regra em si é pura e vive em acesso.py; aqui só se extrai host/token."""
     token = request.headers.get("x-mente-token") or request.query_params.get("token")
     host = request.client.host if request.client else None
-    if not acesso.cliente_autorizado(host, token, settings.access_token):
-        raise HTTPException(status_code=401, detail="não autorizado")
+    veredito = request.app.state.registro.autorizar(
+        token, host, request.url.path,
+        habilitado=settings.aparelhos_habilitado,
+        token_legado=settings.access_token,
+        aceita_token_legado=settings.aparelhos_token_legado,
+    )
+    if not veredito.autorizado:
+        # O `motivo_publico` é grosso de propósito (id inexistente e segredo errado
+        # respondem igual, para não virar oráculo de enumeração). O 401 com corpo é o
+        # que deixa o app distinguir RECUSA de falha de rede — Servidor.kt já trata
+        # o 401 como PedidoVigia.RECUSADO, então o contrato do cliente não muda.
+        raise HTTPException(status_code=401,
+                            detail={"erro": "não autorizado", "motivo": veredito.motivo_publico})
+    request.state.aparelho_id = veredito.aparelho_id
```

⚠ `acesso` continua importado — o `websocket_endpoint` usa `origin_confere`.

### 1.4 `websocket_endpoint` (~linha 772)

O ponto do pedido: **revogar derruba a sessão aberta**. Sem o
`registrar_sessao`/`encerrar_sessao`, revogar só valeria na próxima conexão e o celular
perdido seguiria conversando até alguém desligar o app.

```diff
 @app.websocket("/ws/chat_live")
 async def websocket_endpoint(websocket: WebSocket):
     ctx = get_ctx(websocket=websocket)
     token = websocket.query_params.get("token")
     host = websocket.client.host if websocket.client else None
-    if not acesso.cliente_autorizado(host, token, settings.access_token):
-        await websocket.close(code=1008)
-        return
+    registro = websocket.app.state.registro
+    veredito = registro.autorizar(
+        token, host, "/ws/chat_live",
+        habilitado=settings.aparelhos_habilitado,
+        token_legado=settings.access_token,
+        aceita_token_legado=settings.aparelhos_token_legado,
+    )
+    if not veredito.autorizado:
+        await websocket.close(code=1008)
+        return
     if not acesso.origin_confere(websocket.headers.get("origin"), websocket.headers.get("host", "")):
         await websocket.close(code=1008)
         return
-    await LiveSession(ctx, websocket).run()
+    # A revogação chega de OUTRA thread (painel/CLI), então o fechamento tem de ser
+    # agendado no loop desta conexão — chamar `close()` direto de fora não fecha nada.
+    laco = asyncio.get_running_loop()
+
+    def derrubar() -> None:
+        laco.call_soon_threadsafe(
+            lambda: laco.create_task(websocket.close(code=1008))
+        )
+
+    sid = registro.registrar_sessao(veredito.aparelho_id, derrubar)
+    try:
+        await LiveSession(ctx, websocket).run()
+    finally:
+        registro.encerrar_sessao(veredito.aparelho_id, sid)
```

### 1.5 Rotas do painel (novas — sugestão: junto das outras `/api`)

`emitir` e `revogar` são **só do dono**: `Depends(exigir_loopback)`, não
`exigir_acesso`. Se um aparelho remoto pudesse emitir código, ele se auto-inscreveria e
o teto de 4 viraria decoração. `parear` é a única sem gate — é a porta de entrada de
quem ainda não tem credencial —, e o que a protege é o código de uso único + o bloqueio
progressivo por IP.

```python
async def exigir_loopback(request: Request) -> None:
    """Só a máquina do dono. Emitir convite e revogar são atos de DONO, e o gate
    normal não serve: um aparelho já pareado passaria nele e poderia inscrever o
    quinto ou revogar os outros três."""
    host = request.client.host if request.client else None
    if host not in acesso._LOOPBACK:
        raise HTTPException(status_code=403, detail="só na máquina do assistente")


@app.get("/api/aparelhos", dependencies=[Depends(exigir_loopback)])
async def listar_aparelhos(request: Request):
    reg = request.app.state.registro
    return {
        "habilitado": settings.aparelhos_habilitado,
        "teto": settings.aparelhos_teto,
        "aparelhos": [
            {"id": a.id, "apelido": a.apelido, "criado_em": a.criado_em,
             "ultimo_uso": a.ultimo_uso, "ultimo_ip": a.ultimo_ip,
             "expira_em": a.expira_em, "sessoes": reg.sessoes_vivas(a.id)}
            for a in reg.listar()
        ],
    }


@app.post("/api/aparelhos/convite", dependencies=[Depends(exigir_loopback)])
async def convidar_aparelho(request: Request):
    corpo = await request.json()
    codigo = request.app.state.registro.emitir_codigo(
        (corpo.get("apelido") or "aparelho")[:40], settings.aparelhos_teto)
    if codigo is None:
        return JSONResponse(status_code=409,
                            content={"erro": "teto", "teto": settings.aparelhos_teto})
    return {"codigo": codigo, "validade_minutos": settings.aparelhos_codigo_validade_minutos}


@app.delete("/api/aparelhos/{aparelho_id}", dependencies=[Depends(exigir_loopback)])
async def revogar_aparelho(aparelho_id: str, request: Request):
    return {"revogado": request.app.state.registro.revogar(aparelho_id)}


@app.post("/api/aparelhos/parear")
async def parear_aparelho(request: Request):
    """SEM gate — é a porta de quem ainda não tem credencial. O que a defende é o
    código de uso único, de vida curta, sob o mesmo bloqueio progressivo do gate."""
    corpo = await request.json()
    host = request.client.host if request.client else "?"
    r = request.app.state.registro.parear(
        corpo.get("codigo") or "", host, settings.aparelhos_teto,
        settings.aparelhos_codigo_validade_minutos, settings.aparelhos_expira_dias)
    if not r.ok:
        return JSONResponse(status_code=401, content={"erro": r.motivo})
    return {"credencial": r.credencial, "aparelho_id": r.aparelho_id}
```

---

## 2. `templates/index.html`

A SPA já guarda o token em `localStorage['mente_token']` e o manda como
`X-Mente-Token` (header) e `?token=` (WS e `<img>`). **A credencial nova usa o mesmo
transporte** — é só outra string. Então o mínimo é **zero mudança** para os aparelhos já
configurados.

O que falta é a **tela**, e ela tem duas metades:

**(a) Painel do dono** — só aparece no loopback (as rotas respondem 403 fora dele):

```js
// Painel de aparelhos (só na máquina: as rotas são exigir_loopback).
async function carregarAparelhos() {
    const r = await fetch('/api/aparelhos', { headers: apiHeaders() });
    if (!r.ok) return;                      // 403 = não é a máquina do dono; some
    const d = await r.json();
    // desenhar: apelido, último uso, último IP, sessões vivas, botão Revogar
    // + botão "Convidar aparelho" -> POST /api/aparelhos/convite -> mostra o código
}
async function revogarAparelho(id) {
    await fetch('/api/aparelhos/' + id, { method: 'DELETE', headers: apiHeaders() });
    carregarAparelhos();
}
```

**(b) Pareamento no aparelho novo** — campo para o código, troca por credencial:

```js
async function parear(codigo) {
    const r = await fetch('/api/aparelhos/parear', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ codigo })
    });
    if (!r.ok) return (await r.json()).erro;   // codigo_expirado | codigo_usado | bloqueado
    const d = await r.json();
    localStorage.setItem('mente_token', d.credencial);   // mesmo slot de sempre
    location.reload();
}
```

⚠ **Uma mudança de comportamento que vale a pena**: hoje o `TOKEN_ACESSO` é lido **uma
vez** na abertura da página (linha ~890). Com revogação, o WS pode fechar com 1008 no
meio da conversa — o `ws.onclose` deve distinguir 1008 (revogado: mostrar "este aparelho
perdeu o acesso", **não** reconectar) de queda de rede (reconectar como hoje). Sem isso
o app entra em laço de reconexão contra um servidor que já disse não.

---

## 3. `android/`

**Boa notícia, medida no código:** `Servidor.kt` já manda `X-Mente-Token` no header
(linhas 137 e 174) e já devolve `PedidoVigia.RECUSADO` no 401, distinto de falha de rede
(linhas 127-143). Ou seja, **a exigência do dono de distinguir recusa de falha de rede já
está satisfeita no cliente** e a credencial nova viaja no mesmo campo.

Falta só:

1. **`ui/TelaConfig.kt`** — além do campo "token", um campo "código de pareamento" que
   chama `POST /api/aparelhos/parear` e grava a credencial devolvida em `Ajustes.token`.
   Nada em `Ajustes.kt` muda: ele já guarda no Keystore e já avisa quando cai para
   prefs em claro.
2. **`MainActivity.kt` / `ui/TelaWeb.kt`** — tratar o fechamento 1008 do WebSocket como
   "revogado" (tela própria), não como reconectar.

Nenhuma mudança em `Endereco.kt`, `Gravador.kt`, `ServicoVoz.kt`, `PonteAndroid.kt`.

---

## 4. `mente_digital/vigia.py` (opcional, recomendado)

Hoje o vigia usa `acesso.cliente_autorizado` direto — ou seja, **um aparelho revogado
ainda consegue acordar o PC** se souber o token legado. O `registro_aparelhos` importa em
**0,20 s sem nenhum módulo pesado** (medido: `torch`, `fastapi`, `transformers`,
`llama_cpp`, `chromadb` ausentes de `sys.modules`), então cabe no vigia sem ferir a
regra do arquivo.

```diff
         def _autorizado(self) -> bool:
             token = self.headers.get("X-Mente-Token")
             host = self.client_address[0] if self.client_address else None
-            return acesso.cliente_autorizado(host, token, settings.access_token)
+            # Um aparelho REVOGADO não pode mais levantar o PC do dono. Sem isto, a
+            # revogação teria um buraco: fecha a conversa e deixa o botão de ligar.
+            return _registro().autorizar(
+                token, host, "/vigia/acordar",
+                habilitado=settings.aparelhos_habilitado,
+                token_legado=settings.access_token,
+                aceita_token_legado=settings.aparelhos_token_legado,
+            ).autorizado
```

⚠ Se aplicar, **estenda o teste de peso** (`tests/test_vigia*.py` sobe um subprocesso e
inspeciona `sys.modules`) para cobrir o import novo. Ele é a única coisa que impede um
import distraído de arrastar o torch para dentro do vigia.

---

## 5. Ordem sugerida para ligar

1. Aplicar os diffs **com a flag em `false`** e rodar a suíte → nada muda.
2. `python scripts/aparelhos.py convidar "celular do dono"` → parear os 4 aparelhos.
3. `MENTE_APARELHOS_HABILITADO=true` → cada aparelho passa a responder por si; o
   `MENTE_ACCESS_TOKEN` continua valendo (ponte de migração).
4. Confirmado que os 4 entram: `MENTE_APARELHOS_TOKEN_LEGADO=false` → **o segredo único
   morre**. Enquanto esse passo não for dado, o elo mais fraco continua vivo.
5. `python scripts/aparelhos.py trilha` → conferir que a trilha registra os 4.
