package com.mentedigital.app.ui

import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mentedigital.app.Boot
import com.mentedigital.app.Saude

/**
 * A MESMA tela de boot do app do PC (`app.py:_SPLASH_HTML`), em Compose.
 *
 * Por que ela é nativa e não HTML: pelo mesmo motivo que no desktop — ela existe
 * justamente porque o servidor ainda não está pronto, então não pode depender
 * dele para se desenhar.
 *
 * E por que ela existe NO CELULAR: o PC fica em standby com os modelos soltos
 * (~1,6 GB em vez de 6,8 GB de VRAM). Quando o app abre, ele ACORDA o PC e a
 * espera acontece aqui, com progresso REAL lido do `/api/health` — em vez de
 * dentro da primeira pergunta, onde ela pareceria lentidão.
 */
@Composable
fun TelaBoot(
    saude: Saude,
    segundos: Int,
    escapeOferecido: Boolean,
    aoForcarEntrada: () -> Unit,
) {
    val marcos = Boot.marcosDe(saude)
    val (pct, _) = Boot.progresso(marcos)
    val indeterminado = pct <= 0

    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)
            .statusBarsPadding().navigationBarsPadding().padding(horizontal = 28.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Wordmark(tamanho = 13)
        Spacer(Modifier.height(34.dp))
        Text(Boot.estagio(marcos), fontSize = 22.sp, color = MaterialTheme.colorScheme.onBackground)
        Spacer(Modifier.height(30.dp))

        Anel(pct = pct, indeterminado = indeterminado)

        Spacer(Modifier.height(22.dp))
        Text(detalhe(saude, marcos, segundos), color = Texto2, fontSize = 12.sp)
        Spacer(Modifier.height(18.dp))
        Trilho(pct)
        Spacer(Modifier.height(26.dp))
        Servicos(marcos)

        if (escapeOferecido) {
            Spacer(Modifier.height(28.dp))
            Text(
                faltando(marcos).joinToString(", ") + " não subiu em ${segundos}s. " +
                    "O app funciona sem, com a função correspondente indisponível.",
                color = Texto2, fontSize = 12.sp,
            )
            Spacer(Modifier.height(10.dp))
            OutlinedButton(onClick = aoForcarEntrada, shape = CircleShape) {
                Text("Entrar assim mesmo")
            }
        }

        Spacer(Modifier.height(40.dp))
        Text("100% local · nada sai da sua rede", color = Texto2, fontSize = 11.sp)
    }
}

private fun faltando(marcos: Map<String, Boolean>): List<String> =
    Boot.MARCOS.filter { marcos[it.first] != true }.map { it.second }

private fun detalhe(saude: Saude, marcos: Map<String, Boolean>, seg: Int): String {
    if (!saude.alcancavel) return "${seg}s · acordando o PC…"
    val falta = faltando(marcos)
    if (falta.isEmpty()) return "pronto em ${seg}s"
    if (falta == listOf("Índice e ajustes") && saude.tarefasDeFundo.isNotEmpty())
        return "${seg}s · ${saude.tarefasDeFundo.joinToString(", ")}"
    return "${seg}s · faltando: ${falta.joinToString(", ")}"
}

/** O anel de progresso com o degradê — o mesmo gesto da tela do PC. */
@Composable
private fun Anel(pct: Int, indeterminado: Boolean) {
    val giro = rememberInfiniteTransition(label = "giro")
    val angulo by giro.animateFloat(
        0f, 360f, infiniteRepeatable(tween(1500, easing = LinearEasing)), label = "angulo")
    val alvo by animateFloatAsState(
        targetValue = pct / 100f, animationSpec = tween(550), label = "arco")

    Box(Modifier.size(196.dp), contentAlignment = Alignment.Center) {
        Canvas(
            Modifier.fillMaxSize()
                .then(if (indeterminado) Modifier.rotate(angulo) else Modifier)
        ) {
            val traco = 11f
            val lado = size.minDimension - traco
            val canto = Offset((size.width - lado) / 2, (size.height - lado) / 2)
            drawArc(
                color = Superficie, startAngle = 0f, sweepAngle = 360f, useCenter = false,
                topLeft = canto, size = Size(lado, lado), style = Stroke(traco),
            )
            drawArc(
                brush = Brush.sweepGradient(listOf(Azul, Roxo, Rosa, Azul)),
                startAngle = -90f,
                sweepAngle = if (indeterminado) 90f else 360f * alvo,
                useCenter = false, topLeft = canto, size = Size(lado, lado),
                style = Stroke(traco, cap = StrokeCap.Round),
            )
        }
        if (!indeterminado) {
            Text("$pct%", fontSize = 40.sp, fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onBackground)
        }
    }
}

@Composable
private fun Trilho(pct: Int) {
    val largura by animateFloatAsState(pct / 100f, tween(550), label = "trilho")
    Box(
        Modifier.fillMaxWidth().height(5.dp).clip(CircleShape).background(Superficie)
    ) {
        Box(Modifier.fillMaxWidth(largura.coerceIn(0f, 1f)).fillMaxHeight()
            .clip(CircleShape).background(Degrade))
    }
}

/** A fileira de pontinhos: os serviços acendendo, um a um. */
@Composable
private fun Servicos(marcos: Map<String, Boolean>) {
    val pulso = rememberInfiniteTransition(label = "pulso")
    val brilho by pulso.animateFloat(
        0.3f, 1f,
        infiniteRepeatable(tween(1100, easing = LinearEasing), RepeatMode.Reverse),
        label = "brilho",
    )
    Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
        // "porta" fica de fora, como no PC: ela não é um serviço que o dono
        // reconheça, é consequência de o servidor ter respondido.
        Boot.MARCOS.filter { it.first != "porta" }.forEach { (chave, rotulo, _) ->
            val ok = marcos[chave] == true
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                Box(Modifier.size(6.dp).clip(CircleShape)
                    .background(if (ok) Rosa else Acento.copy(alpha = brilho)))
                Text(rotulo, fontSize = 10.sp,
                    color = if (ok) MaterialTheme.colorScheme.onBackground else Texto2)
            }
        }
    }
}
