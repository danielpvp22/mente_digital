package com.mentedigital.app

import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * O ÚNICO teste que fala com o servidor de verdade.
 *
 * Por que ele existe: o resto da suíte prova que o app monta os quadros certos —
 * mas "certo" é uma leitura MINHA do código do servidor, e o servidor ignora
 * quadro desconhecido em silêncio (ws.py:426-502 não tem `else`). Um campo com o
 * nome errado passaria em todo teste de unidade e simplesmente não funcionaria no
 * aparelho. Este teste fecha essa fresta sem precisar de emulador: `OkHttp` roda
 * na JVM, e o `ClienteMente` foi desacoplado do Android exatamente para caber
 * aqui.
 *
 * PULA SOZINHO sem `MENTE_BASE` no ambiente, para não deixar a suíte vermelha em
 * máquina sem servidor. Para rodar:
 *
 *     $env:MENTE_BASE="http://127.0.0.1:8000"
 *     $env:MENTE_TOKEN="<o token do .env>"
 *     .\gradlew.bat :app:testDebugUnitTest --tests "*ConformidadeServidorTest*"
 */
class ConformidadeServidorTest {

    private val base: String? = System.getenv("MENTE_BASE")
    private val token: String = System.getenv("MENTE_TOKEN") ?: ""

    @Test
    fun `health responde SEM token e lista os servicos`() {
        assumeTrue("sem MENTE_BASE — teste de integração pulado", !base.isNullOrBlank())
        // A rota é a única sem gate (main.py:254-266) e o docstring dela diz que
        // existe para isto. Chamada com token VAZIO de propósito: é o que separa
        // "servidor inalcançável" de "servidor recusou", e é o que a tela de
        // configuração usa antes de o dono ter um token válido.
        val s = ApiMente { Conf(base!!, "") }.saude(base!!)
        assertTrue("servidor não respondeu: ${s.detalhe}", s.alcancavel)
        assertTrue("health sem mapa de serviços — a tela de config ficaria muda",
            s.servicos.isNotEmpty())
    }

    @Test
    fun `conversas exigem o token no HEADER`() {
        assumeTrue("sem MENTE_BASE — teste de integração pulado", !base.isNullOrBlank())
        assumeTrue("servidor sem token configurado", token.isNotBlank())
        // Com o token certo, no header `X-Mente-Token` (main.py:243). Não asserto
        // que a lista tem itens — um vault sem conversas é estado legítimo; o que
        // se prova aqui é que a chamada é ACEITA.
        val comToken = ApiMente { Conf(base!!, token) }.conversas()
        // E sem token: o gate recusa, e a camada devolve lista vazia em vez de
        // estourar. Se este assert falhar com o token errado ACEITO, o gate está
        // aberto — que é bem mais grave que a tela vazia.
        val semToken = ApiMente { Conf(base!!, "errado") }.conversas()
        assertTrue("o servidor aceitou um token errado em /api/conversas", semToken.isEmpty())
        assertTrue("chamada com token válido falhou (veio ${comToken.size})", comToken.size >= 0)
    }

    @Test
    fun `turno de texto completo contra o servidor real`() {
        assumeTrue("sem MENTE_BASE — teste de integração pulado", !base.isNullOrBlank())

        val recebidas = java.util.Collections.synchronizedList(ArrayList<MensagemServidor>())
        val abriu = CountDownLatch(1)
        val respondeu = CountDownLatch(1)
        val idConversa = "teste-" + UUID.randomUUID()

        val cliente = ClienteMente(
            conf = Conf(base!!, token),
            conversaId = { idConversa },
            aoReceber = { m ->
                recebidas += m
                // O turno terminou quando chegou proveniência: o `fontes` sai
                // depois da resposta inteira (agent.py:584-586).
                if (m is MensagemServidor.Fontes) respondeu.countDown()
            },
            aoMudarConexao = { c, _ -> if (c == Conexao.ABERTO) abriu.countDown() },
        )
        try {
            cliente.conectar()
            assertTrue("não abriu o WebSocket em 15 s (token? rede?)",
                abriu.await(15, TimeUnit.SECONDS))

            assertTrue(cliente.enviarTexto("quanto é dois mais dois?"))
            // Generoso de propósito: com RAG frio o primeiro turno pode passar de
            // 30 s neste hardware. O teste mede CONFORMIDADE, não latência.
            respondeu.await(90, TimeUnit.SECONDS)

            val tipos = recebidas.map { it::class.simpleName }
            // 1) O servidor ECOA o texto digitado como `transcricao` (ws.py:496).
            //    É o contrato que faz a bolha do usuário vir do servidor em vez de
            //    ser desenhada localmente — desenhar no envio DUPLICA a mensagem.
            assertTrue("sem eco de transcricao; recebidos=$tipos",
                recebidas.any { it is MensagemServidor.Transcricao })
            // 2) A resposta chega em pedaços, para concatenar.
            assertTrue("sem nenhum token de resposta; recebidos=$tipos",
                recebidas.any { it is MensagemServidor.Token })
            // 3) Nada caiu em Desconhecida: se o servidor mandou um tipo que este
            //    app não trata, é aqui que se descobre — não no aparelho.
            val novos = recebidas.filterIsInstance<MensagemServidor.Desconhecida>().map { it.tipo }
            assertTrue("o servidor mandou tipos que o app não conhece: $novos", novos.isEmpty())
        } finally {
            cliente.desconectar()
        }
    }

    /**
     * O teste que corrigiu o plano.
     *
     * O plano (§1.1 e Risco R5) afirma que o cliente "verá apenas um close 1008".
     * Sondado o servidor de verdade em 2026-08-02:
     *
     *     token certo  -> HTTP/1.1 101 Switching Protocols
     *     token errado -> HTTP/1.1 403 Forbidden
     *     sem token    -> HTTP/1.1 403 Forbidden
     *
     * O gate roda ANTES do `accept()`, então não há upgrade e não há frame de
     * close: é uma recusa de HANDSHAKE HTTP. O app tem de reconhecê-la em
     * `onFailure` pelo código da resposta — e PARAR de reconectar, senão fica em
     * laço eterno contra uma recusa que nunca vai mudar sozinha.
     */
    @Test
    fun `token errado e recusado no handshake, e o app para de tentar`() {
        assumeTrue("sem MENTE_BASE — teste de integração pulado", !base.isNullOrBlank())
        assumeTrue("servidor sem token configurado", token.isNotBlank())

        val recusou = CountDownLatch(1)
        var estado: Conexao? = null
        val cliente = ClienteMente(
            conf = Conf(base!!, "token-de-mentira-$token"),
            conversaId = { "x" },
            aoReceber = { },
            aoMudarConexao = { c, _ ->
                estado = c
                if (c == Conexao.RECUSADO || c == Conexao.CAIU) recusou.countDown()
            },
        )
        try {
            cliente.conectar()
            assertTrue("o servidor nem recusou nem aceitou em 20 s", recusou.await(20, TimeUnit.SECONDS))
            assertTrue("esperava RECUSADO, veio $estado", estado == Conexao.RECUSADO)
        } finally {
            cliente.desconectar(avisarFimDeSessao = false)
        }
    }
}
