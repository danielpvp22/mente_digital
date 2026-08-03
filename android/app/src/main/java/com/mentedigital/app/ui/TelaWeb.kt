package com.mentedigital.app.ui

import android.annotation.SuppressLint
import android.graphics.Color as CorAndroid
import android.view.ViewGroup
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView
import com.mentedigital.app.PonteAndroid

/**
 * A interface: a MESMA SPA que o navegador e a janela do PC servem.
 *
 * ⚠ Este arquivo NÃO desenha nada. Foi a lição da primeira tentativa: eu havia
 * recriado o chat em Compose, e ele nunca ficaria igual — porque era outra
 * coisa. O `templates/index.html` diz no próprio comentário que serve TRÊS
 * cascas (aba de navegador, janela nativa, container) e que é "a base do port
 * Android". Aqui a casca é isto: uma janela e uma ponte.
 *
 * O que o app ganha de graça por ser o mesmo arquivo: barra lateral de
 * conversas, chips de VRAM/RAM, "Consolidar", painel avançado com os filtros do
 * vault e o grafo da malha, palavra-mestre, tema claro/escuro, e tudo o que for
 * construído amanhã.
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun TelaWeb(
    url: String,
    ponte: PonteAndroid,
    aoCriar: (WebView) -> Unit,
    aoLiberar: (WebView) -> Unit = {},
) {
    AndroidView(
        modifier = Modifier.fillMaxSize(),
        // Sair desta tela (modo economia, voltar à configuração) tira o WebView da
        // composição. Sem destruí-lo aqui, cada ida e volta deixaria um WebView
        // vivo — com o processo de renderização e o socket da SPA junto. `onRelease`
        // roda com a view já desanexada, que é a hora segura de destruir.
        onRelease = { w -> aoLiberar(w); w.destroy() },
        factory = { ctx ->
            WebView(ctx).apply {
                layoutParams = ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
                // O fundo do app é #0B0B0D; o branco padrão do WebView pisca
                // durante o primeiro layout e estraga a transição da tela de boot.
                setBackgroundColor(CorAndroid.parseColor("#0B0B0D"))

                settings.apply {
                    javaScriptEnabled = true
                    // localStorage: é ONDE a SPA guarda o token depois da primeira
                    // abertura com `?token=` (index.html:806-808). Sem isto o app
                    // pediria o token a cada início.
                    domStorageEnabled = true
                    // A resposta falada toca sozinha, sem toque do usuário — o
                    // áudio chega por WebSocket, não por clique.
                    mediaPlaybackRequiresUserGesture = false
                    // A SPA é responsiva e já trata `viewport-fit=cover`; deixar o
                    // WebView "ajustar" a largura sozinho quebraria o layout dela.
                    useWideViewPort = false
                    loadWithOverviewMode = false
                    textZoom = 100
                }
                // `MenteAndroid`, e NÃO `pywebview`: o nome é o que a SPA usa para
                // saber o que esta casca sabe fazer. Com `pywebview` ela desenharia
                // a barra de título sem moldura do desktop, que aqui não faz sentido.
                addJavascriptInterface(ponte, "MenteAndroid")

                webViewClient = WebViewClient()
                webChromeClient = WebChromeClient()
                loadUrl(url)
                aoCriar(this)
            }
        },
    )
}
