package com.mentedigital.app.ui

import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
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
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mentedigital.app.Bolha
import com.mentedigital.app.ChatViewModel
import com.mentedigital.app.Conexao

@Composable
fun TelaChat(
    vm: ChatViewModel,
    aoAbrirConversas: () -> Unit,
    aoAbrirConfig: () -> Unit,
    aoAlternarVoz: () -> Unit,
) {
    var entrada by remember { mutableStateOf("") }
    val lista = rememberLazyListState()

    // Rola para o fim a cada token: sem isto o streaming acontece fora da tela.
    LaunchedEffect(vm.bolhas.size, vm.bolhas.lastOrNull()?.texto?.length) {
        if (vm.bolhas.isNotEmpty()) lista.animateScrollToItem(vm.bolhas.lastIndex)
    }

    // ⚠ `statusBarsPadding()` NÃO é enfeite: com `targetSdk = 35` o Android 15
    // FORÇA edge-to-edge, e sem isto a barra do app fica POR BAIXO da barra de
    // status — o relógio some e os botões do topo não recebem toque nenhum.
    // Visto no emulador (API 35) em 2026-08-02: "Conversas" não respondia.
    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)
            .statusBarsPadding()
    ) {
        BarraTopo(vm, aoAbrirConversas, aoAbrirConfig)
        AvisoDeConexao(vm)

        LazyColumn(
            state = lista,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            if (vm.bolhas.isEmpty()) item { BoasVindas() }
            // Chave pelo ÍNDICE: o texto MUDA a cada token, e usar o conteúdo
            // como chave recriaria o item a cada pedaço — a lista piscaria
            // durante o streaming.
            itemsIndexed(vm.bolhas, key = { i, _ -> i }) { _, b -> BolhaView(b) }
        }

        if (vm.modoVoz) BarraDeVoz(vm, aoAlternarVoz)
        Entrada(
            valor = entrada,
            aoMudar = { entrada = it },
            habilitado = vm.conexao == Conexao.ABERTO,
            emVoz = vm.modoVoz,
            aoEnviar = { vm.enviar(entrada); entrada = "" },
            aoAlternarVoz = aoAlternarVoz,
        )
    }
}

// --------------------------------------------------------------------------- //
// Topo                                                                        //
// --------------------------------------------------------------------------- //
@Composable
private fun BarraTopo(vm: ChatViewModel, aoAbrirConversas: () -> Unit, aoAbrirConfig: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 6.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        TextButton(onClick = aoAbrirConversas) { Text("Conversas", fontSize = 13.sp) }
        Spacer(Modifier.weight(1f))
        Wordmark()
        Spacer(Modifier.weight(1f))
        PontoDeConexao(vm.conexao)
        TextButton(onClick = { vm.novaConversa() }) { Text("Novo", fontSize = 13.sp) }
        TextButton(onClick = aoAbrirConfig) { Text("⚙", fontSize = 15.sp) }
    }
    HorizontalDivider(color = MaterialTheme.colorScheme.outline)
}

/** O mesmo wordmark da janela nativa e da tela de boot: "MENTE" em texto normal,
 *  "DIGITAL" preenchido pelo degradê. */
@Composable
fun Wordmark(tamanho: Int = 12) {
    Text(
        buildAnnotatedString {
            withStyle(SpanStyle(color = MaterialTheme.colorScheme.onSurface)) { append("MENTE") }
            withStyle(SpanStyle(brush = Degrade)) { append("DIGITAL") }
        },
        fontSize = tamanho.sp,
        fontWeight = FontWeight.SemiBold,
        letterSpacing = 2.2.sp,
    )
}

@Composable
private fun PontoDeConexao(c: Conexao) {
    val cor = when (c) {
        Conexao.ABERTO -> Acento
        Conexao.CONECTANDO -> Roxo
        Conexao.RECUSADO -> Rosa
        else -> Texto2
    }
    Box(Modifier.size(7.dp).clip(CircleShape).background(cor))
}

@Composable
private fun AvisoDeConexao(vm: ChatViewModel) {
    // O estado da conexão é DITO, não escondido: um app que parece online e
    // engole a mensagem é pior que um que avisa que caiu.
    val aviso = when (vm.conexao) {
        Conexao.RECUSADO -> vm.detalheConexao.ifEmpty { "O servidor recusou o acesso." }
        Conexao.CAIU -> "Sem conexão. Tentando de novo…"
        Conexao.CONECTANDO -> "Conectando…"
        else -> vm.status
    }
    if (aviso.isBlank()) return
    Text(
        aviso,
        color = if (vm.conexao == Conexao.RECUSADO) Rosa else Texto2,
        fontSize = 12.sp,
        modifier = Modifier.fillMaxWidth()
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 16.dp, vertical = 5.dp),
    )
}

@Composable
private fun BoasVindas() {
    Column(
        Modifier.fillMaxWidth().padding(top = 64.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Wordmark(tamanho = 15)
        Text("Pergunte alguma coisa.", color = Texto2, fontSize = 14.sp)
        Text("O que responder vem do vault no seu PC.", color = Texto2, fontSize = 12.sp)
    }
}

// --------------------------------------------------------------------------- //
// Bolhas                                                                      //
// --------------------------------------------------------------------------- //
@Composable
private fun BolhaView(b: Bolha) {
    val alinhamento = if (b.doUsuario) Alignment.CenterEnd else Alignment.CenterStart
    val fundo = when {
        b.erro -> Rosa.copy(alpha = 0.14f)
        b.proativo -> Roxo.copy(alpha = 0.16f)
        b.doUsuario -> BolhaCor
        else -> MaterialTheme.colorScheme.surface
    }
    // Canto "colado" no lado de quem fala — o mesmo gesto do front.
    val forma = if (b.doUsuario)
        RoundedCornerShape(18.dp, 18.dp, 5.dp, 18.dp)
    else
        RoundedCornerShape(18.dp, 18.dp, 18.dp, 5.dp)

    Box(Modifier.fillMaxWidth(), contentAlignment = alinhamento) {
        Column(
            Modifier.widthIn(max = 320.dp).clip(forma).background(fundo)
                .padding(horizontal = 14.dp, vertical = 11.dp),
            verticalArrangement = Arrangement.spacedBy(7.dp),
        ) {
            if (b.proativo) Text("🔔 aviso do servidor", color = Roxo, fontSize = 11.sp)
            Text(b.texto, color = MaterialTheme.colorScheme.onSurface, fontSize = 15.sp,
                lineHeight = 21.sp)
            if (b.fontes.isNotEmpty()) Fontes(b)
        }
    }
}

@Composable
private fun Fontes(b: Bolha) {
    // `itens` são strings COM PREFIXO ("memoria", "nota:x.md", "web:dominio"),
    // não objetos — e a `rota` é a cascata usada, com "+" entre as fontes.
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        Text(if (b.rota.isNotEmpty()) "fontes · ${b.rota}" else "fontes",
            color = Texto2, fontSize = 10.sp, fontWeight = FontWeight.Medium,
            letterSpacing = 0.6.sp)
        FlowFontes(b.fontes)
    }
}

@Composable
private fun FlowFontes(fontes: List<String>) {
    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
        fontes.take(5).forEach { f ->
            val (tipo, nome) = f.substringBefore(':') to f.substringAfter(':', f)
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Box(Modifier.clip(RoundedCornerShape(6.dp))
                    .background(corDaFonte(tipo).copy(alpha = 0.18f))
                    .padding(horizontal = 6.dp, vertical = 1.dp)) {
                    Text(tipo.take(7), color = corDaFonte(tipo), fontSize = 9.sp)
                }
                Text(nome.substringAfterLast('/').removeSuffix(".md"),
                    color = Texto2, fontSize = 11.sp, maxLines = 1)
            }
        }
        if (fontes.size > 5) Text("e mais ${fontes.size - 5}", color = Texto2, fontSize = 10.sp)
    }
}

private fun corDaFonte(tipo: String): Color = when (tipo) {
    "web" -> Rosa
    "nota" -> Azul
    else -> Roxo          // "memoria" e o que vier
}

// --------------------------------------------------------------------------- //
// Voz                                                                         //
// --------------------------------------------------------------------------- //
/** A faixa que aparece SÓ no modo voz, dizendo em que estado ele está. Sem ela o
 *  dono não sabe se o app está ouvindo, se a IA está falando, ou se travou. */
@Composable
private fun BarraDeVoz(vm: ChatViewModel, aoAlternarVoz: () -> Unit) {
    val pulso = rememberInfiniteTransition(label = "pulso")
    val escala by pulso.animateFloat(
        initialValue = 0.55f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(900, easing = LinearEasing), RepeatMode.Reverse),
        label = "escala",
    )
    val falando = vm.iaFalando
    Row(
        Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Box(Modifier.size(10.dp).clip(CircleShape)
            .background(if (falando) Rosa else Acento.copy(alpha = escala)))
        Text(
            if (falando) "Falando… fale por cima para interromper" else "Ouvindo",
            color = if (falando) Rosa else MaterialTheme.colorScheme.onSurface,
            fontSize = 13.sp,
        )
        Spacer(Modifier.weight(1f))
        TextButton(onClick = aoAlternarVoz) { Text("Encerrar", fontSize = 13.sp) }
    }
}

// --------------------------------------------------------------------------- //
// Entrada                                                                     //
// --------------------------------------------------------------------------- //
@Composable
private fun Entrada(
    valor: String,
    aoMudar: (String) -> Unit,
    habilitado: Boolean,
    emVoz: Boolean,
    aoEnviar: () -> Unit,
    aoAlternarVoz: () -> Unit,
) {
    Row(
        Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface)
            .padding(10.dp).navigationBarsPadding().imePadding(),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        OutlinedTextField(
            value = valor, onValueChange = aoMudar,
            modifier = Modifier.weight(1f),
            placeholder = { Text("Pergunte à Mente Digital", color = Texto2) },
            shape = RoundedCornerShape(22.dp),
            colors = OutlinedTextFieldDefaults.colors(
                focusedBorderColor = Acento,
                unfocusedBorderColor = MaterialTheme.colorScheme.outline,
            ),
            maxLines = 5,
        )
        BotaoLive(ativo = emVoz, aoTocar = aoAlternarVoz)
        if (valor.isNotBlank()) {
            Button(
                onClick = aoEnviar, enabled = habilitado,
                shape = CircleShape,
                contentPadding = PaddingValues(horizontal = 18.dp, vertical = 12.dp),
            ) { Text("→", fontSize = 17.sp) }
        }
    }
}

/** O botão de voz — o "live" do front. Acende com o degradê quando ativo. */
@Composable
private fun BotaoLive(ativo: Boolean, aoTocar: () -> Unit) {
    Box(
        Modifier.size(48.dp).clip(CircleShape)
            .then(if (ativo) Modifier.background(Degrade)
                  else Modifier.border(1.dp, MaterialTheme.colorScheme.outline, CircleShape))
            .clickable(onClick = aoTocar),
        contentAlignment = Alignment.Center,
    ) {
        Text("🎙", fontSize = 19.sp)
    }
}
