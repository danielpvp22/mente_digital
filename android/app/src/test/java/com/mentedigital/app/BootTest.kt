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

    @Test
    fun `o campo explicito do servidor vence a deducao`() {
        // Este é o conserto de uma ambiguidade real: durante um boot NORMAL o LLM
        // também está em false, e o app deduzia "standby" — mandando um `ligar` por
        // cima de um carregamento já em curso. Com o campo explícito, boot é boot.
        val subindo = Saude(true, mapOf("llm" to false), descansandoDito = false)
        assertFalse(subindo.descansando)

        val dormindo = Saude(true, mapOf("llm" to false), descansandoDito = true)
        assertTrue(dormindo.descansando)
    }

    @Test
    fun `servidor antigo sem o campo volta a deducao`() {
        // APK novo contra servidor velho: sem o campo, a heurística de antes.
        assertTrue(Saude(true, mapOf("llm" to false), descansandoDito = null).descansando)
    }

    @Test
    fun `descansando dito por servidor inalcancavel nao vale`() {
        assertFalse(Saude(false, descansandoDito = true).descansando)
    }

    // ------------------------------------------------- o RELÓGIO do boot ----
    // O defeito medido em 2026-08-04 no Redmi: o contador ficou em "7s" por mais
    // de dois minutos, e a tela pareceu travada. Não era travamento — o número
    // somava 1 tique por VOLTA do laço (valendo 0,7 s no visor, o `delay(700)`),
    // não por tempo. Como cada volta faz de 1 a 3 chamadas de rede BLOQUEANTES
    // antes do delay, uma volta com o endereço inalcançável custa ~8 s de parede.
    // Um contador que conta voltas em vez de tempo congela E mente para menos.

    @Test
    fun `o relogio comeca em zero`() {
        assertEquals(0, Boot.segundosDecorridos(1_000L, 1_000L))
    }

    @Test
    fun `o relogio conta segundos de parede, arredondando para baixo`() {
        assertEquals(1, Boot.segundosDecorridos(0L, 1_000L))
        assertEquals(1, Boot.segundosDecorridos(0L, 1_999L))
        assertEquals(7, Boot.segundosDecorridos(0L, 7_400L))
        assertEquals(120, Boot.segundosDecorridos(500L, 120_500L))
    }

    @Test
    fun `relogio que anda para tras nao devolve negativo`() {
        // Defensivo: um número negativo no visor é pior que um zero parado.
        assertEquals(0, Boot.segundosDecorridos(5_000L, 4_000L))
    }

    @Test
    fun `voltas lentas NAO encolhem o tempo mostrado`() {
        // A regressão, travada: 10 voltas de ~8 s (sonda + vigia num endereço
        // inalcançável) são 80 s de parede. A régua ANTIGA mostraria 7 s.
        val voltas = 10
        assertEquals(7, voltas * 7 / 10)                          // o que era
        assertEquals(80, Boot.segundosDecorridos(0L, 80_000L))    // o que é
    }

    @Test
    fun `a saida de emergencia e oferecida por tempo de PAREDE`() {
        assertFalse(Boot.ofereceSaida(Boot.SEGUNDOS_ATE_OFERECER_SAIDA - 1))
        assertTrue(Boot.ofereceSaida(Boot.SEGUNDOS_ATE_OFERECER_SAIDA))

        // Por que isto importa mais que o visor: o botão de saída pendia do MESMO
        // número. Na régua antiga seriam necessárias 215 voltas para "150 s" —
        // a ~8 s cada, quase meia hora de parede antes de a tela oferecer saída.
        // É o defeito "tela de boot sem saída" do handoff, pela mesma causa.
        val voltasNaReguaAntiga = Boot.SEGUNDOS_ATE_OFERECER_SAIDA * 10 / 7
        assertTrue(voltasNaReguaAntiga * 8 / 60 > 25)             // minutos
    }

    @Test
    fun `a sonda de saude tem orcamento curto`() {
        // O `/api/health` é um dict — responde em milissegundos ou não está lá.
        // O cliente que havia ali tinha 200 s de LEITURA, criado para o `ligar`
        // carregar modelo. Herdado na sonda, um TCP que conecta e não responde
        // (endereço de LAN visto de fora, proxy que aceita e cala) pendurava a
        // volta por mais de três minutos.
        assertTrue(Servidor.ORCAMENTO_SONDA_MS in 1_000L..15_000L)
    }
}
