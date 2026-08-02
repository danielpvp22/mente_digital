package com.mentedigital.app

import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Um turno de conversa, como o SQLite do servidor devolve. */
data class Turno(val pergunta: String, val resposta: String)

/** Uma conversa na lista lateral. */
data class Conversa(val id: String, val titulo: String, val quando: String)

/** O que `/api/health` respondeu. `alcancavel=false` = o servidor nem respondeu. */
data class Saude(
    val alcancavel: Boolean,
    val servicos: Map<String, Boolean> = emptyMap(),
    val detalhe: String = "",
)

/**
 * As rotas `/api`. OkHttp cru + `org.json`, sem Retrofit: são quatro GETs de
 * JSON simples, e uma dependência a menos é uma superfície a menos.
 *
 * ⚠ Não escreva `/api` seguido de asterisco aqui: o Kotlin aceita comentário de
 * bloco ANINHADO, então a sequência barra-asterisco dentro de um KDoc abre um
 * comentário que nunca fecha — e o compilador engole o arquivo inteiro com um
 * "Unclosed comment" apontando para a última linha. Custou uma compilação.
 *
 * ⚠ O token vai no HEADER `X-Mente-Token` (main.py:243), nunca na query. O app
 * nativo não tem a limitação do `<img>` que forçou a query no front.
 */
class ApiMente(private val conf: () -> Conf) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    /**
     * Teste de conexão da tela de configuração.
     *
     * `/api/health` é a ÚNICA rota sem gate de acesso (main.py:254-266), e o
     * docstring dela diz que existe exatamente para isto. É o que separa
     * "servidor inalcançável" de "servidor recusou o token" — sem essa
     * distinção, o app só teria o close 1008 do WebSocket, que é o mesmo para
     * token errado, Origin divergente e aparelho não autorizado (Risco R5).
     */
    fun saude(base: String): Saude {
        val alvo = Endereco.api(base, "/api/health")
        return try {
            http.newCall(Request.Builder().url(alvo).build()).execute().use { r ->
                if (!r.isSuccessful) return Saude(false, detalhe = "HTTP ${r.code}")
                val corpo = JSONObject(r.body?.string().orEmpty())
                val srv = corpo.optJSONObject("servicos")
                val mapa = LinkedHashMap<String, Boolean>()
                srv?.keys()?.forEach { k -> mapa[k] = srv.optBoolean(k) }
                Saude(true, mapa)
            }
        } catch (e: IOException) {
            Saude(false, detalhe = e.message ?: "sem resposta")
        } catch (e: Exception) {
            Saude(false, detalhe = e.message ?: "resposta ilegível")
        }
    }

    /** Histórico agrupado em CONVERSAS (não turnos soltos) — main.py:502. */
    fun conversas(): List<Conversa> {
        val bruto = get("/api/conversas") ?: return emptyList()
        val arr = try {
            JSONArray(bruto)
        } catch (e: Exception) {
            return emptyList()
        }
        // ⚠ NOMES MEDIDOS contra o servidor, não supostos (2026-08-02):
        //     /api/conversas      -> {"id", "titulo", "fim", "n"}
        // Eu tinha escrito "conversa_id"/"timestamp" por dedução, e o
        // `optString` devolve "" para chave ausente — ou seja, a tela ficava
        // sem data e ninguém via erro nenhum.
        return (0 until arr.length()).mapNotNull { i ->
            arr.optJSONObject(i)?.let { o ->
                Conversa(
                    id = o.optString("id"),
                    titulo = o.optString("titulo"),
                    quando = o.optString("fim").take(16).replace("T", " "),
                )
            }
        }.filter { it.id.isNotEmpty() }
    }

    /** Todos os turnos de uma conversa, para reabrir — main.py:509. */
    fun conversa(id: String): List<Turno> {
        val bruto = get("/api/conversa/$id") ?: return emptyList()
        val arr = try {
            JSONArray(bruto)
        } catch (e: Exception) {
            return emptyList()
        }
        // ⚠ O DEFEITO QUE A TELA PEGOU: os campos são `q`/`a`/`t`, não
        //     `pergunta`/`resposta`. Com os nomes errados, `optString` devolvia
        //     "" para os dois, a conversa reabria VAZIA e nada falhava — nem
        //     exceção, nem log. Foi preciso abrir uma conversa no emulador para
        //     ver. Medido: {"q": "...", "a": "...", "t": "2026-08-02T17:18:55"}.
        return (0 until arr.length()).mapNotNull { i ->
            arr.optJSONObject(i)?.let { Turno(it.optString("q"), it.optString("a")) }
        }
    }

    private fun get(caminho: String): String? {
        // Lido a cada chamada, não no construtor: o dono pode trocar endereço e
        // token na tela de ajustes sem o app ser recriado.
        val c = conf()
        if (c.base.isEmpty()) return null
        val req = Request.Builder()
            .url(Endereco.api(c.base, caminho))
            .header("X-Mente-Token", c.token)
            .build()
        return try {
            http.newCall(req).execute().use { r -> if (r.isSuccessful) r.body?.string() else null }
        } catch (e: Exception) {
            null
        }
    }
}
