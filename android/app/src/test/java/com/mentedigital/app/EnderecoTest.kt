package com.mentedigital.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EnderecoTest {

    @Test
    fun `aceita ip e porta sem esquema`() {
        assertEquals("http://192.168.0.10:8000", Endereco.normalizarBase("192.168.0.10:8000"))
    }

    @Test
    fun `tira barra do fim`() {
        assertEquals("http://x:8000", Endereco.normalizarBase("http://x:8000/"))
        assertEquals("http://x:8000", Endereco.normalizarBase("  http://x:8000//  "))
    }

    @Test
    fun `preserva https`() {
        assertEquals("https://casa:8000", Endereco.normalizarBase("https://casa:8000"))
    }

    @Test
    fun `websocket troca http por ws`() {
        assertEquals("ws://x:8000/ws/chat_live?token=abc",
            Endereco.webSocket("http://x:8000", "abc"))
    }

    @Test
    fun `websocket troca https por wss`() {
        // Esquema errado aqui faz o OkHttp recusar antes de sair da máquina.
        assertTrue(Endereco.webSocket("https://x:8000", "a").startsWith("wss://"))
    }

    @Test
    fun `sem token nao deixa query pendurada`() {
        assertEquals("ws://x:8000/ws/chat_live", Endereco.webSocket("http://x:8000", ""))
    }

    @Test
    fun `token vai codificado`() {
        val u = Endereco.webSocket("http://x:8000", "a b/c&d")
        assertTrue(u, u.endsWith("token=a%20b%2Fc%26d"))
    }

    @Test
    fun `api nao leva token na url`() {
        // Nas /api o token vai no header X-Mente-Token (main.py:243). Mandá-lo na
        // query funcionaria, mas o deixaria no log de qualquer proxy — e o app
        // nativo não tem a limitação do <img> que forçou a query no front.
        val u = Endereco.api("http://x:8000", "/api/health")
        assertEquals("http://x:8000/api/health", u)
        assertFalse(u.contains("token"))
    }

    @Test
    fun `api aceita caminho com e sem barra`() {
        assertEquals("http://x:8000/api/conversas", Endereco.api("http://x:8000/", "api/conversas"))
    }
}

class BackoffTest {

    @Test
    fun `primeira tentativa espera um segundo`() {
        assertEquals(1000L, Backoff.esperaMs(0))
    }

    @Test
    fun `cresce por fator de 1 ponto 6`() {
        assertEquals(1600L, Backoff.esperaMs(1))
        assertEquals(2560L, Backoff.esperaMs(2))
    }

    @Test
    fun `respeita o teto de 15 segundos`() {
        // Os números são os do front (index.html:906-910). Dois clientes do mesmo
        // servidor com políticas de reconexão diferentes é uma divergência que
        // ninguém consegue depurar depois.
        assertEquals(15000L, Backoff.esperaMs(50))
        assertTrue(Backoff.esperaMs(9) <= Backoff.TETO_MS)
    }

    @Test
    fun `nunca devolve espera negativa ou zero`() {
        (-3..20).forEach { assertTrue(Backoff.esperaMs(it) >= 1000L) }
    }
}
