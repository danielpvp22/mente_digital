package com.mentedigital.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A resposta de `/api/energia`, lida do jeito que o servidor a manda.
 *
 * Vale um arquivo de teste porque a leitura tem duas armadilhas que já custaram
 * caro neste projeto: nome de campo DEDUZIDO em vez de medido (a lição do
 * `tts`/`voz` e do `pergunta`/`q`), e "não medi" virando zero — o `energia.py` do
 * servidor é explícito de que campo ausente é null, "porque zero seria
 * indistinguível de 'medi e não há consumo'".
 */
class EnergiaTest {

    private fun json(texto: String) = JSONObject(texto)

    @Test
    fun `le o estado e a medicao de dentro de depois`() {
        val e = Energia.de(json("""
            {"estado":"descansando","antes":{"vram_mb":5200},
             "depois":{"vram_mb":412,"ram_commit_mb":1024,"ram_mb":900}}
        """.trimIndent()))
        assertEquals("descansando", e.estado)
        assertTrue(e.descansando)
        assertEquals(412, e.vramMb)
        assertEquals(1024, e.ramCommitMb)
    }

    @Test
    fun `campo nulo continua nulo, nunca vira zero`() {
        val e = Energia.de(json("""{"estado":"ligado","depois":{"vram_mb":null}}"""))
        assertNull(e.vramMb)
        assertNull(e.ramCommitMb)
        assertFalse(e.descansando)
    }

    @Test
    fun `resposta sem depois nao quebra`() {
        val e = Energia.de(json("""{"estado":"ligado"}"""))
        assertNull(e.vramMb)
        assertEquals("", e.resumo())
    }

    @Test
    fun `resumo em portugues com virgula`() {
        val e = Energia("descansando", 412, 1024)
        assertEquals("0,4 GB de VRAM · 1,0 GB de RAM em uso", e.resumo())
    }

    @Test
    fun `resumo com so uma metrica`() {
        assertEquals("2,0 GB de VRAM em uso", Energia("ligado", 2048, null).resumo())
    }
}
