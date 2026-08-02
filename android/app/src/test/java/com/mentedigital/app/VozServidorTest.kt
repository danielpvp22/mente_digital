package com.mentedigital.app

import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * O caminho de VOZ, ponta a ponta, SEM microfone.
 *
 * O emulador do Android não tem microfone de verdade e o aparelho do dono não
 * está aqui — então apertar o botão de voz prova que o `AudioRecord` abre, e
 * nada além disso. Este teste fecha o resto da fresta: pega uma fala
 * sintetizada pelo PRÓPRIO servidor (`scripts/gerar_fala_teste.py`) e a injeta
 * no WebSocket em quadros de 1024 amostras, byte a byte como o `Gravador`
 * faria.
 *
 * O que ele prova, e que nenhum teste de unidade alcança:
 *
 * - o quadro binário chega no formato que `np.frombuffer(raw, dtype=np.int16)`
 *   lê (ws.py:312) — endianness, largura de amostra e canal certos;
 * - 1024 amostras por mensagem satisfazem `vad_min_frames` (que conta MENSAGENS,
 *   não tempo — ws.py:379, 393-394);
 * - o VAD do servidor de fato ABRE e FECHA o turno com esse áudio;
 * - a transcrição volta, e com o texto certo.
 *
 * Pula sozinho sem `MENTE_BASE` no ambiente ou sem o WAV gerado.
 */
class VozServidorTest {

    private val base: String? = System.getenv("MENTE_BASE")
    private val token: String = System.getenv("MENTE_TOKEN") ?: ""

    private fun falaDeTeste(): Pcm? {
        val fluxo = javaClass.classLoader?.getResourceAsStream("fala_16k.wav") ?: return null
        return Wav.ler(fluxo.use { it.readBytes() })
    }

    @Test
    fun `fala sintetizada vira transcricao no servidor`() {
        assumeTrue("sem MENTE_BASE — teste de integração pulado", !base.isNullOrBlank())
        val fala = falaDeTeste()
        assumeTrue("rode `python scripts/gerar_fala_teste.py` para gerar o áudio", fala != null)
        assertTrue("a amostra tem de estar em 16 kHz", fala!!.taxaHz == Gravador.TAXA)

        val recebidas = java.util.Collections.synchronizedList(ArrayList<MensagemServidor>())
        val abriu = CountDownLatch(1)
        val transcreveu = CountDownLatch(1)

        val cliente = ClienteMente(
            conf = Conf(base!!, token),
            conversaId = { "teste-voz-" + UUID.randomUUID() },
            aoReceber = { m ->
                recebidas += m
                if (m is MensagemServidor.Transcricao) transcreveu.countDown()
            },
            aoMudarConexao = { c, _ -> if (c == Conexao.ABERTO) abriu.countDown() },
        )
        try {
            cliente.conectar()
            assertTrue("não abriu o WebSocket em 15 s", abriu.await(15, TimeUnit.SECONDS))

            // ⚠ EXATAMENTE como o Gravador: 1024 amostras por mensagem, e uma
            // pausa proporcional à duração real do quadro (64 ms). Despejar tudo
            // de uma vez faria o servidor receber 3,6 s de fala em milissegundos
            // — o VAD dele mede silêncio em TEMPO DE PAREDE (ws.py:381-391) e
            // fecharia o turno na hora errada.
            var i = 0
            while (i < fala.amostras.size) {
                val n = minOf(Gravador.AMOSTRAS_QUADRO, fala.amostras.size - i)
                val quadro = ShortArray(Gravador.AMOSTRAS_QUADRO)
                fala.amostras.copyInto(quadro, 0, i, i + n)
                assertTrue("quadro recusado — socket caiu?",
                    cliente.enviarAudio(Gravador.paraBytes(quadro, Gravador.AMOSTRAS_QUADRO)))
                i += n
                Thread.sleep(64)
            }

            assertTrue("o servidor não transcreveu em 90 s; recebidos=" +
                recebidas.map { it::class.simpleName },
                transcreveu.await(90, TimeUnit.SECONDS))

            val t = recebidas.filterIsInstance<MensagemServidor.Transcricao>().first()
            assertNotNull(t)
            assertTrue("transcrição vazia", t.texto.isNotBlank())
            // Não se exige a frase EXATA: o Whisper acerta o conteúdo mas varia
            // pontuação e acento. Cobrar igualdade tornaria o teste instável por
            // motivo que não é defeito.
            val normal = t.texto.lowercase()
            assertTrue("transcrição não parece com a fala enviada: '${t.texto}'",
                normal.contains("dois") || normal.contains("quanto"))
        } finally {
            cliente.desconectar(avisarFimDeSessao = false)
        }
    }
}
