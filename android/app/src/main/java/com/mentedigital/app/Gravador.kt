package com.mentedigital.app

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Captura do microfone, no formato EXATO que o servidor lê.
 *
 * ⚠ O QUADRO DE 1024 AMOSTRAS NÃO É DETALHE — é o achado mais importante do
 * plano (§1.2). `vad_min_frames` e `barge_min_frames` contam MENSAGENS do
 * WebSocket, não tempo (ws.py:379, 393-394). Com o quadro de 1024 do navegador
 * isso dá 960 ms de fala mínima; usar o `getMinBufferSize()` do aparelho (que
 * varia por fabricante) mudaria esses limiares SEM NADA FALHAR — um buffer de
 * 4096 faria o servidor exigir 3,8 s de fala, e perguntas curtas passariam a ser
 * descartadas em silêncio. Por isso o buffer interno é uma coisa e o que sai
 * daqui é outra: fatiado em 1024 sempre.
 *
 * ⚠ `MediaRecorder` NÃO serve: entrega arquivo comprimido (AAC/AMR), e o
 * servidor faz `np.frombuffer(raw, dtype=np.int16)` sobre o payload inteiro.
 *
 * `VOICE_COMMUNICATION` e não `MIC`: liga o cancelamento de eco do aparelho, que
 * é o que torna o full-duplex viável mais adiante (§4.2c).
 */
class Gravador(
    private val aoQuadro: (ShortArray, Int) -> Unit,
) {
    @Volatile private var rodando = false
    private var thread: Thread? = null
    private var rec: AudioRecord? = null

    val ativo: Boolean get() = rodando

    @SuppressLint("MissingPermission")   // quem chama garante RECORD_AUDIO
    fun iniciar(): Boolean {
        if (rodando) return true
        val minimo = AudioRecord.getMinBufferSize(TAXA, CANAL, FORMATO)
        if (minimo <= 0) {
            Log.e(TAG, "getMinBufferSize devolveu $minimo — microfone indisponível")
            return false
        }
        // Buffer interno FOLGADO (>= 4 quadros): ele existe para o driver não
        // perder amostra enquanto a thread está ocupada. O tamanho do que SAI
        // daqui é independente disto — ver o KDoc.
        val buffer = maxOf(minimo, AMOSTRAS_QUADRO * 2 * 4)
        val r = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, TAXA, CANAL, FORMATO, buffer)
        } catch (e: Exception) {
            Log.e(TAG, "não consegui abrir o AudioRecord", e); return false
        }
        if (r.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord não inicializou (state=${r.state})")
            r.release(); return false
        }
        rec = r
        rodando = true
        r.startRecording()
        thread = Thread({ laco(r) }, "gravador").also { it.start() }
        Log.i(TAG, "gravando: ${TAXA}Hz mono PCM16, quadros de $AMOSTRAS_QUADRO amostras")
        return true
    }

    fun parar() {
        rodando = false
        thread?.join(500)
        thread = null
        try {
            rec?.stop()
        } catch (e: IllegalStateException) {
            // Já parado: acontece quando `parar()` corre com o fim do laço.
        }
        rec?.release()
        rec = null
    }

    private fun laco(r: AudioRecord) {
        val quadro = ShortArray(AMOSTRAS_QUADRO)
        while (rodando) {
            // Lê ATÉ encher o quadro. O `read` pode devolver menos que o pedido,
            // e mandar um quadro curto quebraria a aritmética de 64 ms por
            // mensagem em que o VAD do servidor foi calibrado.
            var lidas = 0
            while (rodando && lidas < AMOSTRAS_QUADRO) {
                val n = r.read(quadro, lidas, AMOSTRAS_QUADRO - lidas)
                if (n <= 0) {
                    if (n < 0) Log.w(TAG, "AudioRecord.read devolveu $n")
                    break
                }
                lidas += n
            }
            if (lidas == AMOSTRAS_QUADRO) aoQuadro(quadro, lidas)
        }
    }

    companion object {
        const val TAG = "Gravador"

        /** 16 kHz: o que o Whisper espera sem reamostragem. O servidor NÃO valida
         *  nem reamostra (ws.py:312 → audio.py:266-284) — taxa errada não levanta
         *  exceção, dá transcrição errada, com a fala acelerada. */
        const val TAXA = 16000
        const val CANAL = AudioFormat.CHANNEL_IN_MONO
        const val FORMATO = AudioFormat.ENCODING_PCM_16BIT

        /** 1024 amostras = 2048 bytes = 64 ms. Ver o KDoc da classe. */
        const val AMOSTRAS_QUADRO = 1024

        /** Converte para os bytes que vão no quadro binário do WebSocket:
         *  PCM16 LITTLE-ENDIAN, que é o que `np.int16` lê num x86-64. */
        fun paraBytes(quadro: ShortArray, n: Int): ByteArray {
            val bb = ByteBuffer.allocate(n * 2).order(ByteOrder.LITTLE_ENDIAN)
            for (i in 0 until n) bb.putShort(quadro[i])
            return bb.array()
        }
    }
}
