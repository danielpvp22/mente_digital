package com.mentedigital.app

import java.nio.ByteBuffer
import java.nio.ByteOrder

/** PCM decodificado de um WAV: as amostras e a taxa REAL do arquivo. */
data class Pcm(val taxaHz: Int, val canais: Int, val amostras: ShortArray) {
    override fun equals(other: Any?): Boolean =
        other is Pcm && taxaHz == other.taxaHz && canais == other.canais &&
            amostras.contentEquals(other.amostras)

    override fun hashCode(): Int = (taxaHz * 31 + canais) * 31 + amostras.contentHashCode()
}

/**
 * Leitura do WAV que o servidor manda, e o RMS do microfone. PURO/testável.
 *
 * ⚠ A TAXA DE SAÍDA NÃO É FIXA E NÃO É A DE ENTRADA. O servidor grava o WAV com
 * a taxa do modelo em tempo de execução: Piper usa a do `.onnx` da voz
 * (audio.py:433-436) e o XTTS usa 24000 (tts_xtts.py:412-415). Trocar
 * `MENTE_TTS_ENGINE` muda a taxa SEM AVISO, e um player com número fixo passaria
 * a falar com voz de esquilo ou de trator. Por isso a taxa sai daqui, do
 * cabeçalho, e nunca de uma constante.
 *
 * ⚠ E O `data` NÃO COMEÇA SEMPRE NO BYTE 44. Isso é verdade só para o WAV
 * canônico de 3 blocos; qualquer `LIST`/`fact` antes desloca tudo. Percorrer os
 * blocos custa 20 linhas e evita um defeito que se manifesta como ruído no
 * início da fala — sintoma que ninguém associa a um parser.
 */
object Wav {

    /** Decodifica um WAV PCM de 16 bits. `null` se não for isso. */
    fun ler(bytes: ByteArray): Pcm? {
        if (bytes.size < 12) return null
        val b = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
        if (texto(bytes, 0, 4) != "RIFF" || texto(bytes, 8, 4) != "WAVE") return null

        var pos = 12
        var taxa = 0
        var canais = 1
        var bits = 16
        while (pos + 8 <= bytes.size) {
            val nome = texto(bytes, pos, 4)
            val tam = b.getInt(pos + 4)
            val corpo = pos + 8
            if (tam < 0 || corpo > bytes.size) return null
            when (nome) {
                "fmt " -> {
                    if (corpo + 16 > bytes.size) return null
                    canais = b.getShort(corpo + 2).toInt()
                    taxa = b.getInt(corpo + 4)
                    bits = b.getShort(corpo + 14).toInt()
                }
                "data" -> {
                    if (taxa <= 0 || bits != 16) return null
                    // `tam` pode mentir (arquivo truncado no meio da transmissão);
                    // o menor entre ele e o que sobrou é o único seguro.
                    val fim = minOf(corpo + tam, bytes.size)
                    val n = (fim - corpo) / 2
                    if (n <= 0) return null
                    val amostras = ShortArray(n)
                    val sb = ByteBuffer.wrap(bytes, corpo, n * 2)
                        .order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
                    sb.get(amostras)
                    return Pcm(taxa, canais, amostras)
                }
            }
            // Blocos de tamanho ÍMPAR levam um byte de preenchimento (RIFF).
            pos = corpo + tam + (tam and 1)
        }
        return null
    }

    /**
     * RMS normalizado (0..1) de um quadro do microfone.
     *
     * É o mesmo cálculo do navegador (index.html), e o limiar que o consome é o
     * do cliente (0,12), NÃO o do servidor (0,02) — de propósito: o microfone do
     * dono capta a voz dele direto, enquanto o servidor precisa tolerar eco
     * atenuado. Copiar o limiar errado faria o app se cortar sozinho.
     */
    fun rms(quadro: ShortArray, tamanho: Int = quadro.size): Float {
        if (tamanho <= 0) return 0f
        var soma = 0.0
        for (i in 0 until tamanho) {
            val v = quadro[i] / 32768.0
            soma += v * v
        }
        return kotlin.math.sqrt(soma / tamanho).toFloat()
    }

    private fun texto(b: ByteArray, off: Int, n: Int): String =
        if (off + n > b.size) "" else String(b, off, n, Charsets.US_ASCII)
}
