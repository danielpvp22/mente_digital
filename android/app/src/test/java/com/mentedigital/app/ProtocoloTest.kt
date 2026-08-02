package com.mentedigital.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * O parser do protocolo — a parte do app que quebraria mais silenciosamente.
 *
 * O servidor IGNORA `tipo` desconhecido sem devolver erro (ws.py:426-502 não tem
 * `else`), e a mesma leniência vale na volta. Ou seja: um campo lido errado não
 * dá exceção nem log no servidor — só deixa de funcionar. Estes testes são o
 * único lugar onde isso aparece antes do aparelho.
 */
class ProtocoloTest {

    @Test
    fun `transcricao vira bolha do usuario`() {
        val m = parseMensagem("""{"tipo":"transcricao","texto":"quanto e 2+2"}""")
        assertEquals(MensagemServidor.Transcricao("quanto e 2+2"), m)
    }

    @Test
    fun `token traz o pedaco da resposta`() {
        assertEquals(MensagemServidor.Token(" quatro"),
            parseMensagem("""{"tipo":"token","texto":" quatro"}"""))
    }

    @Test
    fun `fontes sao strings com prefixo, nao objetos`() {
        val m = parseMensagem(
            """{"tipo":"fontes","rota":"RAM+Banco","itens":["memoria","nota:x.md","web:pt.wikipedia.org"]}"""
        ) as MensagemServidor.Fontes
        assertEquals("RAM+Banco", m.rota)
        assertEquals(listOf("memoria", "nota:x.md", "web:pt.wikipedia.org"), m.itens)
    }

    @Test
    fun `fontes sem itens nao estoura`() {
        val m = parseMensagem("""{"tipo":"fontes","rota":"Web"}""") as MensagemServidor.Fontes
        assertTrue(m.itens.isEmpty())
    }

    @Test
    fun `proativo com ack_id preserva o id`() {
        val m = parseMensagem("""{"tipo":"proativo","texto":"tomar remedio","ack_id":"ag-7"}""")
        assertEquals(MensagemServidor.Proativo("tomar remedio", "ag-7"), m)
    }

    @Test
    fun `proativo SEM ack_id devolve null, nao string vazia`() {
        // `optString` devolve "" para ausente. Se "" passasse como id, o app
        // confirmaria um agendamento inexistente — e o de verdade continuaria
        // sendo reentregue a cada conexão (ws.py:441-446).
        val m = parseMensagem("""{"tipo":"proativo","texto":"oi"}""") as MensagemServidor.Proativo
        assertNull(m.ackId)
    }

    @Test
    fun `barge_in e objeto unico`() {
        assertEquals(MensagemServidor.BargeIn, parseMensagem("""{"tipo":"barge_in"}"""))
    }

    @Test
    fun `tipo novo do servidor nao vira crash nem silencio`() {
        val m = parseMensagem("""{"tipo":"inventado_amanha","x":1}""")
        assertEquals(MensagemServidor.Desconhecida("inventado_amanha"), m)
    }

    @Test
    fun `json invalido devolve null`() {
        assertNull(parseMensagem("isto nao e json"))
        assertNull(parseMensagem(""))
    }

    // --------------------------------------------------------------- envio --
    @Test
    fun `texto usa o campo payload, nao texto`() {
        // O servidor lê `payload` (ws.py:485-502). Mandar {"texto":...} não dá
        // erro nenhum: vira um no-op silencioso.
        assertTrue(Envio.texto("oi").contains("\"payload\""))
        assertTrue(Envio.texto("oi").contains("\"tipo\":\"texto\""))
    }

    @Test
    fun `todos os envios carregam um tipo`() {
        val quadros = listOf(
            Envio.texto("a"), Envio.setConversa("1"), Envio.novaConversa("2"),
            Envio.carregarConversa("3"), Envio.ackProativo("4"), Envio.encerrarSessao(),
        )
        quadros.forEach { assertTrue(it, it.contains("\"tipo\"")) }
    }

    @Test
    fun `ack_proativo usa o campo id`() {
        assertEquals("""{"tipo":"ack_proativo","id":"ag-9"}""", Envio.ackProativo("ag-9"))
    }
}
