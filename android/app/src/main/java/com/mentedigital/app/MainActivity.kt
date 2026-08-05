package com.mentedigital.app

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.SystemClock
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material3.Surface
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
import com.mentedigital.app.ui.AvisoDaCasca
import com.mentedigital.app.ui.FolhaUtilidades
import com.mentedigital.app.ui.TelaBoot
import com.mentedigital.app.ui.TelaConfig
import com.mentedigital.app.ui.TelaDormindo
import com.mentedigital.app.ui.TelaWeb
import com.mentedigital.app.ui.TemaMenteDigital
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private enum class Tela { CONFIG, BOOT, APP, DORMINDO }

/**
 * O app Android: um CLONE do `app.py`.
 *
 * A mesma anatomia, na mesma ordem: uma tela de boot nativa que mostra progresso
 * REAL enquanto os modelos sobem, e depois a MESMA SPA de sempre num WebView.
 * Nenhuma interface própria — foi a correção de rumo desta sessão.
 *
 * O que o celular acrescenta ao desktop: ele ACORDA e DEIXA DORMIR o PC. O
 * servidor fica de plantão (modelos soltos, máquina livre); ao abrir aqui, o app
 * manda `/api/energia {ligar}` e a espera acontece na tela de boot, com o mesmo
 * anel e os mesmos pontinhos. É o "watcher" pedido pelo dono, pelo avesso: em vez
 * de o PC vigiar a rede, o celular avisa — sem porta extra, sem descoberta e sem
 * processo vigiando. (O outro lado do watcher, o que faz o PC dormir SOZINHO
 * depois de 20 min, mora no servidor: `mente_digital/standby.py`.)
 *
 * As quatro telas: configuração (só na primeira vez ou a pedido), boot,
 * a SPA, e o repouso. Utilidades de casca ficam na folha do botão VOLTAR — ver
 * `FolhaUtilidades`.
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
                    var vigiaRespondeu by remember { mutableStateOf(false) }
                    // Estado da CASCA (não da conversa — essa é toda da SPA).
                    var folhaAberta by remember { mutableStateOf(false) }
                    var consolidando by remember { mutableStateOf(false) }
                    var ocupado by remember { mutableStateOf<String?>(null) }
                    var aviso by remember { mutableStateOf<String?>(null) }
                    var medidaRepouso by remember { mutableStateOf("") }
                    val escopo = rememberCoroutineScope()

                    // A permissão de microfone é pedida no PRIMEIRO uso da voz, não
                    // na abertura: pedir antes de a pessoa querer falar é o padrão
                    // que faz todo mundo negar. Quem dispara é a SPA, pela ponte —
                    // então pedimos assim que o app entra, mas sem bloquear nada.
                    val pedirMic = rememberLauncherForActivityResult(
                        ActivityResultContracts.RequestPermission()
                    ) { }

                    // O aviso da casca some sozinho — é um toast, não um estado.
                    LaunchedEffect(aviso) {
                        if (aviso != null) { delay(3200); aviso = null }
                    }

                    // ---- o RELÓGIO da tela de boot --------------------------
                    // Em coroutine PRÓPRIA, de propósito. O laço de sondagem
                    // abaixo passa a maior parte do tempo BLOQUEADO na rede, e um
                    // contador que só anda entre as chamadas dele congela na tela
                    // — foi o "7s" parado por dois minutos no Redmi, que se lê
                    // como app travado. Aqui o número anda sempre, mesmo quando a
                    // notícia não vem.
                    //
                    // ⚠ `elapsedRealtime` e não `currentTimeMillis`: o relógio de
                    // parede pode SALTAR (NTP, fuso, ajuste do dono) e um salto
                    // para trás no meio do boot poria tempo negativo no visor.
                    LaunchedEffect(tela) {
                        if (tela != Tela.BOOT) return@LaunchedEffect
                        val inicio = SystemClock.elapsedRealtime()
                        segundos = 0
                        while (true) {
                            segundos = Boot.segundosDecorridos(
                                inicio, SystemClock.elapsedRealtime())
                            delay(TIQUE_RELOGIO_MS)
                        }
                    }

                    // ---- o laço de boot -------------------------------------
                    LaunchedEffect(tela) {
                        if (tela != Tela.BOOT) return@LaunchedEffect
                        forcou = false; vigiaRespondeu = false
                        var acordou = false
                        var pediuAoVigia = false
                        while (true) {
                            val s = withContext(Dispatchers.IO) { servidor.saude() }
                            saude = s

                            // O VIGIA, quando o assistente nem existe. Depois de 45 min
                            // sem uso o `app.py` se ENCERRA e o PC volta a zero — aí não
                            // há `/api/health` para perguntar, e sem esta pergunta o app
                            // ficaria em "procurando o servidor…" para sempre, como o dono
                            // viu em 2026-08-02. O vigia é um processo de dezenas de MB que
                            // atende, valida o TOKEN e levanta o assistente.
                            if (!s.alcancavel && !pediuAoVigia) {
                                val v = withContext(Dispatchers.IO) { servidor.vigiaStatus() }
                                // Quem atendeu compra tempo: o vigia responder
                                // prova que a máquina está na rede e o endereço
                                // está certo — então o silêncio do `/api/health`
                                // é o assistente subindo, não erro de config.
                                if (v != null) vigiaRespondeu = true
                                if (v != null && !v.servidorDePe && !v.subindo) {
                                    pediuAoVigia = true
                                    when (withContext(Dispatchers.IO) { servidor.vigiaAcordar() }) {
                                        // Token errado é problema de CREDENCIAL e se resolve
                                        // configurando — mandar esperar seria mentir. É a
                                        // "tela de autenticação" do fluxo pedido pelo dono.
                                        PedidoVigia.RECUSADO -> {
                                            aviso = "O PC recusou o acesso — confira o token."
                                            tela = Tela.CONFIG
                                            break
                                        }
                                        PedidoVigia.ACEITO -> aviso = "Acordando o PC…"
                                        PedidoVigia.FALHOU -> pediuAoVigia = false
                                    }
                                } else if (v != null && v.subindo) {
                                    pediuAoVigia = true      // já está subindo: só esperar
                                }
                            }
                            // ACORDA O PC uma única vez, assim que ele responde e se
                            // revela em repouso.
                            //
                            // ⚠ `launch` e NÃO `withContext`: o `ligar` carrega LLM,
                            // Whisper, voz e embeddings, e só responde uns 30 s
                            // depois. Esperado aqui dentro, ele CONGELAVA o laço —
                            // a barra e o contador de segundos ficavam parados no
                            // mesmo número durante todo o religamento, que é
                            // indistinguível de app travado. A tela de progresso
                            // existe justamente para essa espera não parecer isso.
                            if (!acordou && s.descansando) {
                                acordou = true
                                launch(Dispatchers.IO) { servidor.ligar() }
                            }
                            val (_, tudoPronto) = Boot.progresso(Boot.marcosDe(s))
                            if (tudoPronto || forcou) { tela = Tela.APP; break }
                            // Cadência da SONDAGEM, não do visor — quem conta o
                            // tempo é o relógio acima, e ele não depende disto.
                            delay(700)
                        }
                    }

                    // ---- as ações da folha de utilidades --------------------
                    fun economizar() {
                        escopo.launch {
                            ocupado = "liberando…"
                            val r = withContext(Dispatchers.IO) { servidor.desligar() }
                            ocupado = null
                            folhaAberta = false
                            when {
                                r == null -> aviso = "Não consegui liberar o PC."
                                // O servidor cedeu a vez a uma resposta em andamento
                                // (alguém está usando o PC agora). Ir para a tela de
                                // repouso aqui seria mentira: o PC não dormiu.
                                r.adiado -> aviso = "O PC está respondendo agora — tente de novo em instantes."
                                else -> {
                                    medidaRepouso = r.resumo()
                                    tela = Tela.DORMINDO
                                }
                            }
                        }
                    }

                    fun consolidar() {
                        escopo.launch {
                            val alvo = if (consolidando) "parar" else "rodar"
                            ocupado = if (consolidando) "parando…" else "disparando…"
                            val ok = withContext(Dispatchers.IO) { servidor.idle(alvo) }
                            ocupado = null
                            folhaAberta = false
                            if (ok) {
                                consolidando = !consolidando
                                aviso = if (consolidando) "Consolidando em segundo plano."
                                        else "Consolidação interrompida."
                            } else {
                                aviso = "O servidor não aceitou o comando."
                            }
                        }
                    }

                    Box(Modifier.fillMaxSize()) {
                        when (tela) {
                            Tela.CONFIG -> TelaConfig(ajustes, servidor) { tela = Tela.BOOT }

                            Tela.BOOT -> TelaBoot(
                                saude = saude,
                                segundos = segundos,           // segundos de PAREDE
                                oferta = Boot.oferta(
                                    saude.alcancavel, vigiaRespondeu, segundos),
                                aoForcarEntrada = { forcou = true },
                                aoConfigurar = { tela = Tela.CONFIG },
                            )

                            Tela.DORMINDO -> {
                                // Voltar daqui é sair: não há para onde ir, e reabrir o
                                // app custa um toque (o servidor continua de pé).
                                BackHandler(enabled = true) { finish() }
                                TelaDormindo(
                                    medida = medidaRepouso,
                                    religando = ocupado != null,
                                    aoAcordar = { tela = Tela.BOOT },
                                )
                            }

                            Tela.APP -> {
                                val alvo = remember {
                                    val base = ajustes.base
                                    if (ajustes.token.isBlank()) base
                                    else "$base/?token=${ajustes.token}"
                                }
                                // Voltar navega DENTRO da SPA (fechar o painel, sair da
                                // nota) — o mesmo que o Esc faz no desktop. Quando não
                                // há mais para onde voltar, em vez de matar o app em
                                // silêncio, abre as utilidades da casca: é onde o
                                // "modo economia" precisa estar, porque quem está
                                // saindo é exatamente quem quer o PC de volta.
                                BackHandler(enabled = !folhaAberta) {
                                    val w = web
                                    if (w != null && w.canGoBack()) w.goBack() else folhaAberta = true
                                }
                                TelaWeb(
                                    url = alvo,
                                    ponte = ponte!!,
                                    aoCriar = { w -> web = w },
                                    // A ponte do microfone guarda um `() -> WebView?`;
                                    // deixá-lo apontando para um WebView já destruído
                                    // faria o próximo quadro de áudio chamar
                                    // `evaluateJavascript` num objeto morto.
                                    aoLiberar = { w -> if (web === w) web = null },
                                )
                                LaunchedEffect(Unit) {
                                    if (ContextCompat.checkSelfPermission(
                                            this@MainActivity, Manifest.permission.RECORD_AUDIO
                                        ) != PackageManager.PERMISSION_GRANTED
                                    ) pedirMic.launch(Manifest.permission.RECORD_AUDIO)
                                }
                                if (folhaAberta) {
                                    FolhaUtilidades(
                                        consolidando = consolidando,
                                        ocupado = ocupado,
                                        aoFechar = { folhaAberta = false },
                                        aoEconomizar = ::economizar,
                                        aoConsolidar = ::consolidar,
                                        aoRecarregar = {
                                            web?.reload(); folhaAberta = false
                                            aviso = "Recarregando a interface."
                                        },
                                        aoConfigurar = { folhaAberta = false; tela = Tela.CONFIG },
                                        aoSair = { finish() },
                                    )
                                }
                            }
                        }
                        aviso?.let {
                            Box(Modifier.fillMaxSize().statusBarsPadding()) { AvisoDaCasca(it) }
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
        /** De quanto em quanto o relógio da tela de boot se atualiza. Menor que
         *  um segundo para o visor nunca "pular" um número; o estado só muda
         *  quando o inteiro muda, então isto não recompõe a tela à toa. */
        const val TIQUE_RELOGIO_MS = 200L
    }
}
