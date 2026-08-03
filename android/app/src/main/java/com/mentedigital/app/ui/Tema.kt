package com.mentedigital.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.sp

// A MESMA paleta do index.html e da casca desktop — o app é a quarta casca da
// mesma interface, não um produto com identidade própria.
val Azul = Color(0xFF4285F4)
val Roxo = Color(0xFF9B72CB)
val Rosa = Color(0xFFD96570)
val Grafite = Color(0xFF0B0B0D)
val Superficie = Color(0xFF16161A)
val BolhaCor = Color(0xFF23252B)
val Texto = Color(0xFFF2F2F3)
val Texto2 = Color(0xFF8B8F96)
val Acento = Color(0xFF8AB4F8)

/** A linha divisória do tema escuro — o `rgba(255,255,255,.08)` do index.html.
 *  ⚠ Declarada ANTES dos `ColorScheme`: propriedade de topo em Kotlin inicializa
 *  na ordem do arquivo, e usá-la antes daria "must be initialized". */
val Linha = Color(0x14FFFFFF)

/** O gradiente da marca, para a barra e os acentos. */
val Degrade = Brush.horizontalGradient(listOf(Azul, Roxo, Rosa))

private val Escuro = darkColorScheme(
    primary = Acento, onPrimary = Grafite,
    background = Grafite, onBackground = Texto,
    surface = Superficie, onSurface = Texto,
    surfaceVariant = BolhaCor, onSurfaceVariant = Texto,
    outline = Linha, outlineVariant = Linha,
    error = Rosa,
)

private val Claro = lightColorScheme(
    primary = Azul, onPrimary = Color.White,
    background = Color(0xFFF7F7F9), onBackground = Color(0xFF1A1B1E),
    surface = Color.White, onSurface = Color(0xFF1A1B1E),
    surfaceVariant = Color(0xFFECEDF1), onSurfaceVariant = Color(0xFF1A1B1E),
    error = Rosa,
)

/**
 * ⚠ ESCURO POR PADRÃO, e NÃO seguindo o sistema.
 *
 * É a mesma decisão do front (`index.html` nasce com `data-tema="escuro"`) e da
 * janela nativa. O gradiente azul→roxo→rosa foi desenhado sobre #0B0B0D; no
 * fundo claro ele perde o contraste e o app deixa de parecer o produto. Quem
 * quiser claro passa `escuro = false` — a paleta clara existe e está calibrada,
 * só não é o default.
 */
@Composable
fun TemaMenteDigital(escuro: Boolean = true, conteudo: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (escuro) Escuro else Claro, content = conteudo)
}

/** O tema do sistema, para quando o app oferecer a escolha ao dono. */
@Composable
fun sistemaPrefereEscuro(): Boolean = isSystemInDarkTheme()

/**
 * O wordmark: "MENTE" em texto normal, "DIGITAL" preenchido pelo degradê — o
 * mesmo da barra de título do PC e da tela de boot.
 *
 * Vive aqui, e não numa tela, porque só as duas telas NATIVAS (configuração e
 * boot) o desenham. Dentro do app quem desenha tudo é a SPA.
 */
@Composable
fun Wordmark(tamanho: Int = 12) {
    Text(
        buildAnnotatedString {
            withStyle(SpanStyle(color = MaterialTheme.colorScheme.onBackground)) { append("MENTE") }
            withStyle(SpanStyle(brush = Degrade)) { append("DIGITAL") }
        },
        fontSize = tamanho.sp,
        fontWeight = FontWeight.SemiBold,
        letterSpacing = 2.2.sp,
    )
}
