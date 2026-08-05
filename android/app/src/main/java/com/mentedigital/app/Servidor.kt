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

    /**
     * ⚠ `httpCurto` e NÃO `http`: o cliente de cima tem 200 s de LEITURA, que
     * existem para o `ligar` carregar modelo. A sonda de boot herdava esse teto,
     * e um TCP que CONECTA e não responde (endereço de LAN visto de fora, proxy
     * que aceita e cala) pendurava a volta do laço por mais de três minutos —
     * sem nada mudar na tela. O `/api/health` é um dict: responde em
     * milissegundos ou não está lá, e não responder é uma resposta útil.
     */
    fun saude(base: String = conf().base): Saude {
        if (base.isBlank()) return Saude(false, detalhe = "sem endereço")
        return try {
            httpCurto.newCall(Request.Builder().url(Endereco.api(base, "/api/health")).build())
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

    // ---- o VIGIA -----------------------------------------------------------
    /**
     * O plantão mínimo do PC (`mente_digital/vigia.py`), consultado ANTES do
     * `/api/health`.
     *
     * Por que existe: depois de 45 min sem uso o assistente se ENCERRA e o PC
     * volta a zero — então não há `/api/health` para perguntar. Quem atende é o
     * vigia, num processo de algumas dezenas de MB sem torch. Ele é a diferença entre "o PC está
     * desligado/fora da rede" e "o assistente está dormindo e eu posso acordá-lo".
     */
    fun vigiaStatus(base: String = conf().base): VigiaStatus? {
        if (base.isBlank()) return null
        val url = Endereco.vigia(base, "/vigia/status")
        return try {
            httpCurto.newCall(Request.Builder().url(url).build()).execute().use { r ->
                if (!r.isSuccessful) return null
                val o = JSONObject(r.body?.string().orEmpty())
                VigiaStatus(o.optBoolean("servidor"), o.optBoolean("subindo"))
            }
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Pede ao vigia que LEVANTE o assistente. Autenticado — é o "só abre quando
     * autenticado" pedido pelo dono; sem o token o vigia devolve 401 e o app manda
     * a pessoa para a tela de configuração.
     *
     * ⚠ Devolve `RECUSADO` (e não simplesmente falso) no 401, porque as duas
     * situações pedem telas diferentes: token errado é problema de credencial e se
     * resolve configurando; falha de rede é esperar e tentar de novo.
     */
    fun vigiaAcordar(): PedidoVigia {
        val c = conf()
        if (c.base.isBlank()) return PedidoVigia.FALHOU
        val corpo = JSONObject().toString().toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url(Endereco.vigia(c.base, "/vigia/acordar"))
            .header("X-Mente-Token", c.token)
            .post(corpo)
            .build()
        return try {
            httpCurto.newCall(req).execute().use { r ->
                when {
                    r.code == 401 -> PedidoVigia.RECUSADO
                    r.isSuccessful -> PedidoVigia.ACEITO
                    else -> PedidoVigia.FALHOU
                }
            }
        } catch (e: Exception) {
            PedidoVigia.FALHOU
        }
    }

    // ---- identidade POR APARELHO -------------------------------------------
    /**
     * Troca o CÓDIGO DE PAREAMENTO pela credencial deste aparelho.
     *
     * `base` vem por parâmetro, como em `saude` e pela mesma razão: isto é
     * chamado da tela de CONFIGURAÇÃO, onde o endereço ainda está no campo e
     * pode nem ter sido salvo.
     *
     * ⚠ SEM o header `X-Mente-Token`, e isso é deliberado: esta é a única rota
     * sem gate (main.py, `parear_aparelho`) justamente por ser a porta de quem
     * ainda NÃO tem credencial. Mandar o token velho aqui não abriria nada e só
     * o exporia a mais um lugar.
     */
    fun parear(codigo: String, base: String = conf().base): Pareamento.Resultado {
        if (base.isBlank()) return Pareamento.Resultado.Falhou("sem endereço")
        val corpo = JSONObject().put("codigo", Pareamento.normalizarCodigo(codigo))
            .toString().toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url(Endereco.api(base, "/api/aparelhos/parear"))
            .post(corpo)
            .build()
        // `httpCurto` e não `http`: parear é uma consulta ao SQLite e responde em
        // milissegundos. Os 200 s de leitura do `http` existem para o `ligar`
        // carregar modelo; herdá-los aqui deixaria o dono olhando um botão
        // "Pareando…" por mais de três minutos quando o endereço estivesse errado.
        return try {
            httpCurto.newCall(req).execute().use { r ->
                Pareamento.lerResposta(r.code, r.body?.string().orEmpty())
            }
        } catch (e: Exception) {
            // ⚠ Só o que passa por AQUI é falha de rede. Um 401 com motivo não
            // levanta exceção no OkHttp — ele é RECUSA, sai pelo `lerResposta`, e
            // pede da tela uma frase completamente diferente.
            Pareamento.Resultado.Falhou(e.message ?: "sem resposta")
        }
    }

    /** Cliente de tempo curto: o vigia responde em milissegundos ou não está lá.
     *  O `http` normal tem 200 s de leitura por causa do `ligar`, e esperar isso
     *  para descobrir que não há vigia deixaria a tela de boot muda.
     *
     *  ⚠ O `callTimeout` é o teto do PEDIDO INTEIRO — DNS, TLS, redirecionamento
     *  e corpo. Sem ele, connect + read são tetos por ETAPA e uma sonda podia
     *  somar bem mais que a soma dos dois; é ele que garante a cadência do laço
     *  de boot, e não a boa vontade da rede. */
    private val httpCurto = OkHttpClient.Builder()
        .callTimeout(ORCAMENTO_SONDA_MS, TimeUnit.MILLISECONDS)
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(6, TimeUnit.SECONDS)
        .build()

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

    companion object {
        /**
         * Teto de UMA sonda de boot (health, vigia, pareamento), em ms.
         *
         * Não é preferência de estilo: o laço de boot roda as sondas em série, e
         * o que ele gasta aqui é o intervalo em que a tela não tem notícia nova.
         */
        const val ORCAMENTO_SONDA_MS = 8_000L
    }
}

/** Endereço + token, sem depender do Android — o que torna isto testável. */
data class Conf(val base: String, val token: String)

/** O que o vigia respondeu sobre o assistente. */
data class VigiaStatus(val servidorDePe: Boolean, val subindo: Boolean)

/** Resultado de pedir ao vigia que levante o assistente. */
enum class PedidoVigia { ACEITO, RECUSADO, FALHOU }

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

    // ---- o RELÓGIO da tela de boot -----------------------------------------
    /**
     * Segundos de PAREDE desde que a tela de boot abriu. Puro/testável.
     *
     * ⚠ Existe porque o que havia não era um relógio: o laço de boot fazia
     * `segundos += 1` a cada VOLTA e a tela exibia `× 0,7 s` — o valor do
     * `delay`, como se a volta custasse só ele. Só que cada volta faz de 1 a 3
     * chamadas de rede bloqueantes ANTES do delay, e com o servidor inalcançável
     * ela custa ~8 s. O número então mentia duas vezes ao mesmo tempo: ficava
     * congelado no intervalo da rede e contava um décimo do tempo real (medido
     * no Redmi em 2026-08-04 — "7s" parados por mais de dois minutos).
     *
     * Um número que só anda quando a rede responde descreve a REDE, não o tempo.
     * E quem olha a tela não lê "a rede está lenta", lê "travou".
     */
    fun segundosDecorridos(inicioMs: Long, agoraMs: Long): Int =
        ((agoraMs - inicioMs).coerceAtLeast(0L) / 1_000L).toInt()

    /**
     * Depois disto a tela oferece entrar assim mesmo e DIZ quem não subiu — a
     * mesma régua do `app.py`, que é a rede de segurança da degradação graciosa:
     * um serviço pode falhar no load e ficar `ready=False` PARA SEMPRE (cada
     * `load` do servidor tem pára-quedas próprio), e sem teto "esperar tudo"
     * vira deadlock.
     *
     * ⚠ Mora aqui, e não no laço, porque pendia do contador defeituoso: na régua
     * antiga eram 215 voltas para marcar "150 s", quase meia hora de parede num
     * endereço inalcançável. A saída existia e não chegava — que é o mesmo que
     * não existir para quem está olhando.
     */
    fun ofereceSaida(segundosDecorridos: Int): Boolean =
        segundosDecorridos >= SEGUNDOS_ATE_OFERECER_SAIDA

    const val SEGUNDOS_ATE_OFERECER_SAIDA = 150

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
