package com.mentedigital.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color

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

/** O gradiente da marca, para a barra e os acentos. */
val Degrade = Brush.horizontalGradient(listOf(Azul, Roxo, Rosa))

private val Escuro = darkColorScheme(
    primary = Acento, onPrimary = Grafite,
    background = Grafite, onBackground = Texto,
    surface = Superficie, onSurface = Texto,
    surfaceVariant = BolhaCor, onSurfaceVariant = Texto,
    error = Rosa,
)

private val Claro = lightColorScheme(
    primary = Azul, onPrimary = Color.White,
    background = Color(0xFFF7F7F9), onBackground = Color(0xFF1A1B1E),
    surface = Color.White, onSurface = Color(0xFF1A1B1E),
    surfaceVariant = Color(0xFFECEDF1), onSurfaceVariant = Color(0xFF1A1B1E),
    error = Rosa,
)

@Composable
fun TemaMenteDigital(escuro: Boolean = isSystemInDarkTheme(), conteudo: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (escuro) Escuro else Claro, content = conteudo)
}
