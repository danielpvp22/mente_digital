package com.mentedigital.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A tela de boot do celular conta a MESMA história que a do PC — e estes testes
 * são os mesmos de `tests/test_app_boot.py`, pela mesma razão que eles existem:
 * em 2026-08-02 a tela do desktop anunciava "tudo pronto" com 24 s de trabalho
 * de fundo pela frente, e a primeira pergunta pagava a conta. Um marco que não é
 * contado é um marco que mente.
 */
class BootTest {

    private fun nenhum() = Boot.MARCOS.associate { it.first to false }
    private fun todos() = Boot.MARCOS.associate { it.first to true }

    @Test
    fun `os pesos somam cem`() {
        // Um marco que não soma some da conta e a barra nunca fecha em 100%.
        assertEquals(100, Boot.MARCOS.sumOf { it.third })
    }

    @Test
    fun `as chaves sao unicas`() {
        val chaves = Boot.MARCOS.map { it.first }
        assertEquals(chaves.size, chaves.toSet().size)
    }

    @Test
    fun `vazio e zero e nao libera`() {
        assertEquals(0 to false, Boot.progresso(nenhum()))
    }

    @Test
    fun `completo e cem e libera`() {
        assertEquals(100 to true, Boot.progresso(todos()))
    }

    @Test
    fun `trabalho de fundo pendente NAO libera a tela`() {
        // O arrependimento de 2026-08-02, travado em teste também aqui.
        val p = todos().toMutableMap().apply { this["fundo"] = false }
        val (pct, libera) = Boot.progresso(p)
        assertFalse(libera)
        assertTrue(pct < 100)
    }

    @Test
    fun `o estagio segue a ordem real do boot`() {
        val p = nenhum().toMutableMap()
        assertEquals("Acordando a mente", Boot.estagio(p))
        p["servidor"] = true; assertEquals("Afinando a escuta", Boot.estagio(p))
        p["stt"] = true; assertEquals("Abrindo o vault", Boot.estagio(p))
        p["vault"] = true; assertEquals("Carregando o modelo", Boot.estagio(p))
        p["llm"] = true; assertEquals("Preparando a voz", Boot.estagio(p))
        p["voz"] = true; assertEquals("Terminando o índice", Boot.estagio(p))
        p["fundo"] = true; assertEquals("Tudo pronto", Boot.estagio(p))
    }

    // ------------------------------------------------------------- standby --
    @Test
    fun `servidor de pe com o LLM solto e STANDBY, nao queda`() {
        // É a distinção que faz o app ACORDAR o PC em vez de só esperar: o
        // servidor respondeu, mas os modelos estão descarregados.
        val s = Saude(true, mapOf("llm" to false, "stt" to false, "vault" to true))
        assertTrue(s.descansando)
    }

    @Test
    fun `servidor inalcancavel NAO e standby`() {
        // Mandar `ligar` para quem não respondeu seria pedir a uma porta fechada.
        assertFalse(Saude(false).descansando)
    }

    @Test
    fun `tudo carregado NAO e standby`() {
        assertFalse(Saude(true, mapOf("llm" to true, "stt" to true)).descansando)
    }

    @Test
    fun `health inalcancavel apaga todos os marcos`() {
        assertTrue(Boot.marcosDe(Saude(false)).values.none { it })
    }

    @Test
    fun `tarefa de fundo pendente apaga o marco fundo`() {
        val s = Saude(true, mapOf("llm" to true), tarefasDeFundo = listOf("Malha e índice"))
        assertFalse(Boot.marcosDe(s)["fundo"]!!)
        assertTrue(Boot.marcosDe(s.copy(tarefasDeFundo = emptyList()))["fundo"]!!)
    }

    @Test
    fun `servidor que respondeu tem servidor e porta acesos`() {
        val m = Boot.marcosDe(Saude(true, mapOf("llm" to false)))
        assertTrue(m["servidor"]!!)
        assertTrue(m["porta"]!!)
    }
}
