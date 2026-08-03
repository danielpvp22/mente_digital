package com.mentedigital.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * O CONTRATO da ponte de microfone — o único pedaço de código nativo que o
 * caminho de voz ainda tem depois da correção de rumo.
 *
 * Tudo o mais (RMS, barge-in, mudo, envio) voltou para o `index.html`, onde já
 * estava e onde o navegador e a janela do PC continuam rodando. O que sobrou
 * aqui é: capturar em 16 kHz, fatiar em 1024 amostras, e entregar em base64 num
 * formato que o decodificador JS leia de volta bit a bit.
 *
 * ⚠ Os dois erros possíveis são MUDOS. Quadro de tamanho errado muda o VAD do
 * servidor sem nada falhar (`vad_min_frames` conta MENSAGENS, não tempo —
 * ws.py:379, 393-394). Endianness trocada não dá erro: dá transcrição de ruído.
 */
class PonteTest {

    @Test
    fun `o quadro tem 1024 amostras e 2048 bytes, em 16 kHz`() {
        assertEquals(1024, Gravador.AMOSTRAS_QUADRO)
        assertEquals(16000, Gravador.TAXA)
        val bytes = Gravador.paraBytes(ShortArray(Gravador.AMOSTRAS_QUADRO), Gravador.AMOSTRAS_QUADRO)
        assertEquals(2048, bytes.size)
    }

    @Test
    fun `paraBytes escreve LITTLE-endian`() {
        // `np.frombuffer(raw, dtype=np.int16)` num x86-64 lê little-endian
        // (ws.py:312). Trocar a ordem não daria erro em lugar nenhum.
        val b = Gravador.paraBytes(shortArrayOf(0x0102), 1)
        assertEquals(0x02.toByte(), b[0])
        assertEquals(0x01.toByte(), b[1])
    }

    /**
     * O decodificador que vive no `index.html` (`window.__micAndroid`), portado
     * linha a linha. Se este teste passar, a página remonta exatamente as
     * amostras que o `AudioRecord` capturou — inclusive as negativas, que são
     * onde o erro de sinal apareceria.
     */
    private fun decodificarComoOJs(b64: String): FloatArray {
        val bin = java.util.Base64.getDecoder().decode(b64)
        val n = bin.size shr 1
        val f = FloatArray(n)
        for (i in 0 until n) {
            var v = ((bin[i * 2 + 1].toInt() and 0xFF) shl 8) or (bin[i * 2].toInt() and 0xFF)
            if (v >= 0x8000) v -= 0x10000                 // int16 com sinal
            f[i] = v / 32768f
        }
        return f
    }

    @Test
    fun `o JS remonta as MESMAS amostras que o microfone capturou`() {
        val originais = shortArrayOf(
            0, 1, -1, 127, -128, 32767, -32768, 12345, -12345, 256, -256,
        )
        val b64 = java.util.Base64.getEncoder()
            .encodeToString(Gravador.paraBytes(originais, originais.size))
        val voltou = decodificarComoOJs(b64)

        assertEquals(originais.size, voltou.size)
        originais.forEachIndexed { i, s ->
            assertEquals("amostra $i ($s) voltou errada", s / 32768f, voltou[i], 1e-6f)
        }
    }

    @Test
    fun `o silencio continua silencio depois da ida e volta`() {
        val b64 = java.util.Base64.getEncoder()
            .encodeToString(Gravador.paraBytes(ShortArray(1024), 1024))
        assertTrue(decodificarComoOJs(b64).all { it == 0f })
    }
}
