package com.mentedigital.app

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.mentedigital.app.ui.TelaChat
import com.mentedigital.app.ui.TelaConfig
import com.mentedigital.app.ui.TelaConversas
import com.mentedigital.app.ui.TemaMenteDigital

private enum class Tela { CONFIG, CHAT, CONVERSAS }

/**
 * Fase 1 do app Android (docs/PLANO_APP_ANDROID.md): chat por texto, ponta a
 * ponta, sobre o MESMO WebSocket que a web usa. Nenhum modelo no telefone.
 *
 * Sem biblioteca de navegação: são três telas e uma variável de estado. Trazer
 * `androidx.navigation` para isto seria uma dependência a mais para resolver um
 * problema que ainda não existe.
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            TemaMenteDigital {
                Surface(Modifier.fillMaxSize()) {
                    val vm: ChatViewModel = viewModel()
                    var tela by remember {
                        mutableStateOf(if (vm.ajustes.configurado) Tela.CHAT else Tela.CONFIG)
                    }

                    // Conecta ao entrar no chat e a cada volta da configuração —
                    // o endereço/token podem ter mudado.
                    LaunchedEffect(tela) {
                        if (tela == Tela.CHAT) vm.conectar()
                    }

                    // A permissão de microfone é pedida no PRIMEIRO toque no
                    // botão de voz, não na abertura do app: pedir antes de a
                    // pessoa querer falar é o padrão que faz todo mundo negar.
                    val pedirMic = rememberLauncherForActivityResult(
                        ActivityResultContracts.RequestPermission()
                    ) { concedida -> if (concedida) vm.iniciarVoz() }

                    val alternarVoz = {
                        if (vm.modoVoz) {
                            vm.pararVoz()
                        } else if (ContextCompat.checkSelfPermission(
                                this, Manifest.permission.RECORD_AUDIO
                            ) == PackageManager.PERMISSION_GRANTED
                        ) {
                            vm.iniciarVoz()
                        } else {
                            pedirMic.launch(Manifest.permission.RECORD_AUDIO)
                        }
                        Unit
                    }

                    when (tela) {
                        Tela.CONFIG -> TelaConfig(vm) { tela = Tela.CHAT }
                        Tela.CHAT -> TelaChat(
                            vm,
                            aoAbrirConversas = { tela = Tela.CONVERSAS },
                            aoAbrirConfig = { tela = Tela.CONFIG },
                            aoAlternarVoz = alternarVoz,
                        )
                        Tela.CONVERSAS -> TelaConversas(vm) { tela = Tela.CHAT }
                    }
                }
            }
        }
    }
}
