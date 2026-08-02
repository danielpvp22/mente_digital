package com.mentedigital.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * As utilidades da CASCA — o menu da bandeja do PC, num telefone que não tem
 * bandeja.
 *
 * ⚠ O QUE NÃO ESTÁ AQUI, E POR QUÊ: o chat, o histórico, o modo avançado, o tema
 * e o próprio botão de energia da barra de status continuam sendo da SPA. Esta
 * folha só tem o que uma casca precisa e uma página não alcança — desligar o PC
 * de quem está longe dele, apontar para outro servidor, recarregar a interface
 * travada e sair. Duplicar aqui um botão que o `index.html` já desenha criaria
 * duas verdades sobre o mesmo estado, que foi o erro que este app inteiro foi
 * refeito para não cometer.
 *
 * COMO SE CHEGA AQUI: pelo botão VOLTAR, quando não há mais nada para onde
 * voltar dentro da página. Foi de propósito — não custa um pixel de tela, todo
 * mundo aperta voltar sem que ninguém ensine, e o momento em que a pessoa está
 * saindo é exatamente o momento em que liberar o PC importa. Antes, esse mesmo
 * toque matava o app em silêncio.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FolhaUtilidades(
    consolidando: Boolean,
    ocupado: String?,                 // rótulo da ação em curso, ou null
    aoFechar: () -> Unit,
    aoEconomizar: () -> Unit,
    aoConsolidar: () -> Unit,
    aoRecarregar: () -> Unit,
    aoConfigurar: () -> Unit,
    aoSair: () -> Unit,
) {
    ModalBottomSheet(onDismissRequest = aoFechar, containerColor = MaterialTheme.colorScheme.surface) {
        Column(Modifier.padding(bottom = 26.dp).navigationBarsPadding()) {
            Row(
                Modifier.fillMaxWidth().padding(start = 24.dp, end = 24.dp, bottom = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Wordmark(tamanho = 11)
                Spacer(Modifier.weight(1f))
                if (ocupado != null) {
                    Text(ocupado, color = Acento, fontSize = 12.sp)
                }
            }

            Item(
                titulo = "Modo economia",
                descricao = "Solta os modelos e devolve a máquina — o PC fica livre. " +
                    "Acordar é um toque, não um boot.",
                habilitado = ocupado == null,
                aoTocar = aoEconomizar,
            )
            Item(
                titulo = if (consolidando) "Parar de consolidar" else "Consolidar agora",
                descricao = if (consolidando)
                    "Interrompe a consolidação e devolve o que ela ocupou."
                else
                    "Roda a destilação de fundo: transforma o que foi conversado em notas.",
                habilitado = ocupado == null,
                aoTocar = aoConsolidar,
            )
            HorizontalDivider(Modifier.padding(vertical = 8.dp), color = Linha)
            Item(
                titulo = "Recarregar a interface",
                descricao = "Recarrega a página sem reiniciar o app.",
                habilitado = true,
                aoTocar = aoRecarregar,
            )
            Item(
                titulo = "Servidor…",
                descricao = "Trocar o endereço do PC ou o token de acesso.",
                habilitado = true,
                aoTocar = aoConfigurar,
            )
            Item(
                titulo = "Sair",
                descricao = "Fecha o app. O servidor continua como está.",
                habilitado = true,
                aoTocar = aoSair,
            )
        }
    }
}

@Composable
private fun Item(titulo: String, descricao: String, habilitado: Boolean, aoTocar: () -> Unit) {
    val alfa = if (habilitado) 1f else .38f
    Column(
        Modifier
            .fillMaxWidth()
            .clickable(enabled = habilitado, onClick = aoTocar)
            .padding(horizontal = 24.dp, vertical = 13.dp)
    ) {
        Text(
            titulo, fontSize = 16.sp, fontWeight = FontWeight.Medium,
            color = MaterialTheme.colorScheme.onSurface.copy(alpha = alfa),
        )
        Spacer(Modifier.height(3.dp))
        Text(descricao, fontSize = 12.sp, color = Texto2.copy(alpha = alfa), lineHeight = 17.sp)
    }
}

/** Um chip discreto para avisos curtos da casca (ação feita, ação falhou). */
@Composable
fun AvisoDaCasca(texto: String) {
    Box(Modifier.fillMaxWidth().padding(12.dp), contentAlignment = Alignment.TopCenter) {
        Text(
            texto,
            color = MaterialTheme.colorScheme.onSurface,
            fontSize = 13.sp,
            modifier = Modifier
                .clip(CircleShape)
                .background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 16.dp, vertical = 9.dp),
        )
    }
}
