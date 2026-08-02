package com.mentedigital.app

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Onde o endereço e o token ficam guardados.
 *
 * O token é segredo de LONGA DURAÇÃO e dá acesso de ESCRITA ao vault
 * (`POST /api/nota/texto`) — o docstring de acesso.py:4-6 é explícito: sem gate,
 * "qualquer aparelho da LAN lê e ENVENENA a base". Daí
 * `EncryptedSharedPreferences` e não `SharedPreferences` cru.
 *
 * ⚠ A queda para prefs em claro é FAIL-SOFT mas NÃO É SILENCIOSA. O Keystore
 * falha de verdade em alguns aparelhos (ROM modificada, perfil de trabalho,
 * relógio errado na primeira execução), e um app que trava por isso é inútil;
 * mas guardar o segredo em claro sem avisar seria pior que travar. `criptografado`
 * é lido pela tela de configuração, que diz ao dono o que está acontecendo.
 */
class Ajustes(contexto: Context) {

    var criptografado: Boolean = true
        private set

    private val prefs: SharedPreferences = try {
        val chave = MasterKey.Builder(contexto)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            contexto,
            "mente_ajustes",
            chave,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    } catch (e: Exception) {
        criptografado = false
        Log.w(TAG, "Keystore indisponível — token em prefs comuns. O app avisa na tela.", e)
        contexto.getSharedPreferences("mente_ajustes_claro", Context.MODE_PRIVATE)
    }

    var base: String
        get() = prefs.getString(K_BASE, "") ?: ""
        set(v) = prefs.edit().putString(K_BASE, Endereco.normalizarBase(v)).apply()

    var token: String
        get() = prefs.getString(K_TOKEN, "") ?: ""
        set(v) = prefs.edit().putString(K_TOKEN, v.trim()).apply()

    /** Id da conversa corrente. Gerado pelo CLIENTE (index.html:598) — o servidor
     *  não o inventa, e é o app que é dono do ciclo de vida. Persistido para a
     *  reabertura do app continuar a mesma conversa. */
    var conversaId: String
        get() = prefs.getString(K_CONVERSA, "") ?: ""
        set(v) = prefs.edit().putString(K_CONVERSA, v).apply()

    val configurado: Boolean get() = base.isNotEmpty()

    private companion object {
        const val TAG = "Ajustes"
        const val K_BASE = "base"
        const val K_TOKEN = "token"
        const val K_CONVERSA = "conversa_id"
    }
}
