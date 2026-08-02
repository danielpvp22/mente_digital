package com.mentedigital.app

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.util.Log
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Fila de fala: um WAV por FRASE, tocados em sequência sem buraco entre eles.
 *
 * ⚠ Os dois erros opostos que o Risco R3 do plano descreve, e que este arquivo
 * existe para não cometer:
 *
 * 1. **Esperar a resposta inteira para tocar** joga fora o mecanismo de TTFA. O
 *    servidor manda uma mensagem `audio` POR FRASE conforme o chunker fecha
 *    sentenças (agent.py:112-119), e a consultoria #8 (`tts_chunk_primeiro_max_chars`)
 *    existe justamente para a PRIMEIRA frase sair antes. Bufferizar tudo
 *    transformaria "0,3 s até a voz" em "o tempo de gerar a resposta toda".
 * 2. **Um `MediaPlayer` novo por frase** insere um gap audível a cada frase — a
 *    fala sai picotada. `AudioTrack` em modo STREAM permite escrever a frase N+1
 *    antes de a N terminar, e é isso que dá continuidade.
 *
 * A taxa vem do CABEÇALHO de cada WAV (ver Wav.kt), nunca de constante.
 */
class Reprodutor {

    private val fila = LinkedBlockingQueue<Pcm>()
    @Volatile private var rodando = false
    @Volatile private var falandoAgora = false
    private var thread: Thread? = null
    private var track: AudioTrack? = null
    private var taxaAtual = 0

    /** Verdadeiro entre o começo da 1ª frase e o fim da última da fila.
     *  É o que faz o app PARAR de enviar PCM enquanto a IA fala (§4.2c). */
    val falando: Boolean get() = falandoAgora || fila.isNotEmpty()

    fun iniciar() {
        if (rodando) return
        rodando = true
        thread = Thread(::laco, "reprodutor").also { it.start() }
    }

    fun parar() {
        rodando = false
        thread?.interrupt()
        thread?.join(400)
        thread = null
        soltarTrack()
        fila.clear()
        falandoAgora = false
    }

    fun enfileirar(pcm: Pcm) {
        fila.offer(pcm)
    }

    /**
     * Barge-in: descarta o que ainda não tocou e cala a voz AGORA.
     *
     * ⚠ Quem chama por causa de um `barge_in` RECEBIDO não pode responder com um
     * `barge_in` enviado — o mesmo nome tem semântica oposta nos dois sentidos e
     * isso fecharia um laço cliente↔servidor (ws.py:328-332, index.html:884-885).
     */
    fun esvaziar() {
        fila.clear()
        try {
            track?.pause()
            track?.flush()
        } catch (e: IllegalStateException) {
            Log.w(TAG, "flush num track já solto", e)
        }
        falandoAgora = false
    }

    private fun laco() {
        while (rodando) {
            val pcm = try {
                fila.poll(200, TimeUnit.MILLISECONDS)
            } catch (e: InterruptedException) {
                break
            } ?: run { falandoAgora = false; null } ?: continue

            val t = garantirTrack(pcm.taxaHz) ?: continue
            falandoAgora = true
            try {
                if (t.playState != AudioTrack.PLAYSTATE_PLAYING) t.play()
                // `write` BLOQUEANTE de propósito: ele volta conforme o buffer
                // esvazia, e é o que permite escrever a frase seguinte
                // "colada" na anterior em vez de esperar o fim dela.
                var off = 0
                while (rodando && off < pcm.amostras.size) {
                    val n = t.write(pcm.amostras, off, pcm.amostras.size - off)
                    if (n <= 0) break
                    off += n
                }
            } catch (e: IllegalStateException) {
                Log.w(TAG, "track soltou no meio da escrita", e)
            }
        }
        falandoAgora = false
    }

    private fun garantirTrack(taxa: Int): AudioTrack? {
        // Recria só quando a TAXA muda — o que acontece se o dono trocar de
        // engine de TTS (Piper <-> XTTS) com o app aberto.
        if (track != null && taxa == taxaAtual) return track
        soltarTrack()
        val minimo = AudioTrack.getMinBufferSize(
            taxa, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minimo <= 0) {
            Log.e(TAG, "taxa $taxa não suportada (getMinBufferSize=$minimo)")
            return null
        }
        val t = try {
            AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build())
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(taxa)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build())
                // 4x o mínimo: o suficiente para atravessar um engasgo de rede
                // sem cortar a fala, e pouco o bastante para o barge-in ser
                // percebido como imediato.
                .setBufferSizeInBytes(minimo * 4)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setSessionId(AudioManager.AUDIO_SESSION_ID_GENERATE)
                .build()
        } catch (e: Exception) {
            Log.e(TAG, "não consegui criar o AudioTrack em ${taxa}Hz", e); return null
        }
        taxaAtual = taxa
        track = t
        Log.i(TAG, "AudioTrack em ${taxa}Hz (lida do cabeçalho do WAV)")
        return t
    }

    private fun soltarTrack() {
        try {
            track?.pause(); track?.flush(); track?.stop()
        } catch (e: IllegalStateException) {
            // track já em estado terminal — nada a fazer.
        }
        track?.release()
        track = null
        taxaAtual = 0
    }

    private companion object {
        const val TAG = "Reprodutor"
    }
}
