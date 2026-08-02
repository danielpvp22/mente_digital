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
import com.mentedigital.app.ChatViewModel
import com.mentedigital.app.Saude
import kotlinx.coroutines.launch

/**
 * Endereço + token, com um botão que TESTA antes de salvar.
 *
 * O teste bate em `/api/health`, a única rota sem gate (main.py:254-266) — é o
 * que separa "servidor inalcançável" de "servidor recusou o token". Sem essa
 * distinção o app só teria o close 1008 do WebSocket, que é idêntico para token
 * errado, Origin divergente e aparelho não autorizado (Risco R5 do plano).
 */
@Composable
fun TelaConfig(vm: ChatViewModel, aoConcluir: () -> Unit) {
    var base by remember { mutableStateOf(vm.ajustes.base.ifEmpty { "http://192.168.0.10:8000" }) }
    var token by remember { mutableStateOf(vm.ajustes.token) }
    var testando by remember { mutableStateOf(false) }
    var resultado by remember { mutableStateOf<Saude?>(null) }
    val escopo = rememberCoroutineScope()

    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)
            .verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Spacer(Modifier.height(24.dp))
        Text("Mente Digital", fontSize = 26.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onBackground)
        Text("Aponte o app para o servidor de casa. Nenhum modelo roda no telefone.",
            color = Texto2, fontSize = 14.sp)

        OutlinedTextField(
            value = base, onValueChange = { base = it; resultado = null },
            label = { Text("Endereço do servidor") },
            placeholder = { Text("192.168.0.10:8000") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = token, onValueChange = { token = it; resultado = null },
            label = { Text("Token de acesso") },
            visualTransformation = PasswordVisualTransformation(),
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )

        if (!vm.ajustes.criptografado) {
            // Fail-soft do Keystore, dito em voz alta: guardar o segredo em claro
            // sem avisar seria pior do que o app não abrir.
            Text(
                "⚠ O cofre do aparelho não está disponível. O token será guardado " +
                    "sem criptografia nesta instalação.",
                color = Rosa, fontSize = 13.sp,
            )
        }

        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(
                onClick = {
                    testando = true
                    escopo.launch {
                        resultado = vm.testar(base)
                        testando = false
                    }
                },
                enabled = !testando && base.isNotBlank(),
            ) { Text(if (testando) "Testando…" else "Testar conexão") }

            OutlinedButton(
                onClick = {
                    vm.ajustes.base = base
                    vm.ajustes.token = token
                    aoConcluir()
                },
                enabled = base.isNotBlank(),
            ) { Text("Salvar e entrar") }
        }

        resultado?.let { r -> ResultadoDoTeste(r) }
    }
}

@Composable
private fun ResultadoDoTeste(r: Saude) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (!r.alcancavel) {
                Text("Servidor inalcançável", color = Rosa, fontWeight = FontWeight.Medium)
                Text(
                    "Verifique se o Mente Digital está aberto no PC, se o endereço está " +
                        "certo e se o celular está na mesma rede." +
                        if (r.detalhe.isNotEmpty()) "\n(${r.detalhe})" else "",
                    color = Texto2, fontSize = 13.sp,
                )
                return@Column
            }
            Text("Servidor respondeu", color = Acento, fontWeight = FontWeight.Medium)
            // O mapa de serviços é o MESMO que a tela de boot do desktop lê
            // (app.py:_prontos_remotos). Mostrar quem não subiu evita o dono
            // culpar o app por uma função que o servidor não carregou.
            r.servicos.forEach { (nome, pronto) ->
                Row(verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Box(Modifier.size(8.dp).clip(CircleShape)
                        .background(if (pronto) Acento else Texto2))
                    Text(nome, color = if (pronto) MaterialTheme.colorScheme.onSurface else Texto2,
                        fontSize = 13.sp)
                }
            }
            Text(
                "O token não é checado aqui — esta rota não tem gate. Se o chat " +
                    "recusar a conexão depois, o token é o suspeito.",
                color = Texto2, fontSize = 12.sp,
            )
        }
    }
}
