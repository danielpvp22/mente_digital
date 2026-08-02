package com.mentedigital.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mentedigital.app.ChatViewModel

/**
 * Histórico agrupado em CONVERSAS (não turnos soltos) — `/api/conversas`,
 * main.py:502. Abrir uma manda `carregar_conversa` no WebSocket (que recarrega a
 * RAM do servidor) E busca os turnos pela REST (que é quem tem o texto).
 */
@Composable
fun TelaConversas(vm: ChatViewModel, aoVoltar: () -> Unit) {
    LaunchedEffect(Unit) { vm.carregarConversas() }

    Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        Row(
            Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 12.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            TextButton(onClick = aoVoltar) { Text("Voltar") }
            Spacer(Modifier.weight(1f))
            Text("Conversas", color = MaterialTheme.colorScheme.onSurface,
                fontWeight = FontWeight.Medium, modifier = Modifier.padding(top = 12.dp))
            Spacer(Modifier.weight(1f))
        }
        if (vm.conversas.isEmpty()) {
            Text("Nada por aqui ainda — ou o servidor não respondeu.",
                color = Texto2, fontSize = 14.sp, modifier = Modifier.padding(24.dp))
            return@Column
        }
        LazyColumn(
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(vm.conversas, key = { it.id }) { c ->
                Column(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .clickable { vm.abrirConversa(c.id); aoVoltar() }
                        .padding(14.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    Text(c.titulo.ifEmpty { "(sem título)" },
                        color = MaterialTheme.colorScheme.onSurface, fontSize = 15.sp,
                        maxLines = 2)
                    if (c.quando.isNotEmpty()) {
                        Text(c.quando, color = Texto2, fontSize = 12.sp)
                    }
                }
            }
        }
    }
}
