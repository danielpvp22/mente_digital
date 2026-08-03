# Usar o assistente de fora de casa

O pedido: **acessar de fora da rede, com segurança, só nos aparelhos que o dono
autorizar, com controle total dele.** O software já faz a parte dele — identidade
por aparelho, revogação com efeito imediato, teto de 4, trilha de auditoria e o
plantão que levanta o PC. O que falta não é código: é **transporte**.

Este documento é o roteiro do que só o dono pode fazer, com as armadilhas na
ordem em que elas aparecem.

---

## 1. Por que não basta abrir a porta no roteador

Duas razões, e a segunda é a que costuma pegar as pessoas de surpresa:

1. **Expor a porta 8000 na internet** põe o vault inteiro atrás de um único
   segredo, aberto ao mundo. O gate por aparelho aguenta, mas a superfície passa
   a ser a internet inteira.
2. **Sem HTTPS o microfone não funciona.** `getUserMedia` exige *secure context*,
   e o navegador só considera seguro `localhost` ou HTTPS. Um endereço como
   `http://100.x.y.z:8000` **não** é contexto seguro, então de fora de casa o
   assistente vira **só texto** — sem voz, sem modo live.

O caminho escolhido resolve os dois de uma vez.

---

## 2. O caminho: Tailscale + `tailscale cert`

O Tailscale cria uma rede privada entre os seus aparelhos (WireGuard, ponta a
ponta). Nada é publicado na internet: o celular alcança o PC como se estivesse na
mesma sala, de qualquer lugar.

> **Medido em 2026-08-03: o Tailscale NÃO está instalado nesta máquina.** Hoje o
> celular só alcança o PC pelo WiFi de casa.

### Os passos

1. Instale o Tailscale no **PC** e no **celular**, com a **mesma conta**.
2. No painel do Tailscale (admin console), ligue **MagicDNS** e **HTTPS
   Certificates** — sem os dois, o passo 3 não funciona.
3. No PC, emita o certificado (rode no diretório onde quer os arquivos):

   ```bash
   tailscale cert maquina.SUA-TAILNET.ts.net
   ```

   Ele escreve dois arquivos (o `.crt` e o `.key` com o nome da máquina) — confira
   os nomes na saída do próprio comando.

4. Aponte o `.env` para eles:

   ```
   MENTE_SSL_CERT=C:\caminho\maquina.SUA-TAILNET.ts.net.crt
   MENTE_SSL_KEY=C:\caminho\maquina.SUA-TAILNET.ts.net.key
   ```

5. Reinicie o assistente e, no app do celular, troque o endereço para
   **`https://maquina.SUA-TAILNET.ts.net:8000`**.

O token/credencial não muda: é o mesmo campo, o mesmo header.

### ⚠ Use o NOME, nunca o IP 100.x

O certificado é emitido para o nome MagicDNS, **não** para o endereço `100.x.y.z`.
Configurar o app com o IP faz a verificação de nome falhar e o app reporta isso
como falha de conexão genérica — parece o PC fora do ar. É o erro mais provável de
todo este roteiro.

### ⚠ O certificado expira em 90 dias

Emitido por Let's Encrypt via `tailscale cert`, com validade de 90 dias e
**renovação manual** — quem chama o comando diretamente é responsável por renovar.
Vencido, o assistente para de atender em HTTPS **sem aviso**: marque no calendário
ou automatize o `tailscale cert` num agendamento (ele é idempotente e o Let's
Encrypt tem limite de emissões, então mensal já é frequente demais — trimestral,
antes do vencimento, é o certo).

### ⚠ O Tailscale precisa subir no logon

Se ele não subir sozinho, o PC fica inalcançável justamente quando está sozinho em
casa — que é exatamente o cenário para o qual o vigia existe. Confira isso no
próprio instalador (ele registra um serviço no Windows).

### ⚠ O Firewall do Windows

A interface do Tailscale é nova para o Firewall. Se o celular não alcançar as
portas **8000** (assistente) e **8765** (vigia), é o primeiro lugar a olhar.

---

## 3. O que o código já faz por você

| peça | estado |
|---|---|
| Servir HTTPS/WSS | pronto: `MENTE_SSL_CERT` / `MENTE_SSL_KEY` em `main.py` e `app.py` |
| Escutar em qualquer interface | pronto: o servidor já sobe em `0.0.0.0` |
| Vigia (porta 8765) em HTTPS | **novo em 2026-08-03** — usa o MESMO par de certificados |
| Identidade por aparelho | pronto (`MENTE_APARELHOS_HABILITADO`, ver [INTEGRACAO_APARELHOS.md](INTEGRACAO_APARELHOS.md)) |
| Recusa distinguível de queda de rede | **novo em 2026-08-03** — o app para de insistir e diz o motivo |

O vigia falar TLS não é detalhe: o app Android **deriva** o endereço do plantão do
endereço do assistente, trocando só a porta — inclusive o esquema
(`Endereco.vigia`). Com o assistente em HTTPS e o plantão em HTTP puro, o app
falaria `https://…:8765` com um socket que não fala TLS, e a tela diria "o PC está
desligado". Seria a ambiguidade que o vigia existe para matar, ressuscitada pelo
conserto de outra coisa. E é a rota `acordar` que carrega o token: deixá-la em
claro seria cifrar tudo menos a chave.

---

## 4. O caminho alternativo (LAN, sem Tailscale) e por que ele é pior no celular

`python scripts/gerar_cert.py` gera um certificado local com **mkcert** (ou
openssl). Funciona bem no navegador do PC. No **app Android**, não:

`android/app/src/main/res/xml/network_security_config.xml` declara só
`<certificates src="system" />`. Desde a API 24 o Android **não confia em CA
instalada pelo usuário** a menos que o app peça explicitamente — então instalar a
CA do mkcert no celular **não basta**: o app continuaria recusando, e a mensagem
na tela seria uma falha de conexão sem explicação.

Ligar `<certificates src="user" />` resolveria, mas passa a confiar em **qualquer**
CA que alguém instale naquele celular — é enfraquecer o app para cobrir um caminho
que o `tailscale cert` já cobre com um certificado publicamente confiável. Por isso
o arquivo **não** foi mexido: a decisão é do dono, não do agente.

---

## 5. O que foi medido e o que não foi

**Medido nesta máquina (2026-08-03):**

- O Tailscale não está instalado.
- O vigia atende em TLS: servidor real, certificado real, handshake real
  (`tests/test_vigia.py::test_o_plantao_atende_em_TLS_com_o_cert_do_servidor`).
- Certificado configurado mas inexistente → aviso e plantão em HTTP (fail-soft,
  igual ao `main.py`): um vigia mudo é pior que um vigia em claro.
- O `app.py` sonda o plantão no mesmo esquema; antes cravava `http://`, o que daria
  um falso "não tem vigia" e faria o app **deixar de se encerrar** — falha na
  direção silenciosa.

**NÃO medido aqui** (vem da documentação do fornecedor, confira ao executar):
o comportamento do `tailscale cert` (nomes dos arquivos, os dois interruptores do
admin console) e a validade de 90 dias com renovação manual.

**Impossível medir sem o dono:** se o certificado do Tailscale é aceito pelo
WebView do celular dele. A expectativa é que sim, sem instalar CA nenhuma, por ser
Let's Encrypt — mas isso só o primeiro acesso responde.
