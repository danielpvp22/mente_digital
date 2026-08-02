package com.mentedigital.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mentedigital.app.Bolha
import com.mentedigital.app.ChatViewModel
import com.mentedigital.app.Conexao

@Composable
fun TelaChat(vm: ChatViewModel, aoAbrirConversas: () -> Unit, aoAbrirConfig: () -> Unit) {
    var entrada by remember { mutableStateOf("") }
    val lista = rememberLazyListState()

    // Rola para o fim a cada token: sem isto o streaming acontece fora da tela.
    LaunchedEffect(vm.bolhas.size, vm.bolhas.lastOrNull()?.texto?.length) {
        if (vm.bolhas.isNotEmpty()) lista.animateScrollToItem(vm.bolhas.lastIndex)
    }

    Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        BarraTopo(vm, aoAbrirConversas, aoAbrirConfig)

        if (vm.status.isNotEmpty()) {
            Text(vm.status, color = Texto2, fontSize = 12.sp,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp))
        }

        LazyColumn(
            state = lista,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            if (vm.bolhas.isEmpty()) {
                item {
                    Text(
                        "Pergunte alguma coisa. O que responder vem do vault no seu PC.",
                        color = Texto2, fontSize = 14.sp,
                        modifier = Modifier.fillMaxWidth().padding(top = 48.dp),
                    )
                }
            }
            // `itemsIndexed` com chave estável pelo índice: o texto MUDA a cada
            // token, então usar o conteúdo como chave recriaria o item a cada
            // pedaço e a lista piscaria durante o streaming.
            itemsIndexed(vm.bolhas, key = { i, _ -> i }) { _, b -> BolhaView(b) }
        }

        Entrada(
            valor = entrada,
            aoMudar = { entrada = it },
            habilitado = vm.conexao == Conexao.ABERTO,
            aoEnviar = { vm.enviar(entrada); entrada = "" },
        )
    }
}

@Composable
private fun BarraTopo(vm: ChatViewModel, aoAbrirConversas: () -> Unit, aoAbrirConfig: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        TextButton(onClick = aoAbrirConversas) { Text("Conversas") }
        Spacer(Modifier.weight(1f))
        PontoDeConexao(vm.conexao)
        TextButton(onClick = { vm.novaConversa() }) { Text("Novo") }
        TextButton(onClick = aoAbrirConfig) { Text("Ajustes") }
    }
    // O estado da conexão é DITO, não escondido: um app que parece online e
    // engole a mensagem é pior que um que avisa que caiu.
    val aviso = when (vm.conexao) {
        Conexao.RECUSADO -> vm.detalheConexao.ifEmpty { "O servidor recusou o acesso." }
        Conexao.CAIU -> "Sem conexão. Tentando de novo…"
        Conexao.CONECTANDO -> "Conectando…"
        else -> ""
    }
    if (aviso.isNotEmpty()) {
        Text(aviso, color = if (vm.conexao == Conexao.RECUSADO) Rosa else Texto2,
            fontSize = 12.sp,
            modifier = Modifier.fillMaxWidth()
                .background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 16.dp, vertical = 4.dp))
    }
}

@Composable
private fun PontoDeConexao(c: Conexao) {
    val cor = when (c) {
        Conexao.ABERTO -> Acento
        Conexao.CONECTANDO -> Roxo
        Conexao.RECUSADO -> Rosa
        else -> Texto2
    }
    Box(Modifier.size(8.dp).clip(CircleShape).background(cor))
}

@Composable
private fun BolhaView(b: Bolha) {
    val alinhamento = if (b.doUsuario) Alignment.CenterEnd else Alignment.CenterStart
    val fundo = when {
        b.erro -> Rosa.copy(alpha = 0.16f)
        b.proativo -> Roxo.copy(alpha = 0.18f)
        b.doUsuario -> MaterialTheme.colorScheme.surfaceVariant
        else -> MaterialTheme.colorScheme.surface
    }
    Box(Modifier.fillMaxWidth(), contentAlignment = alinhamento) {
        Column(
            Modifier.widthIn(max = 320.dp).clip(RoundedCornerShape(16.dp))
                .background(fundo).padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            if (b.proativo) Text("🔔 aviso do servidor", color = Texto2, fontSize = 11.sp)
            Text(b.texto, color = MaterialTheme.colorScheme.onSurface, fontSize = 15.sp)
            if (b.fontes.isNotEmpty()) Fontes(b)
        }
    }
}

@Composable
private fun Fontes(b: Bolha) {
    // `itens` são strings COM PREFIXO ("memoria", "nota:x.md", "web:dominio"),
    // não objetos — e a `rota` é a cascata usada, com "+" entre as fontes.
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(if (b.rota.isNotEmpty()) "fontes · ${b.rota}" else "fontes",
            color = Texto2, fontSize = 11.sp, fontWeight = FontWeight.Medium)
        b.fontes.take(6).forEach { f ->
            Text("• " + f.substringAfter(':').substringAfterLast('/'),
                color = Texto2, fontSize = 11.sp)
        }
        if (b.fontes.size > 6) Text("• e mais ${b.fontes.size - 6}", color = Texto2, fontSize = 11.sp)
    }
}

@Composable
private fun Entrada(valor: String, aoMudar: (String) -> Unit, habilitado: Boolean, aoEnviar: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface)
            .padding(10.dp).navigationBarsPadding().imePadding(),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        OutlinedTextField(
            value = valor, onValueChange = aoMudar,
            modifier = Modifier.weight(1f),
            placeholder = { Text("Pergunte à Mente Digital") },
            maxLines = 5,
        )
        Button(onClick = aoEnviar, enabled = habilitado && valor.isNotBlank()) { Text("Enviar") }
    }
}
