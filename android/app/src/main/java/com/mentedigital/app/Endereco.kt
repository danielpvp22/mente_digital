package com.mentedigital.app

/**
 * Montagem de endereços. PURO/testável — e vale a pena testar porque cada erro
 * aqui vira uma falha sem mensagem útil:
 *
 * - esquema errado no WebSocket => o OkHttp recusa antes de sair da máquina;
 * - token na superfície errada => close 1008 sem corpo, indistinguível de
 *   "aparelho não autorizado" (main.py:541-548);
 * - barra sobrando => 404 numa rota que existe.
 */
object Endereco {

    /** Normaliza o que o dono digitou. Aceita "192.168.0.10:8000", com ou sem
     *  esquema, com ou sem barra no fim. */
    fun normalizarBase(bruto: String): String {
        var s = bruto.trim()
        if (s.isEmpty()) return ""
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://$s"
        return s.trimEnd('/')
    }

    /**
     * A URL do WebSocket.
     *
     * ⚠ O token vai na QUERY, e não há escolha: o gate do WS lê só
     * `websocket.query_params` (main.py:541). É o tradeoff que o plano registra
     * em §2.2 — mitigado por TLS (Fase 5) e pelo `log_level="error"` do uvicorn,
     * não resolvido.
     */
    fun webSocket(base: String, token: String): String {
        val b = normalizarBase(base)
        val comEsquema = when {
            b.startsWith("https://") -> "wss://" + b.removePrefix("https://")
            else -> "ws://" + b.removePrefix("http://")
        }
        val alvo = "$comEsquema/ws/chat_live"
        return if (token.isEmpty()) alvo else "$alvo?token=${codificar(token)}"
    }

    /** Rota `/api/...`. Sem token na URL: nas /api ele vai no header
     *  `X-Mente-Token` (main.py:243), que é o certo — o app nativo não tem a
     *  limitação do `<img>` que forçou a query no front. */
    fun api(base: String, caminho: String): String {
        val c = if (caminho.startsWith("/")) caminho else "/$caminho"
        return normalizarBase(base) + c
    }

    /** Percent-encode do que pode aparecer num token. `URLEncoder` não está
     *  disponível de forma idêntica em todo nível de API para este caso, e o
     *  conjunto aqui é pequeno e conhecido. */
    private fun codificar(v: String): String = buildString {
        for (ch in v) {
            if (ch.isLetterOrDigit() || ch in "-._~") append(ch)
            else append('%').append("%02X".format(ch.code))
        }
    }
}

/**
 * Backoff da reconexão. PURO.
 *
 * Os números são os do front (index.html:906-910): 1 s, fator 1,6, teto 15 s.
 * Copiados de propósito — dois clientes do mesmo servidor com políticas de
 * reconexão diferentes é uma diferença que ninguém consegue depurar depois.
 */
object Backoff {
    const val INICIAL_MS = 1000L
    const val FATOR = 1.6
    const val TETO_MS = 15000L

    fun esperaMs(tentativa: Int): Long {
        if (tentativa <= 0) return INICIAL_MS
        var v = INICIAL_MS.toDouble()
        repeat(tentativa) { v *= FATOR }
        return v.toLong().coerceAtMost(TETO_MS)
    }
}
