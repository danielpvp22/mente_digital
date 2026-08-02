package com.mentedigital.app

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit

/** Estado da conexão, para a UI dizer a verdade em vez de fingir "online". */
enum class Conexao { DESLIGADO, CONECTANDO, ABERTO, RECUSADO, CAIU }

/**
 * O WebSocket `/ws/chat_live`.
 *
 * Três coisas que o plano levantou do servidor e que este arquivo tem de acertar:
 *
 * 1. **Sem header `Origin`.** O gate só morde se o cliente MANDAR um
 *    (acesso.py:37-44: `if not origin: return True`); mandando, ele tem de bater
 *    exatamente com o `Host`, ou o servidor fecha em 1008 e a causa é invisível.
 *    O OkHttp não manda `Origin` sozinho — o cuidado aqui é NÃO acrescentar.
 *
 * 2. **`set_conversa` em TODO `onOpen`** (index.html:825). É o que impede a
 *    reconexão de jogar turnos na conversa errada — o bug histórico documentado
 *    em ws.py:74-78.
 *
 * 3. **`barge_in` recebido não gera `barge_in` enviado** (index.html:884-885).
 *    O mesmo nome tem semântica OPOSTA nos dois sentidos, e o instinto em Kotlin
 *    é ter um `onBargeIn()` só — que trava o laço cliente↔servidor.
 */
/**
 * O que o cliente precisa saber para conectar. Um data class simples, e NÃO o
 * `Ajustes`: assim o cliente inteiro roda em teste de JVM contra o servidor de
 * verdade, sem emulador (ver ConformidadeServidorTest). Amarrá-lo ao
 * `SharedPreferences` do Android tornaria o único teste que exercita o protocolo
 * REAL um teste instrumentado — que precisa de aparelho e por isso não roda.
 */
data class Conf(val base: String, val token: String)

class ClienteMente(
    private val conf: Conf,
    /** Lido a cada `onOpen`, não capturado uma vez: o id muda quando o dono
     *  troca de conversa, e a reconexão tem de mandar o ATUAL. */
    private val conversaId: () -> String,
    private val aoReceber: (MensagemServidor) -> Unit,
    private val aoMudarConexao: (Conexao, String) -> Unit,
) {
    private val http = OkHttpClient.Builder()
        // Detecta socket morto — não o impede (Risco R1 do plano). Sob Doze o
        // Android suspende o rádio e a conexão cai sem aviso; o servidor já
        // tolera isso (reentrega o pendente no próximo accept), então aqui basta
        // PERCEBER rápido e reconectar.
        .pingInterval(30, TimeUnit.SECONDS)
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)   // 0 = sem timeout de leitura no WS
        .build()

    private var ws: WebSocket? = null
    private var tentativas = 0
    private var querendoConectar = false
    private val laco = java.util.concurrent.Executors.newSingleThreadScheduledExecutor()

    fun conectar() {
        querendoConectar = true
        abrir()
    }

    fun desconectar(avisarFimDeSessao: Boolean = true) {
        querendoConectar = false
        // `end_session` dispara a consolidação do conhecimento no servidor
        // (ws.py:222-246). O disconnect já é rede de segurança (ws.py:218-220),
        // então o pior caso não perde nada — mas avisar é o certo.
        if (avisarFimDeSessao) enviar(Envio.encerrarSessao())
        ws?.close(1000, "saindo")
        ws = null
        aoMudarConexao(Conexao.DESLIGADO, "")
    }

    fun enviar(quadro: String): Boolean = ws?.send(quadro) ?: false

    /** Quadro BINÁRIO: PCM16 LE mono 16 kHz, 1024 amostras. É o que
     *  `np.frombuffer(raw, dtype=np.int16)` lê em ws.py:312. */
    fun enviarAudio(pcm: ByteArray): Boolean =
        ws?.send(okio.ByteString.of(*pcm)) ?: false

    fun enviarTexto(t: String): Boolean {
        val limpo = t.trim()
        if (limpo.isEmpty()) return false          // vazio é no-op no servidor
        return enviar(Envio.texto(limpo))
    }

    private fun abrir() {
        val alvo = Endereco.webSocket(conf.base, conf.token)
        aoMudarConexao(Conexao.CONECTANDO, "")
        // ⚠ Nenhum `.header("Origin", ...)` aqui. Ver ponto 1 do KDoc.
        val req = Request.Builder().url(alvo).build()
        ws = http.newWebSocket(req, Ouvinte())
    }

    private fun reagendar() {
        if (!querendoConectar) return
        val espera = Backoff.esperaMs(tentativas)
        tentativas++
        // RASTRO OBRIGATÓRIO. Isto age sozinho, em segundo plano, e o sintoma de
        // estar errado ("o app não volta") é idêntico ao de estar certo mas o
        // servidor estar fora. Sem esta linha, distinguir os dois exigiria
        // reencenar a queda inteira com um depurador acoplado.
        Log.i(TAG, "reconexão #$tentativas em ${espera}ms")
        laco.schedule({ if (querendoConectar) abrir() }, espera, TimeUnit.MILLISECONDS)
    }

    private inner class Ouvinte : WebSocketListener() {

        override fun onOpen(webSocket: WebSocket, response: Response) {
            val reconexao = tentativas > 0
            tentativas = 0
            // SEMPRE, e antes de qualquer outra coisa. Ver ponto 2 do KDoc.
            val id = conversaId()
            webSocket.send(Envio.setConversa(id))
            Log.i(TAG, if (reconexao) "RECONECTADO; set_conversa=$id" else "conectado; set_conversa=$id")
            aoMudarConexao(Conexao.ABERTO, "")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val m = parseMensagem(text)
            if (m == null) {
                Log.w(TAG, "quadro ilegível, ignorado")
                return
            }
            // ACK do proativo aqui, e não na UI: sem ele o scheduler não conclui
            // nem reprograma o agendamento, e reentrega na próxima conexão
            // (ws.py:441-446). Amarrá-lo ao desenho da bolha faria o ack depender
            // de a tela estar viva.
            if (m is MensagemServidor.Proativo && m.ackId != null) {
                webSocket.send(Envio.ackProativo(m.ackId))
            }
            aoReceber(m)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            ws = null
            // ⚠ MEDIDO em 2026-08-02, e CONTRARIA o plano (§1.1 e Risco R5, que
            // dizem que o cliente "verá apenas um close 1008"):
            //
            //     token certo  -> HTTP/1.1 101 Switching Protocols
            //     token errado -> HTTP/1.1 403 Forbidden
            //     sem token    -> HTTP/1.1 403 Forbidden
            //
            // O gate roda ANTES do `accept()` (main.py:538-548), então o uvicorn
            // nunca faz o upgrade: o `websocket.close(1008)` do lado do servidor
            // vira uma RECUSA DE HANDSHAKE HTTP. O OkHttp reporta isso aqui, em
            // `onFailure` com `response.code == 403` — `onClosed` JAMAIS é
            // chamado. Sem este ramo o app trataria "token errado" como queda de
            // rede e reconectaria em laço para sempre, sem nunca dizer ao dono
            // que o problema é o token.
            val codigo = response?.code
            if (codigo == 401 || codigo == 403) {
                querendoConectar = false
                aoMudarConexao(
                    Conexao.RECUSADO,
                    "O servidor recusou o acesso (HTTP $codigo). Confira o token nos ajustes.",
                )
                return
            }
            aoMudarConexao(Conexao.CAIU, t.message ?: "sem conexão")
            reagendar()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            ws = null
            if (code == FECHADO_POR_POLITICA) {
                // 1008 é o MESMO código para token errado, Origin divergente e
                // "sem token e não é loopback" (main.py:544, 548), e vem sem
                // corpo. Reconectar em laço só repetiria a recusa — quem
                // desambigua é o `/api/health` da tela de configuração.
                querendoConectar = false
                aoMudarConexao(Conexao.RECUSADO, "O servidor recusou o acesso (token ou permissão).")
                return
            }
            aoMudarConexao(Conexao.CAIU, reason)
            reagendar()
        }
    }

    private companion object {
        const val TAG = "ClienteMente"
        const val FECHADO_POR_POLITICA = 1008
    }
}
