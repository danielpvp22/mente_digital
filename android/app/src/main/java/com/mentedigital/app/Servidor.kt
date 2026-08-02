package com.mentedigital.app

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Prontidão dos serviços, lida de `/api/health`. */
data class Saude(
    val alcancavel: Boolean,
    val servicos: Map<String, Boolean> = emptyMap(),
    val tarefasDeFundo: List<String> = emptyList(),
    val detalhe: String = "",
) {
    /** O servidor está de pé mas com os modelos soltos — o "standby" do PC. */
    val descansando: Boolean
        get() = alcancavel && servicos.isNotEmpty() && servicos["llm"] == false
}

/**
 * As rotas `/api` que o APP nativo usa. São só duas, e as duas existem para a
 * TELA DE BOOT — o resto da conversa acontece dentro do WebView, na mesma SPA do
 * desktop.
 *
 * ⚠ Token no HEADER `X-Mente-Token` (main.py:243). `/api/health` é a única rota
 * SEM gate (main.py:254-266), e o docstring dela diz que existe exatamente para
 * "a tela de boot do app nativo apontada para um servidor remoto".
 */
class Servidor(private val conf: () -> Conf) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(200, TimeUnit.SECONDS)    // `ligar` carrega modelo: demora
        .build()

    fun saude(base: String = conf().base): Saude {
        if (base.isBlank()) return Saude(false, detalhe = "sem endereço")
        return try {
            http.newCall(Request.Builder().url(Endereco.api(base, "/api/health")).build())
                .execute().use { r ->
                    if (!r.isSuccessful) return Saude(false, detalhe = "HTTP ${r.code}")
                    val o = JSONObject(r.body?.string().orEmpty())
                    val srv = o.optJSONObject("servicos")
                    val mapa = LinkedHashMap<String, Boolean>()
                    srv?.keys()?.forEach { k -> mapa[k] = srv.optBoolean(k) }
                    val arr = o.optJSONArray("tarefas_de_fundo")
                    val fundo = (0 until (arr?.length() ?: 0)).map { arr!!.optString(it) }
                    Saude(true, mapa, fundo)
                }
        } catch (e: Exception) {
            Saude(false, detalhe = e.message ?: "sem resposta")
        }
    }

    /**
     * Acorda os modelos no PC.
     *
     * É o "watcher" que o dono pediu, pelo avesso: em vez de o PC vigiar o
     * celular, o CELULAR avisa o PC. Mais simples e mais robusto — não precisa de
     * descoberta, de porta extra nem de processo vigiando; o app só faz, na
     * abertura, o que o botão de energia da bandeja já faz.
     *
     * Idempotente do lado do servidor: `ligar` com tudo carregado devolve o
     * estado e não recarrega nada.
     */
    fun ligar(): Boolean = acao("ligar")

    private fun acao(qual: String): Boolean {
        val c = conf()
        if (c.base.isBlank()) return false
        val corpo = JSONObject().put("acao", qual).toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url(Endereco.api(c.base, "/api/energia"))
            .header("X-Mente-Token", c.token)
            .post(corpo)
            .build()
        return try {
            http.newCall(req).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            false
        }
    }
}

/** Endereço + token, sem depender do Android — o que torna isto testável. */
data class Conf(val base: String, val token: String)

/**
 * Os marcos da tela de boot, com o peso de cada um. Somam 100 de propósito: um
 * marco que não soma some da conta e a barra nunca fecha.
 *
 * São os MESMOS do `app.py` (MARCOS), e pela mesma razão — a tela do celular tem
 * de contar a mesma história que a do PC, senão são dois produtos.
 */
object Boot {
    // ⚠ As CHAVES são as do `/api/health`, medidas contra o servidor:
    //     {"servidor","llm","vault","stt","voz","porta","fundo"}
    // Escrevi "tts" por dedução e a tela de boot nunca fecharia — o marco ficaria
    // apagado para sempre, esperando uma chave que o servidor não manda.
    val MARCOS: List<Triple<String, String, Int>> = listOf(
        Triple("servidor", "Servidor", 10),
        Triple("vault", "Vault", 20),
        Triple("llm", "Modelo", 25),
        Triple("stt", "Escuta", 15),
        Triple("voz", "Voz", 10),
        Triple("porta", "Rede", 5),
        Triple("fundo", "Índice e ajustes", 15),
    )

    /** (percentual, tudo_pronto). Puro/testável. */
    fun progresso(prontos: Map<String, Boolean>): Pair<Int, Boolean> {
        val total = MARCOS.filter { prontos[it.first] == true }.sumOf { it.third }
        return minOf(total, 100) to MARCOS.all { prontos[it.first] == true }
    }

    /** A frase grande, na ordem real em que o servidor alcança os marcos. */
    fun estagio(prontos: Map<String, Boolean>): String = when {
        prontos["servidor"] != true -> "Acordando a mente"
        prontos["stt"] != true -> "Afinando a escuta"
        prontos["vault"] != true -> "Abrindo o vault"
        prontos["llm"] != true -> "Carregando o modelo"
        prontos["voz"] != true -> "Preparando a voz"
        prontos["fundo"] != true -> "Terminando o índice"
        else -> "Tudo pronto"
    }

    /** Traduz o `/api/health` nos marcos da tela. */
    fun marcosDe(s: Saude): Map<String, Boolean> {
        if (!s.alcancavel) return MARCOS.associate { it.first to false }
        val m = HashMap<String, Boolean>()
        MARCOS.forEach { m[it.first] = s.servicos[it.first] ?: false }
        m["servidor"] = true
        m["porta"] = true                       // respondeu = a porta está aberta
        m["fundo"] = s.tarefasDeFundo.isEmpty()
        return m
    }
}
