package com.mentedigital.app

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
import com.mentedigital.app.ui.TelaBoot
import com.mentedigital.app.ui.TelaConfig
import com.mentedigital.app.ui.TelaWeb
import com.mentedigital.app.ui.TemaMenteDigital
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

private enum class Tela { CONFIG, BOOT, APP }

/**
 * O app Android: um CLONE do `app.py`.
 *
 * A mesma anatomia, na mesma ordem: uma tela de boot nativa que mostra progresso
 * REAL enquanto os modelos sobem, e depois a MESMA SPA de sempre num WebView.
 * Nenhuma interface própria — foi a correção de rumo desta sessão.
 *
 * O que o celular acrescenta ao desktop: ele ACORDA o PC. O `app.py` fica aberto
 * em standby (modelos soltos, ~1,6 GB de VRAM); ao abrir aqui, o app manda
 * `/api/energia {ligar}` e a espera acontece na tela de boot, com o mesmo anel e
 * os mesmos pontinhos. É o "watcher" pedido pelo dono, pelo avesso: em vez de o
 * PC vigiar a rede esperando o celular, o celular avisa — sem porta extra, sem
 * descoberta e sem processo vigiando.
 */
class MainActivity : ComponentActivity() {

    private var web: WebView? = null
    private var ponte: PonteAndroid? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ajustes = Ajustes(this)
        val servidor = Servidor { Conf(ajustes.base, ajustes.token) }
        ponte = PonteAndroid(this) { web }

        setContent {
            TemaMenteDigital {
                Surface(Modifier.fillMaxSize()) {
                    var tela by remember {
                        mutableStateOf(if (ajustes.configurado) Tela.BOOT else Tela.CONFIG)
                    }
                    var saude by remember { mutableStateOf(Saude(false)) }
                    var segundos by remember { mutableIntStateOf(0) }
                    var forcou by remember { mutableStateOf(false) }

                    // A permissão de microfone é pedida no PRIMEIRO uso da voz, não
                    // na abertura: pedir antes de a pessoa querer falar é o padrão
                    // que faz todo mundo negar. Quem dispara é a SPA, pela ponte —
                    // então pedimos assim que o app entra, mas sem bloquear nada.
                    val pedirMic = rememberLauncherForActivityResult(
                        ActivityResultContracts.RequestPermission()
                    ) { }

                    // ---- o laço de boot -------------------------------------
                    LaunchedEffect(tela) {
                        if (tela != Tela.BOOT) return@LaunchedEffect
                        segundos = 0; forcou = false
                        var acordou = false
                        while (true) {
                            val s = withContext(Dispatchers.IO) { servidor.saude() }
                            saude = s
                            // ACORDA O PC uma única vez, assim que ele responde e se
                            // revela em standby. Idempotente do lado do servidor,
                            // mas repetir seria pedir recarga em cima de recarga.
                            if (!acordou && s.descansando) {
                                acordou = true
                                withContext(Dispatchers.IO) { servidor.ligar() }
                            }
                            val (_, tudoPronto) = Boot.progresso(Boot.marcosDe(s))
                            if (tudoPronto || forcou) { tela = Tela.APP; break }
                            delay(700)
                            segundos += 1
                        }
                    }

                    when (tela) {
                        Tela.CONFIG -> TelaConfig(ajustes, servidor) { tela = Tela.BOOT }

                        Tela.BOOT -> TelaBoot(
                            saude = saude,
                            segundos = segundos * 7 / 10,          // tiques de 700 ms
                            // Rede de segurança da degradação graciosa: um serviço
                            // pode falhar no load e ficar `ready=False` PARA SEMPRE
                            // (cada `load` do servidor tem pára-quedas próprio).
                            // Sem este teto, "espera tudo" viraria deadlock.
                            escapeOferecido = segundos * 7 / 10 >= SEGUNDOS_ATE_OFERECER_SAIDA,
                            aoForcarEntrada = { forcou = true },
                        )

                        Tela.APP -> {
                            val alvo = remember {
                                val base = ajustes.base
                                if (ajustes.token.isBlank()) base
                                else "$base/?token=${ajustes.token}"
                            }
                            // Voltar navega DENTRO da SPA (fechar o painel, sair da
                            // nota) antes de sair do app — o mesmo que o Esc faz no
                            // desktop.
                            BackHandler(enabled = true) {
                                val w = web
                                if (w != null && w.canGoBack()) w.goBack() else finish()
                            }
                            TelaWeb(alvo, ponte!!) { w -> web = w }
                            LaunchedEffect(Unit) {
                                if (ContextCompat.checkSelfPermission(
                                        this@MainActivity, Manifest.permission.RECORD_AUDIO
                                    ) != PackageManager.PERMISSION_GRANTED
                                ) pedirMic.launch(Manifest.permission.RECORD_AUDIO)
                            }
                        }
                    }
                }
            }
        }
    }

    override fun onDestroy() {
        ponte?.encerrar()
        web?.destroy()
        web = null
        super.onDestroy()
    }

    private companion object {
        /** Igual ao `app.py`: depois disto a tela oferece entrar assim mesmo e DIZ
         *  quem não subiu — não entra escondido. */
        const val SEGUNDOS_ATE_OFERECER_SAIDA = 150
    }
}
