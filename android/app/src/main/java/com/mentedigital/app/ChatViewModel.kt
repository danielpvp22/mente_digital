package com.mentedigital.app

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.UUID

/** Uma bolha na tela. `emAndamento` marca a resposta que ainda está streamando. */
data class Bolha(
    val doUsuario: Boolean,
    val texto: String,
    val fontes: List<String> = emptyList(),
    val rota: String = "",
    val proativo: Boolean = false,
    val erro: Boolean = false,
    val emAndamento: Boolean = false,
)

class ChatViewModel(app: Application) : AndroidViewModel(app) {

    val ajustes = Ajustes(app)
    private val api = ApiMente { Conf(ajustes.base, ajustes.token) }

    val bolhas = mutableStateListOf<Bolha>()
    var conexao by mutableStateOf(Conexao.DESLIGADO)
        private set
    var detalheConexao by mutableStateOf("")
        private set
    var status by mutableStateOf("")
        private set
    var conversas by mutableStateOf<List<Conversa>>(emptyList())
        private set

    private var cliente: ClienteMente? = null

    init {
        if (ajustes.conversaId.isEmpty()) ajustes.conversaId = novoId()
    }

    // ----------------------------------------------------------------- ciclo --
    fun conectar() {
        if (!ajustes.configurado) return
        cliente?.desconectar(avisarFimDeSessao = false)
        cliente = ClienteMente(
            conf = Conf(ajustes.base, ajustes.token),
            conversaId = { ajustes.conversaId },
            aoReceber = { m -> viewModelScope.launch { tratar(m) } },
            aoMudarConexao = { c, d ->
                viewModelScope.launch { conexao = c; detalheConexao = d }
            },
        ).also { it.conectar() }
    }

    fun desconectar() = cliente?.desconectar()

    override fun onCleared() {
        cliente?.desconectar()
        super.onCleared()
    }

    // ---------------------------------------------------------------- enviar --
    fun enviar(texto: String) {
        val limpo = texto.trim()
        if (limpo.isEmpty()) return
        // ⚠ A bolha do usuário NÃO é desenhada aqui. O servidor devolve o texto
        // digitado como `transcricao` (ws.py:496), e desenhar no envio duplica a
        // mensagem — a regressão que index.html:779-784 documenta. O servidor é
        // a fonte da verdade; a tela espera o eco.
        if (cliente?.enviarTexto(limpo) != true) {
            bolhas += Bolha(false, "Não consegui enviar: sem conexão com o servidor.", erro = true)
        }
    }

    fun novaConversa() {
        val id = novoId()
        cliente?.enviar(Envio.encerrarSessao())
        cliente?.enviar(Envio.novaConversa(id))
        ajustes.conversaId = id
        bolhas.clear()
        status = ""
    }

    fun abrirConversa(id: String) {
        ajustes.conversaId = id
        bolhas.clear()
        cliente?.enviar(Envio.carregarConversa(id))
        // O `carregar_conversa` recarrega a RAM do SERVIDOR (ws.py:474-483) mas
        // não reenvia os turnos; quem desenha o histórico é a rota REST.
        viewModelScope.launch {
            val turnos = withContext(Dispatchers.IO) { api.conversa(id) }
            turnos.forEach { t ->
                if (t.pergunta.isNotEmpty()) bolhas += Bolha(true, t.pergunta)
                if (t.resposta.isNotEmpty()) bolhas += Bolha(false, t.resposta)
            }
        }
    }

    fun carregarConversas() {
        viewModelScope.launch {
            conversas = withContext(Dispatchers.IO) { api.conversas() }
        }
    }

    suspend fun testar(base: String): Saude = withContext(Dispatchers.IO) { api.saude(base) }

    // --------------------------------------------------------------- receber --
    /** O `when` é EXAUSTIVO de propósito: quando o servidor ganhar um tipo novo,
     *  o compilador cobra o tratamento aqui. */
    private fun tratar(m: MensagemServidor) {
        when (m) {
            is MensagemServidor.Transcricao -> {
                bolhas += Bolha(true, m.texto)
                status = ""
            }
            is MensagemServidor.Token -> anexarNaResposta(m.texto)
            is MensagemServidor.Status -> status = m.texto
            is MensagemServidor.Erro -> {
                fecharResposta()
                bolhas += Bolha(false, m.texto, erro = true)
                // Não é fim de turno: o servidor manda uma frase falada logo
                // atrás (agent.py:608-611). Só o estado da UI é resetado.
                status = ""
            }
            is MensagemServidor.Fontes -> {
                val i = bolhas.indexOfLast { !it.doUsuario }
                if (i >= 0) bolhas[i] = bolhas[i].copy(fontes = m.itens, rota = m.rota)
            }
            is MensagemServidor.Proativo -> {
                // O ack já saiu no ClienteMente, antes de a tela existir.
                bolhas += Bolha(false, m.texto, proativo = true)
            }
            is MensagemServidor.Navegar -> when (m.acao) {
                "nova_conversa" -> novaConversa()
                "carregar_conversa" -> m.id?.let { abrirConversa(it) }
                else -> Unit          // histórico/live: sem efeito na Fase 1
            }
            // Fase 2. Reconhecidos para não caírem em Desconhecida — e o
            // `barge_in` recebido NÃO gera envio (fecharia laço, ws.py:328-332).
            is MensagemServidor.Audio -> Unit
            MensagemServidor.BargeIn -> Unit
            is MensagemServidor.Desconhecida -> Unit
        }
    }

    private fun anexarNaResposta(pedaco: String) {
        status = ""
        val i = bolhas.indexOfLast { !it.doUsuario }
        if (i >= 0 && bolhas[i].emAndamento) {
            bolhas[i] = bolhas[i].copy(texto = bolhas[i].texto + pedaco)
        } else {
            bolhas += Bolha(false, pedaco, emAndamento = true)
        }
    }

    private fun fecharResposta() {
        val i = bolhas.indexOfLast { !it.doUsuario }
        if (i >= 0 && bolhas[i].emAndamento) bolhas[i] = bolhas[i].copy(emAndamento = false)
    }

    companion object {
        /** O id da conversa é gerado pelo CLIENTE (index.html:598), não pelo
         *  servidor — o app é dono do ciclo de vida dela. */
        fun novoId(): String = UUID.randomUUID().toString()
    }
}
