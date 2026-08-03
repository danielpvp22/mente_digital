package com.mentedigital.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mentedigital.app.Ajustes
import com.mentedigital.app.Saude
import com.mentedigital.app.Servidor
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Endereço + token, com um botão que TESTA antes de salvar.
 *
 * O teste bate em `/api/health`, a única rota sem gate (main.py:254-266) — é o
 * que separa "servidor inalcançável" de "servidor recusou o token". Sem essa
 * distinção o app só teria o 403 do handshake do WebSocket, que é idêntico para
 * token errado, Origin divergente e aparelho não autorizado (Risco R5).
 */
@Composable
fun TelaConfig(ajustes: Ajustes, servidor: Servidor, aoConcluir: () -> Unit) {
    var base by remember { mutableStateOf(ajustes.base.ifEmpty { "http://192.168.15.13:8000" }) }
    var token by remember { mutableStateOf(ajustes.token) }
    var testando by remember { mutableStateOf(false) }
    var resultado by remember { mutableStateOf<Saude?>(null) }
    val escopo = rememberCoroutineScope()

    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)
            .statusBarsPadding().navigationBarsPadding().imePadding()
            .verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Spacer(Modifier.height(20.dp))
        Wordmark(tamanho = 16)
        Text("Aponte o app para o servidor de casa. Nenhum modelo roda no telefone.",
            color = Texto2, fontSize = 14.sp)

        OutlinedTextField(
            value = base, onValueChange = { base = it; resultado = null },
            label = { Text("Endereço do servidor") },
            placeholder = { Text("192.168.0.10:8000") },
            // A dica existe porque é o erro nº 1 de quem testa no emulador: lá
            // `localhost` é o PRÓPRIO Android, não o PC.
            supportingText = { Text("No emulador use 10.0.2.2 — é como ele enxerga o PC.") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = token, onValueChange = { token = it; resultado = null },
            label = { Text("Token de acesso") },
            visualTransformation = PasswordVisualTransformation(),
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )

        if (!ajustes.criptografado) {
            // Fail-soft do Keystore, dito em voz alta: guardar o segredo em claro
            // sem avisar seria pior do que o app não abrir.
            Text("⚠ O cofre do aparelho não está disponível. O token será guardado " +
                "sem criptografia nesta instalação.", color = Rosa, fontSize = 13.sp)
        }

        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(
                onClick = {
                    testando = true
                    escopo.launch {
                        resultado = withContext(Dispatchers.IO) { servidor.saude(base) }
                        testando = false
                    }
                },
                enabled = !testando && base.isNotBlank(), shape = CircleShape,
            ) { Text(if (testando) "Testando…" else "Testar conexão") }

            OutlinedButton(
                onClick = {
                    ajustes.base = base
                    ajustes.token = token
                    aoConcluir()
                },
                enabled = base.isNotBlank(), shape = CircleShape,
            ) { Text("Salvar e entrar") }
        }

        resultado?.let { ResultadoDoTeste(it) }
    }
}

@Composable
private fun ResultadoDoTeste(r: Saude) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (!r.alcancavel) {
                Text("Servidor inalcançável", color = Rosa, fontWeight = FontWeight.Medium)
                Text("Verifique se o Mente Digital está aberto no PC, se o endereço está " +
                    "certo e se o celular está na mesma rede." +
                    if (r.detalhe.isNotEmpty()) "\n(${r.detalhe})" else "",
                    color = Texto2, fontSize = 13.sp)
                return@Column
            }
            Text("Servidor respondeu", color = Acento, fontWeight = FontWeight.Medium)
            if (r.descansando) {
                Text("Está em standby — o app acorda os modelos ao entrar.",
                    color = Texto2, fontSize = 12.sp)
            }
            // O mapa de serviços é o MESMO que a tela de boot do desktop lê.
            // Mostrar quem não subiu evita culpar o app por uma função que o
            // servidor não carregou.
            r.servicos.forEach { (nome, pronto) ->
                Row(verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Box(Modifier.size(8.dp).clip(CircleShape)
                        .background(if (pronto) Acento else Texto2))
                    Text(nome, fontSize = 13.sp,
                        color = if (pronto) MaterialTheme.colorScheme.onSurface else Texto2)
                }
            }
            Text("O token não é checado aqui — esta rota não tem gate. Se o app recusar " +
                "a conexão depois, o token é o suspeito.", color = Texto2, fontSize = 12.sp)
        }
    }
}
