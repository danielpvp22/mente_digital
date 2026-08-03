package com.mentedigital.app

/**
 * Montagem de endereços. PURO/testável.
 *
 * Só o que as duas telas NATIVAS precisam (configuração e boot). O WebSocket e o
 * backoff de reconexão saíram daqui na correção de rumo de 2026-08-02: quem
 * mantém a conexão é a SPA dentro do WebView, com o mesmo código que o navegador
 * e a janela do PC usam. Um segundo cliente seria uma segunda verdade.
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

    /** Rota `/api/...`. Sem token na URL: nas `/api` ele vai no header
     *  `X-Mente-Token` (main.py:243), que é o certo — o app nativo não tem a
     *  limitação do `<img>` que forçou a query no front. */
    fun api(base: String, caminho: String): String {
        val c = if (caminho.startsWith("/")) caminho else "/$caminho"
        return normalizarBase(base) + c
    }

    /**
     * Rota do VIGIA — o processo mínimo que fica de plantão no PC quando o
     * assistente está encerrado (`mente_digital/vigia.py`).
     *
     * O endereço é DERIVADO do que o dono já configurou: mesma máquina, outra
     * porta. Pedir um segundo campo na tela de configuração seria pedir duas vezes
     * a mesma informação — e um campo a mais para errar.
     */
    fun vigia(base: String, caminho: String, porta: Int = PORTA_VIGIA): String {
        val limpa = normalizarBase(base)
        if (limpa.isEmpty()) return ""
        val esquema = limpa.substringBefore("://")
        val semEsquema = limpa.substringAfter("://")
        // Corta porta E qualquer caminho: o host é tudo até o primeiro ':' ou '/'.
        val host = semEsquema.substringBefore('/').substringBefore(':')
        val c = if (caminho.startsWith("/")) caminho else "/$caminho"
        return "$esquema://$host:$porta$c"
    }

    /** O default do `vigia_port` no servidor (config.py). */
    const val PORTA_VIGIA = 8765
}
