package com.mentedigital.app

import org.json.JSONObject

/**
 * A troca do CÓDIGO DE PAREAMENTO pela credencial deste aparelho — as partes
 * PURAS. Quem abre socket é `Servidor.parear`.
 *
 * Por que existe: o campo "Token de acesso" ao lado guarda um segredo ÚNICO,
 * compartilhado por todos os celulares. O cabeçalho de `mente_digital/aparelhos.py`
 * é explícito sobre o que isso custa — o servidor não distingue um aparelho do
 * outro, não tem teto, e não revoga um sem derrubar todos. O pareamento resolve
 * isso sem tela nova: o dono gera um código de 10 caracteres no PC, digita-o
 * aqui, e o app recebe de volta uma credencial `mdk1.<id>.<segredo>` que ocupa
 * exatamente o MESMO slot do token de hoje (header `X-Mente-Token`). Daí isto
 * ser conveniência, não desbloqueio: sem este campo, dá para colar a credencial
 * à mão no campo de cima.
 */
object Pareamento {

    const val TAMANHO = 10

    // ⚠ Os valores foram COPIADOS de `mente_digital/aparelhos.py`, não deduzidos
    // do nome da constante. Lá o teto se chama `MOTIVO_TETO` mas VALE
    // "teto_aparelhos" — quem copiasse o nome cairia no ramo de motivo
    // desconhecido e o dono leria um texto genérico no lugar da única frase que
    // resolve o problema dele ("revogue um aparelho antigo").
    const val CODIGO_INVALIDO = "codigo_invalido"
    const val CODIGO_EXPIRADO = "codigo_expirado"
    const val CODIGO_USADO = "codigo_usado"
    const val TETO = "teto_aparelhos"
    const val BLOQUEADO = "bloqueado"

    /**
     * Os três desfechos possíveis — e a distinção entre os dois últimos é o
     * ponto deste arquivo: RECUSA não é falha de REDE.
     *
     * O app já paga essa lição em dois lugares (`PedidoVigia.RECUSADO` vs
     * `FALHOU`, e o `Saude.alcancavel`). Ela vale aqui em dobro porque as ações
     * são opostas: recusa se resolve com OUTRO código, falha de rede se resolve
     * esperando e tentando de novo com o MESMO.
     */
    sealed interface Resultado {
        data class Ok(val credencial: String, val aparelhoId: String) : Resultado
        data class Recusado(val motivo: String) : Resultado
        data class Falhou(val detalhe: String) : Resultado
    }

    /**
     * Espelho de `aparelhos.normalizar_codigo`: MAIÚSCULAS e sem separador — o
     * dono lê o código na tela do PC e digita no celular, então "abcd-2345" e
     * "ABCD 2345" têm de ser o mesmo código.
     *
     * ⚠ O que NÃO se faz aqui, e o servidor documenta o porquê: consertar sósia.
     * Trocar um "O" digitado por "0" seria ADIVINHAR parte de um segredo —
     * exatamente o que aquele módulo existe para impedir. As sósias já saíram do
     * alfabeto na emissão (não há 0/O nem 1/I/L); o que sobrar fora dele falha
     * como inválido, e é assim que tem de ser.
     *
     * `uppercase()` sem argumento é `Locale.ROOT` em Kotlin — e é o que faz esta
     * função concordar com a do servidor caractere a caractere, porque o `.upper()`
     * do Python também é independente de locale. O `toUpperCase()` do Java seguiria
     * o locale do APARELHO (em turco, "i" vira "İ"), e duas normalizações que
     * divergem em QUALQUER símbolo transformam código bom em recusa.
     */
    fun normalizarCodigo(bruto: String): String =
        bruto.uppercase().filter { it in '0'..'9' || it in 'A'..'Z' }

    /**
     * Só um código do tamanho certo vai à rede.
     *
     * Não é firula de interface: um código incompleto o servidor conta como
     * FALHA, e a partir da terceira falha seguida entra o castigo progressivo
     * (`aparelhos.atraso_bloqueio`, que DOBRA a cada erro). Quem tropeçasse no
     * teclado ficaria de castigo sem nunca ter errado o código.
     */
    fun completo(bruto: String): Boolean = normalizarCodigo(bruto).length == TAMANHO

    /**
     * A linha de apoio do campo. Existe para o botão nunca ficar desligado em
     * SILÊNCIO: quem ainda não pode clicar lê no contador o que falta.
     */
    fun dica(bruto: String): String {
        val n = normalizarCodigo(bruto).length
        return when {
            n == 0 -> "$TAMANHO caracteres, gerados no PC do dono. Espaço e hífen podem entrar."
            n < TAMANHO -> "$n de $TAMANHO caracteres."
            n == TAMANHO -> "Pronto para trocar pela credencial deste aparelho."
            else -> "$n caracteres — são $TAMANHO. Confira se não colou duas vezes."
        }
    }

    /**
     * O que o servidor devolveu, virado em desfecho. PURO — recebe o status e o
     * TEXTO, não a resposta do OkHttp, e é por isso que dá para provar cada ramo
     * sem servidor nenhum.
     *
     * ⚠ A chave do erro é `erro`, NÃO `motivo` (main.py, `parear_aparelho`:
     * `content={"erro": r.motivo}` — o nome do campo Python virou o VALOR, e o
     * nome da chave é outro). Ler a chave errada devolveria motivo vazio em toda
     * recusa e o app cairia sempre no texto genérico.
     */
    fun lerResposta(status: Int, corpo: String): Resultado {
        val o = try { JSONObject(corpo) } catch (e: Exception) { null }
        if (status in 200..299) {
            val credencial = o?.optString("credencial").orEmpty()
            // Um 200 sem credencial não é recusa: é OUTRA COISA atendendo neste
            // endereço (portal de rede, proxy, servidor antigo sem a rota). Cai
            // em `Falhou` porque a ação é conferir o ENDEREÇO, não o código.
            if (credencial.isEmpty()) return Resultado.Falhou("resposta sem credencial")
            return Resultado.Ok(credencial, o?.optString("aparelho_id").orEmpty())
        }
        // Corpo ilegível num status de erro ainda é RECUSA — o servidor falou.
        // Guardar o status como motivo mantém a tela acionável em vez de virar
        // um "erro desconhecido" mudo, que é o defeito que este ramo evita.
        val motivo = o?.optString("erro").orEmpty()
        return Resultado.Recusado(motivo.ifEmpty { "http_$status" })
    }

    /** A manchete do cartão: é ela que diz, de relance, se o problema é o CÓDIGO
     *  ou a REDE. As três são distintas de propósito. */
    fun titulo(r: Resultado): String = when (r) {
        is Resultado.Ok -> "Aparelho pareado"
        is Resultado.Recusado -> "O servidor recusou o código"
        is Resultado.Falhou -> "Não deu para completar o pareamento"
    }

    /**
     * A frase que diz o que FAZER. Cada motivo do servidor tem a sua, porque as
     * saídas são diferentes: "expirado" e "usado" pedem outro código ao dono,
     * "teto" pede que ele revogue um aparelho, "bloqueado" pede espera, e
     * "inválido" pede conferir a digitação. "Falhou" seria uma resposta errada
     * para qualquer um dos cinco.
     */
    fun mensagem(r: Resultado): String = when (r) {
        is Resultado.Ok ->
            "A credencial deste aparelho já está guardada no campo de token, acima. " +
                "Ela vale só para este celular, e o dono pode revogá-la no PC sem " +
                "mexer nos outros aparelhos."

        is Resultado.Falhou ->
            "O servidor não completou o pareamento (${r.detalhe}). Confira o endereço, se o " +
                "Mente Digital está aberto no PC e se o celular alcança essa rede. Tente de " +
                "novo: se então ele disser que o código já foi usado, é porque o pareamento " +
                "completou do outro lado e basta pedir um novo código ao dono."

        // Motivo que esta versão não conhece é MOSTRADO, não engolido: um app
        // mais velho que o servidor tem de deixar o dono ler o que aconteceu.
        is Resultado.Recusado -> MENSAGENS[r.motivo]
            ?: ("O servidor recusou com um motivo que esta versão do app não conhece " +
                "(${r.motivo}). Atualize o app, ou peça ao dono para ver a trilha de " +
                "auditoria no PC.")
    }

    private val MENSAGENS = mapOf(
        CODIGO_EXPIRADO to
            "Esse código venceu. Peça outro ao dono — ele vale poucos minutos de propósito: " +
                "é a peça de baixa entropia do sistema, porque é a única digitada à mão.",

        CODIGO_USADO to
            "Esse código já virou a credencial de algum aparelho. Cada um serve UMA vez; " +
                "peça um novo ao dono.",

        // O servidor também usa `codigo_invalido` quando falha ao ler ou gravar
        // no banco. A frase serve aos dois casos: conferir e tentar de novo.
        CODIGO_INVALIDO to
            "Esse código não existe neste servidor. Confira caractere por caractere — o " +
                "alfabeto não tem 0, O, 1, I nem L, então o que parecer um deles é outro " +
                "símbolo.",

        TETO to
            "O servidor já está no limite de aparelhos autorizados. O dono precisa revogar " +
                "um dos antigos no PC antes de este entrar.",

        BLOQUEADO to
            "Tentativas erradas demais vindas deste celular: o servidor pôs um castigo de " +
                "alguns minutos, e ele DOBRA a cada novo erro. Espere, e volte só com o " +
                "código já em mãos.",
    )
}
