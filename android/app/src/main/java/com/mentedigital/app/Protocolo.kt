package com.mentedigital.app

import org.json.JSONException
import org.json.JSONObject

/**
 * O protocolo do `/ws/chat_live`, traduzido para tipos.
 *
 * PURO de propósito: nada aqui toca rede, Android ou disco, então roda em teste
 * de JVM sem emulador. É a parte do app que mais barato dá para provar, e a que
 * mais silenciosamente quebraria — o servidor IGNORA `tipo` desconhecido sem
 * erro (ws.py:426-502 não tem `else`), então escrever um tipo errado não devolve
 * nada: só deixa de funcionar.
 *
 * `sealed interface` para o `when` do app ser EXAUSTIVO: quando o servidor
 * ganhar um tipo novo, o compilador cobra o tratamento em vez de o app ignorar
 * calado — que é exatamente o que o servidor faz e o que se quer evitar aqui.
 */
sealed interface MensagemServidor {
    /** O que o servidor entendeu que o usuário disse. **Também chega para texto
     *  DIGITADO** (eco, ws.py:496) — e é por isso que a bolha do usuário se
     *  desenha a partir DAQUI, nunca localmente: desenhar no envio duplica a
     *  mensagem (a regressão documentada em index.html:779-784). */
    data class Transcricao(val texto: String) : MensagemServidor

    /** Pedaço da resposta, para concatenar na ordem. Turno digitado vem palavra a
     *  palavra; turno falado, frase a frase (respostas.py:236). O app não precisa
     *  saber qual é qual. */
    data class Token(val texto: String) : MensagemServidor

    /** Aviso efêmero ("Modelo religando…"). Não é resposta. */
    data class Status(val texto: String) : MensagemServidor

    /** Exceção no pipeline. **Não é fim de turno**: o servidor manda `erro` E uma
     *  frase falada de desculpa logo atrás (agent.py:608-611). Trata-se como
     *  reset de estado da UI. */
    data class Erro(val texto: String) : MensagemServidor

    /** Proveniência. `itens` são strings COM PREFIXO — "memoria",
     *  "nota:<arquivo>.md", "web:<dominio>" — não objetos. */
    data class Fontes(val rota: String, val itens: List<String>) : MensagemServidor

    /** Push do servidor (lembrete/watcher/briefing). Se trouxer `ackId`, o ack é
     *  OBRIGATÓRIO: sem ele o scheduler não conclui nem reprograma, e reentrega
     *  na próxima conexão (ws.py:441-446). */
    data class Proativo(val texto: String, val ackId: String?) : MensagemServidor

    /** O servidor mandando operar a UI (nova_conversa, abrir_historico, …). */
    data class Navegar(val acao: String, val id: String?) : MensagemServidor

    /** WAV completo em base64, uma mensagem por frase. Fase 2 — aqui só se
     *  reconhece para não cair em `Desconhecida`. */
    data class Audio(val base64: String) : MensagemServidor

    /** "Esvazie sua fila de áudio". ⚠ O MESMO nome existe no sentido oposto
     *  (cliente→servidor = "cancele o pipeline"), com semântica DIFERENTE.
     *  Receber isto NÃO pode gerar um envio: fecha laço cliente↔servidor
     *  (ws.py:328-332, index.html:884-885). */
    data object BargeIn : MensagemServidor

    /** Tipo que este app ainda não conhece. Existe para o `when` continuar
     *  exaustivo sem o app fingir que a mensagem não chegou. */
    data class Desconhecida(val tipo: String) : MensagemServidor
}

/** Traduz um quadro de texto do WebSocket. `null` = JSON inválido (o servidor
 *  faz o mesmo com o nosso: warn e ignora, ws.py:429-431). */
fun parseMensagem(bruto: String): MensagemServidor? {
    val o = try {
        JSONObject(bruto)
    } catch (e: JSONException) {
        return null
    }
    return when (val tipo = o.optString("tipo")) {
        "transcricao" -> MensagemServidor.Transcricao(o.optString("texto"))
        "token" -> MensagemServidor.Token(o.optString("texto"))
        "status" -> MensagemServidor.Status(o.optString("texto"))
        "erro" -> MensagemServidor.Erro(o.optString("texto"))
        "audio" -> MensagemServidor.Audio(o.optString("base64"))
        "barge_in" -> MensagemServidor.BargeIn
        "fontes" -> {
            val arr = o.optJSONArray("itens")
            val itens = ArrayList<String>(arr?.length() ?: 0)
            for (i in 0 until (arr?.length() ?: 0)) itens.add(arr!!.optString(i))
            MensagemServidor.Fontes(o.optString("rota"), itens)
        }
        // `optString` devolve "" para ausente, não null — e "" seria um ack_id
        // válido de mentira, que faria o app confirmar um lembrete inexistente.
        "proativo" -> MensagemServidor.Proativo(o.optString("texto"), o.optString("ack_id").ifEmpty { null })
        "navegar" -> MensagemServidor.Navegar(o.optString("acao"), o.optString("id").ifEmpty { null })
        else -> MensagemServidor.Desconhecida(tipo)
    }
}

/**
 * Os quadros que o app ENVIA. Montados aqui, num lugar só, porque um `tipo`
 * escrito errado não devolve erro nenhum — o servidor ignora em silêncio.
 */
object Envio {
    fun texto(payload: String): String =
        JSONObject().put("tipo", "texto").put("payload", payload).toString()

    fun setConversa(id: String): String =
        JSONObject().put("tipo", "set_conversa").put("id", id).toString()

    fun novaConversa(id: String): String =
        JSONObject().put("tipo", "nova_conversa").put("id", id).toString()

    fun carregarConversa(id: String): String =
        JSONObject().put("tipo", "carregar_conversa").put("id", id).toString()

    fun ackProativo(id: String): String =
        JSONObject().put("tipo", "ack_proativo").put("id", id).toString()

    fun encerrarSessao(): String =
        JSONObject().put("tipo", "end_session").toString()
}
