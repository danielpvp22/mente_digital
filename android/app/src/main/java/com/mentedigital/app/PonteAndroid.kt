package com.mentedigital.app

import android.content.Context
import android.util.Base64
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.WebView

/**
 * A ponte que a SPA usa para gravar no Android — exposta como `window.MenteAndroid`.
 *
 * POR QUE ELA EXISTE: `getUserMedia` só funciona em contexto seguro, e o servidor
 * sobe em HTTP (config.py: `ssl_cert=""`). A própria página já barra o botão de
 * voz por isso (index.html:1025). No app NATIVO essa restrição não vale: quem
 * grava é o `AudioRecord`, que só depende da permissão RECORD_AUDIO.
 *
 * ⚠ O DESENHO IMPORTA: a ponte entrega só o QUADRO CRU. RMS, barge-in, mudo e
 * envio continuam sendo o mesmo JavaScript que o navegador e a janela do PC
 * rodam (`processarQuadroMic`). Reimplementar isso em Kotlin daria duas noções
 * de "voz alta" com o mesmo nome, envelhecendo separadas — que é exatamente o
 * erro que este app inteiro foi refeito para não cometer.
 *
 * É o mesmo padrão do `window.pywebview` que a barra de título já usa: uma
 * casca, uma capacidade; a lógica é de todo mundo.
 */
class PonteAndroid(
    private val contexto: Context,
    private val web: () -> WebView?,
) {
    private val gravador = Gravador { quadro, n -> empurrar(quadro, n) }

    /** Chamado pelo JS. Devolve se o microfone abriu. */
    @JavascriptInterface
    fun iniciarMicrofone(): Boolean {
        val ok = gravador.iniciar()
        // O serviço em primeiro plano é o que impede o Android de suspender o
        // processo com a tela apagada (Risco R1). Só sobe se o microfone abriu:
        // uma notificação "está ouvindo" sem estar ouvindo seria mentira.
        if (ok) ServicoVoz.ligar(contexto)
        Log.i(TAG, if (ok) "microfone aberto pela SPA" else "microfone RECUSADO")
        return ok
    }

    @JavascriptInterface
    fun pararMicrofone() {
        gravador.parar()
        ServicoVoz.desligar(contexto)
        Log.i(TAG, "microfone fechado pela SPA")
    }

    /** O app é nativo — a SPA pergunta para decidir o que desenhar. */
    @JavascriptInterface
    fun info(): String = """{"nativo":true,"plataforma":"android"}"""

    fun encerrar() {
        gravador.parar()
        ServicoVoz.desligar(contexto)
    }

    /**
     * Um quadro de 1024 amostras para dentro da página.
     *
     * Roda na thread do gravador; `evaluateJavascript` exige a thread da UI, daí
     * o `post`. Base64 e não binário porque a fronteira JS aceita string — o
     * custo é ~2,7 KB e 15,6 chamadas por segundo, que é ruído perto do que um
     * WebView já faz por quadro de tela.
     *
     * ⚠ UM quadro por chamada, e não um lote: `vad_min_frames` no servidor conta
     * MENSAGENS do WebSocket (ws.py:379, 393-394), e a página faz um `ws.send`
     * por chamada. Agrupar aqui mudaria o VAD do servidor sem nada falhar.
     */
    private fun empurrar(quadro: ShortArray, n: Int) {
        val b64 = Base64.encodeToString(Gravador.paraBytes(quadro, n), Base64.NO_WRAP)
        val w = web() ?: return
        w.post {
            try {
                w.evaluateJavascript("window.__micAndroid&&window.__micAndroid('$b64')", null)
            } catch (e: Exception) {
                Log.w(TAG, "não consegui entregar o quadro à página", e)
            }
        }
    }

    private companion object {
        const val TAG = "PonteAndroid"
    }
}
