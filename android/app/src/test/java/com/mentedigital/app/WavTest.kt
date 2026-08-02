package com.mentedigital.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * O parser do WAV que o servidor manda, e o RMS do barge-in.
 *
 * Por que vale testar: os dois defeitos possíveis aqui são MUDOS. Taxa lida
 * errada dá voz de esquilo ou de trator (e a taxa MUDA quando o dono troca
 * `MENTE_TTS_ENGINE` — Piper usa a do `.onnx`, XTTS usa 24000). Offset de `data`
 * errado dá um estalo no início de cada frase, que ninguém associa a um parser.
 */
class WavTest {

    // ---------------------------------------------------------- construtores --
    private fun bloco(nome: String, corpo: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        out.write(nome.toByteArray(Charsets.US_ASCII))
        out.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(corpo.size).array())
        out.write(corpo)
        if (corpo.size % 2 == 1) out.write(0)      // preenchimento do RIFF
        return out.toByteArray()
    }

    private fun fmt(taxa: Int, canais: Int = 1, bits: Int = 16): ByteArray =
        ByteBuffer.allocate(16).order(ByteOrder.LITTLE_ENDIAN)
            .putShort(1)                            // PCM
            .putShort(canais.toShort())
            .putInt(taxa)
            .putInt(taxa * canais * bits / 8)       // byte rate
            .putShort((canais * bits / 8).toShort())
            .putShort(bits.toShort())
            .array()

    private fun dados(vararg amostras: Short): ByteArray {
        val bb = ByteBuffer.allocate(amostras.size * 2).order(ByteOrder.LITTLE_ENDIAN)
        amostras.forEach { bb.putShort(it) }
        return bb.array()
    }

    private fun wav(taxa: Int, amostras: ShortArray, extras: List<ByteArray> = emptyList()): ByteArray {
        val miolo = ByteArrayOutputStream()
        miolo.write("WAVE".toByteArray(Charsets.US_ASCII))
        miolo.write(bloco("fmt ", fmt(taxa)))
        extras.forEach { miolo.write(it) }
        miolo.write(bloco("data", dados(*amostras.toTypedArray().toShortArray())))
        val corpo = miolo.toByteArray()
        val out = ByteArrayOutputStream()
        out.write("RIFF".toByteArray(Charsets.US_ASCII))
        out.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(corpo.size).array())
        out.write(corpo)
        return out.toByteArray()
    }

    // ---------------------------------------------------------------- leitura --
    @Test
    fun `le taxa e amostras de um wav canonico`() {
        val p = Wav.ler(wav(22050, shortArrayOf(1, -2, 3, -4)))
        assertNotNull(p)
        assertEquals(22050, p!!.taxaHz)
        assertEquals(1, p.canais)
        assertTrue(shortArrayOf(1, -2, 3, -4).contentEquals(p.amostras))
    }

    @Test
    fun `a taxa vem do cabecalho, nao de constante`() {
        // Piper e XTTS gravam com taxas diferentes, e trocar MENTE_TTS_ENGINE
        // muda o número SEM AVISO. Um player com valor fixo falaria errado.
        assertEquals(16000, Wav.ler(wav(16000, shortArrayOf(0, 0)))!!.taxaHz)
        assertEquals(24000, Wav.ler(wav(24000, shortArrayOf(0, 0)))!!.taxaHz)
        assertEquals(48000, Wav.ler(wav(48000, shortArrayOf(0, 0)))!!.taxaHz)
    }

    @Test
    fun `data NAO precisa comecar no byte 44`() {
        // Um bloco LIST antes do data desloca tudo. Assumir 44 é o erro clássico,
        // e o sintoma é ruído no começo da fala.
        val listExtra = bloco("LIST", ByteArray(30) { 7 })
        val p = Wav.ler(wav(16000, shortArrayOf(5, 6, 7), extras = listOf(listExtra)))
        assertNotNull(p)
        assertTrue(shortArrayOf(5, 6, 7).contentEquals(p!!.amostras))
    }

    @Test
    fun `bloco de tamanho impar leva byte de preenchimento`() {
        val impar = bloco("fact", ByteArray(3) { 1 })      // 3 bytes + 1 de padding
        val p = Wav.ler(wav(16000, shortArrayOf(9), extras = listOf(impar)))
        assertNotNull(p)
        assertEquals(9.toShort(), p!!.amostras[0])   // Short, não Int: o assertEquals boxa
    }

    @Test
    fun `wav truncado no meio nao estoura`() {
        // Acontece de verdade: a mensagem chega cortada por uma queda de rede.
        val inteiro = wav(16000, shortArrayOf(1, 2, 3, 4, 5, 6))
        val cortado = inteiro.copyOf(inteiro.size - 5)
        val p = Wav.ler(cortado)
        assertNotNull("truncado deveria render o que sobrou, não null", p)
        assertTrue(p!!.amostras.isNotEmpty())
    }

    @Test
    fun `lixo nao vira audio`() {
        assertNull(Wav.ler(ByteArray(0)))
        assertNull(Wav.ler("nao sou um wav".toByteArray()))
        assertNull(Wav.ler(ByteArray(100) { 0 }))
    }

    // -------------------------------------------------------------------- rms --
    @Test
    fun `silencio tem rms zero`() {
        assertEquals(0f, Wav.rms(ShortArray(1024)), 1e-6f)
    }

    @Test
    fun `saturado tem rms proximo de um`() {
        assertEquals(1f, Wav.rms(ShortArray(64) { Short.MAX_VALUE }), 0.01f)
    }

    @Test
    fun `o limiar do cliente separa fala de ruido de fundo`() {
        // BARGE_RMS = 0,12 (index.html:975). Um sinal a 20% da escala passa; um
        // sussurro a 5% não. É o que impede o app de se cortar sozinho.
        val fala = ShortArray(1024) { (0.20 * Short.MAX_VALUE).toInt().toShort() }
        val fundo = ShortArray(1024) { (0.05 * Short.MAX_VALUE).toInt().toShort() }
        assertTrue(Wav.rms(fala) >= ChatViewModel.BARGE_RMS)
        assertTrue(Wav.rms(fundo) < ChatViewModel.BARGE_RMS)
    }

    @Test
    fun `quadro vazio nao divide por zero`() {
        assertEquals(0f, Wav.rms(ShortArray(0)), 1e-6f)
        assertEquals(0f, Wav.rms(ShortArray(10), 0), 1e-6f)
    }

    // ------------------------------------------------------- contrato do envio --
    @Test
    fun `o quadro do microfone tem 1024 amostras e 2048 bytes`() {
        // O achado nº 1 do plano: `vad_min_frames` conta MENSAGENS, não tempo.
        // Mudar este número muda o VAD do servidor sem nada falhar.
        assertEquals(1024, Gravador.AMOSTRAS_QUADRO)
        assertEquals(16000, Gravador.TAXA)
        val bytes = Gravador.paraBytes(ShortArray(Gravador.AMOSTRAS_QUADRO), Gravador.AMOSTRAS_QUADRO)
        assertEquals(2048, bytes.size)
    }

    @Test
    fun `paraBytes escreve LITTLE-endian`() {
        // `np.int16` num x86-64 lê little-endian. Trocar a ordem não daria erro:
        // daria transcrição de ruído.
        val b = Gravador.paraBytes(shortArrayOf(0x0102), 1)
        assertEquals(0x02.toByte(), b[0])
        assertEquals(0x01.toByte(), b[1])
    }
}
