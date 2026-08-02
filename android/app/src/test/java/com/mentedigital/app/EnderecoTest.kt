package com.mentedigital.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    fun `vazio continua vazio`() {
        assertEquals("", Endereco.normalizarBase("   "))
    }

    @Test
    fun `api nao leva token na url`() {
        // Nas /api o token vai no header X-Mente-Token (main.py:243). Na query
        // ele ficaria no log de qualquer proxy — e o app nativo não tem a
        // limitação do <img> que forçou a query no front.
        val u = Endereco.api("http://x:8000", "/api/health")
        assertEquals("http://x:8000/api/health", u)
        assertFalse(u.contains("token"))
    }

    @Test
    fun `api aceita caminho com e sem barra`() {
        assertEquals("http://x:8000/api/energia", Endereco.api("http://x:8000/", "api/energia"))
    }
}
