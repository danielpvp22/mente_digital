package com.mentedigital.app.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * O PC está liberado — a MESMA tela de repouso do `app.py` (a função `dormir()`
 * do splash), em Compose.
 *
 * Por que ela é uma TELA e não um aviso dentro da SPA: com os modelos soltos, o
 * chat não pode responder nada até alguém religar. Deixar a conversa na frente
 * com um chip cinzento convidaria a digitar uma pergunta que ficaria pendurada
 * nos ~30 s do religamento sem nada explicando. Aqui o estado é o assunto da
 * tela, e sair dele é um toque.
 *
 * O anel aparece APAGADO em vez de sumir, como no desktop: é o mesmo app, em
 * repouso — não uma tela de erro nem um app fechado.
 */
@Composable
fun TelaDormindo(
    medida: String,
    religando: Boolean,
    aoAcordar: () -> Unit,
) {
    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)
            .statusBarsPadding().navigationBarsPadding().padding(horizontal = 30.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Wordmark(tamanho = 13)
        Spacer(Modifier.height(36.dp))

        AnelApagado()

        Spacer(Modifier.height(28.dp))
        Text("Descansando", fontSize = 22.sp, color = MaterialTheme.colorScheme.onBackground)
        Spacer(Modifier.height(10.dp))
        Text(
            if (medida.isNotBlank()) medida else "O PC está livre",
            color = Texto2, fontSize = 12.sp,
        )
        Spacer(Modifier.height(20.dp))
        Text(
            "Os modelos foram soltos e a máquina do PC está liberada. " +
                "O assistente continua de pé — ele volta quando você quiser.",
            color = Texto2, fontSize = 13.sp, textAlign = TextAlign.Center,
            lineHeight = 19.sp,
        )
        Spacer(Modifier.height(26.dp))
        Button(
            onClick = aoAcordar,
            enabled = !religando,
            shape = CircleShape,
        ) { Text(if (religando) "Acordando…" else "Acordar o assistente") }
    }
}

/** O anel da tela de boot, sem giro e sem cor — repouso, não progresso. */
@Composable
private fun AnelApagado() {
    Box(Modifier.size(150.dp), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val traco = 9f
            val lado = size.minDimension - traco
            val canto = Offset((size.width - lado) / 2, (size.height - lado) / 2)
            drawArc(
                color = Superficie, startAngle = 0f, sweepAngle = 360f, useCenter = false,
                topLeft = canto, size = Size(lado, lado), style = Stroke(traco),
            )
            drawArc(
                color = Texto2.copy(alpha = .35f), startAngle = -90f, sweepAngle = 34f,
                useCenter = false, topLeft = canto, size = Size(lado, lado),
                style = Stroke(traco, cap = StrokeCap.Round),
            )
        }
    }
}
