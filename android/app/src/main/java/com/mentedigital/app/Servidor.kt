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
    /**
     * O servidor está de pé com os modelos SOLTOS de propósito — o modo economia.
     *
     * ⚠ Isto era DEDUZIDO de `llm == false`, e a dedução é ambígua no caso mais
     * comum de todos: durante um boot normal o LLM também está em false, e o app
     * mandava um "ligar" por cima de um carregamento já em curso. O servidor passou
     * a dizer com todas as letras (main.py, /api/health). O `null` guarda o caso de
     * um APK novo contra um servidor antigo: aí, e só aí, volta-se à dedução.
     */
    val descansandoDito: Boolean? = null,
) {
    val descansando: Boolean
        get() = when {
            !alcancavel -> false
            descansandoDito != null -> descansandoDito
            else -> servicos.isNotEmpty() && servicos["llm"] == false
        }
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
                    val dito = if (o.has("descansando")) o.optBoolean("descansando") else null
                    Saude(true, mapa, fundo, descansandoDito = dito)
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
    fun ligar(): Energia? = energia("ligar")

    /**
     * MODO ECONOMIA: solta os modelos e devolve a máquina ao dono.
     *
     * É o mesmo botão que a bandeja do PC tem ("Descansar"), agora ao alcance de
     * quem está longe do computador — que é justamente quem mais precisa dele. O
     * servidor continua de pé: acordar é um toque, não um boot.
     */
    fun desligar(): Energia? = energia("desligar")

    /** Dispara ou interrompe a consolidação de fundo (o "Consolidar" da bandeja). */
    fun idle(acao: String): Boolean = postar("/api/idle", acao) != null

    private fun energia(qual: String): Energia? = postar("/api/energia", qual)?.let(Energia::de)

    /**
     * POST curto nas rotas de controle. Devolve o corpo, ou null se falhou.
     *
     * ⚠ Token no HEADER, sempre: sem ele o gate de acesso responde 401 e a ação
     * some sem nada na tela. As duas rotas daqui são gateadas (só /api/health não é).
     */
    private fun postar(rota: String, acao: String): JSONObject? {
        val c = conf()
        if (c.base.isBlank()) return null
        val corpo = JSONObject().put("acao", acao).toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url(Endereco.api(c.base, rota))
            .header("X-Mente-Token", c.token)
            .post(corpo)
            .build()
        return try {
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) null else JSONObject(r.body?.string().orEmpty())
            }
        } catch (e: Exception) {
            null
        }
    }
}

/** Endereço + token, sem depender do Android — o que torna isto testável. */
data class Conf(val base: String, val token: String)

/**
 * A resposta de `/api/energia` — estado e a medição que o servidor fez.
 *
 * A medição vem junto porque o projeto inteiro prefere MOSTRAR o número a
 * afirmar o efeito: "liberei 4,9 GB" é conferível, "liberado" é uma promessa.
 */
data class Energia(
    val estado: String,
    val vramMb: Int?,
    val ramCommitMb: Int?,
    /**
     * O servidor CEDEU A VEZ a uma resposta em andamento e não dormiu.
     *
     * Existe porque este botão é apertado de longe, por quem não vê o que está
     * acontecendo no PC: soltar os modelos no meio de um turno não derruba a
     * resposta, deixa-a pior em silêncio (medido em 2026-08-02 — o embedding
     * sumiu e a busca de figuras caiu para "só com texto"). Aqui isso vira uma
     * recusa com motivo, e o app diz para tentar de novo.
     */
    val adiado: Boolean = false,
    val motivo: String = "",
) {

    val descansando: Boolean get() = estado == "descansando"

    /**
     * A medição em uma linha, em português. Puro/testável.
     *
     * O que o servidor manda é o uso do DISPOSITIVO (inclui o desktop e qualquer
     * outro programa na GPU) — é o número que aparece no nvidia-smi, e por isso o
     * rótulo aqui é "em uso", não "liberado": atribuir a queda inteira ao
     * assistente seria propaganda.
     */
    fun resumo(): String {
        val partes = mutableListOf<String>()
        vramMb?.let { partes += gb(it) + " GB de VRAM" }
        ramCommitMb?.let { partes += gb(it) + " GB de RAM" }
        return if (partes.isEmpty()) "" else partes.joinToString(" · ") + " em uso"
    }

    private fun gb(mb: Int): String = String.format(java.util.Locale.US, "%.1f", mb / 1024.0)
        .replace('.', ',')

    companion object {
        fun de(o: JSONObject): Energia {
            // A medição vem em "depois" ("como ficou"); o /api/energia sem ação
            // devolve só ela. Campo ausente é null, nunca 0 — "não medi" e "zero"
            // são afirmações diferentes (mesma régua do energia.py no servidor).
            val m = o.optJSONObject("depois") ?: JSONObject()
            fun inteiro(chave: String): Int? = if (m.isNull(chave)) null else m.optInt(chave)
            return Energia(
                o.optString("estado", "ligado"),
                inteiro("vram_mb"), inteiro("ram_commit_mb"),
                adiado = o.optBoolean("adiado"),
                motivo = o.optString("motivo", ""),
            )
        }
    }
}

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
