package com.mentedigital.app

import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A troca do código pela credencial, na parte que não depende de rede.
 *
 * Duas armadilhas justificam o arquivo, e as duas já cobraram preço neste
 * projeto. A primeira é NOME DE CAMPO DEDUZIDO em vez de medido (o `tts`/`voz`
 * do `Boot.MARCOS`): aqui o corpo de erro traz a chave `erro`, e o VALOR dela é
 * que se chama motivo. A segunda é RECUSA LIDA COMO FALHA DE REDE — o defeito
 * que abriu 2026-08-03, quando um 401 virou "nenhuma conversa ainda" no front.
 *
 * ⚠ Os valores de motivo aqui são LITERAIS de propósito, copiados de
 * `mente_digital/aparelhos.py`. Comparar constante com constante passaria mesmo
 * se o valor estivesse errado — provaria só que a constante é igual a si mesma.
 */
class PareamentoTest {

    // --- normalização ---------------------------------------------------------

    @Test
    fun `maiusculiza e tira separador, como o servidor faz`() {
        // `aparelhos.normalizar_codigo`: re.sub(r"[^0-9A-Z]", "", bruto.upper())
        assertEquals("A3K72M9PXR", Pareamento.normalizarCodigo("a3k7-2m9p-xr"))
        assertEquals("A3K72M9PXR", Pareamento.normalizarCodigo("  A3K7 2M9P XR  "))
        assertEquals("A3K72M9PXR", Pareamento.normalizarCodigo("a3k7.2m9p/xr"))
    }

    @Test
    fun `NAO conserta sosia — O continua O e nunca vira zero`() {
        // A regra de segurança do módulo: trocar um "O" digitado por "0" seria o
        // app ADIVINHANDO parte de um segredo. As sósias já saíram do alfabeto na
        // emissão; o que vier fora dele tem de falhar como inválido, não ser
        // "corrigido" para dentro.
        assertEquals("OOOO", Pareamento.normalizarCodigo("oooo"))
        assertEquals("IL1", Pareamento.normalizarCodigo("il1"))
        assertNotEquals("0000", Pareamento.normalizarCodigo("OOOO"))
    }

    @Test
    fun `nao depende do locale do aparelho`() {
        // Kotlin `uppercase()` é Locale.ROOT, como o `.upper()` do Python. Com o
        // `toUpperCase()` do Java este teste falharia em turco — e duas
        // normalizações que divergem em um símbolo viram recusa de código bom.
        val antes = Locale.getDefault()
        try {
            Locale.setDefault(Locale.forLanguageTag("tr"))
            assertEquals("ABCI", Pareamento.normalizarCodigo("abci"))
        } finally {
            Locale.setDefault(antes)
        }
    }

    // --- guarda local: código incompleto não vai à rede -----------------------

    @Test
    fun `so o tamanho exato esta completo`() {
        // Não é firula: código curto o servidor conta como FALHA, e da terceira
        // seguida em diante entra o castigo que DOBRA (atraso_bloqueio). Quem
        // tropeça no teclado ficaria bloqueado sem ter errado o código.
        assertTrue(Pareamento.completo("A3K7-2M9P-XR"))     // 10 depois de limpar
        assertFalse(Pareamento.completo("A3K72M9PX"))       // 9
        assertFalse(Pareamento.completo("A3K72M9PXR7"))     // 11
        assertFalse(Pareamento.completo(""))
        assertFalse(Pareamento.completo("----------"))      // só separador: 0
    }

    @Test
    fun `a dica conta o que falta, para o botao nunca ficar mudo`() {
        assertTrue(Pareamento.dica("").contains("10"))
        assertEquals("7 de 10 caracteres.", Pareamento.dica("a3k7-2m9"))
        assertTrue(Pareamento.dica("A3K72M9PXR").contains("Pronto"))
        assertTrue(Pareamento.dica("A3K72M9PXR77").contains("12"))
    }

    // --- leitura da resposta --------------------------------------------------

    @Test
    fun `200 devolve a credencial e o id`() {
        // O cast é a própria asserção: qualquer outro desfecho estoura aqui.
        val r = Pareamento.lerResposta(200,
            """{"credencial":"mdk1.0a1b2c3d4e5f6071.SEGREDO_urlsafe_de_32_bytes",
                "aparelho_id":"0a1b2c3d4e5f6071"}""") as Pareamento.Resultado.Ok
        assertEquals("mdk1.0a1b2c3d4e5f6071.SEGREDO_urlsafe_de_32_bytes", r.credencial)
        assertEquals("0a1b2c3d4e5f6071", r.aparelhoId)
    }

    @Test
    fun `o motivo vem da chave 'erro', nao de 'motivo'`() {
        // main.py, parear_aparelho: JSONResponse(401, {"erro": r.motivo}). O nome
        // do campo Python virou o VALOR; a CHAVE é outra. Ler "motivo" devolveria
        // vazio em toda recusa e o app cairia sempre no texto genérico.
        val r = Pareamento.lerResposta(401, """{"erro":"codigo_expirado"}""")
        assertEquals(Pareamento.Resultado.Recusado("codigo_expirado"), r)
    }

    @Test
    fun `401 e RECUSA, nunca falha de rede — nem com o corpo ilegivel`() {
        // O defeito de 2026-08-03 pelo avesso. As duas ações são opostas: recusa
        // pede OUTRO código, rede pede repetir o MESMO. Um corpo que não é JSON
        // ainda é o servidor falando, então continua sendo recusa — e o status
        // entra no motivo para a tela não virar um "erro desconhecido" mudo.
        val r = Pareamento.lerResposta(401, "<html>proxy</html>")
        assertTrue(r is Pareamento.Resultado.Recusado)
        assertFalse(r is Pareamento.Resultado.Falhou)
        assertTrue(Pareamento.mensagem(r).contains("401"))
    }

    @Test
    fun `200 sem credencial e falha de ENDERECO, nao recusa`() {
        // Só outra coisa atende neste endereço (portal de rede, proxy, servidor
        // antigo). A ação é conferir o endereço — não adianta pedir outro código.
        assertEquals(Pareamento.Resultado.Falhou("resposta sem credencial"),
            Pareamento.lerResposta(200, """{"status":"ok"}"""))
        assertTrue(Pareamento.lerResposta(200, "bem-vindo ao roteador")
            is Pareamento.Resultado.Falhou)
    }

    @Test
    fun `o titulo separa recusa de rede`() {
        assertNotEquals(
            Pareamento.titulo(Pareamento.Resultado.Recusado("codigo_usado")),
            Pareamento.titulo(Pareamento.Resultado.Falhou("timeout")),
        )
    }

    // --- motivo → mensagem acionável ------------------------------------------

    @Test
    fun `cada motivo do servidor tem a sua propria frase`() {
        // Os cinco que `RegistroAparelhos.parear` pode devolver. Um mapeamento
        // faltando não daria erro nenhum: cairia no texto genérico, e o dono
        // leria "atualize o app" quando a saída real era "revogue um aparelho".
        val motivos = listOf(
            "codigo_invalido", "codigo_expirado", "codigo_usado",
            "teto_aparelhos", "bloqueado",
        )
        val frases = motivos.map { Pareamento.mensagem(Pareamento.Resultado.Recusado(it)) }
        frases.forEach { assertFalse(it.contains("não conhece")) }
        assertTrue("cada motivo precisa de frase PRÓPRIA", frases.toSet().size == motivos.size)
    }

    @Test
    fun `a frase diz o que FAZER, nao so que falhou`() {
        fun frase(motivo: String) =
            Pareamento.mensagem(Pareamento.Resultado.Recusado(motivo)).lowercase()

        // Expirado: a saída é pedir outro ao dono, e saber que ele é curto de vida.
        assertTrue(frase("codigo_expirado").contains("peça outro"))
        assertTrue(frase("codigo_expirado").contains("minutos"))
        // Usado: também é pedir outro — mas pelo motivo oposto (uso único).
        assertTrue(frase("codigo_usado").contains("uma vez"))
        // Teto: nenhum código novo resolve; quem age é o dono, no PC.
        assertTrue(frase("teto_aparelhos").contains("revogar"))
        // Bloqueado: a saída é ESPERAR, não repetir.
        assertTrue(frase("bloqueado").contains("espere"))
        // Inválido: conferir a digitação, sabendo quais símbolos não existem.
        assertTrue(frase("codigo_invalido").contains("confira"))
    }

    @Test
    fun `motivo desconhecido aparece, em vez de virar erro mudo`() {
        // Um app mais velho que o servidor tem de deixar o dono LER o que houve.
        val m = Pareamento.mensagem(Pareamento.Resultado.Recusado("motivo_do_futuro"))
        assertTrue(m.contains("motivo_do_futuro"))
    }

    @Test
    fun `sucesso diz onde a credencial foi parar`() {
        val m = Pareamento.mensagem(Pareamento.Resultado.Ok("mdk1.a.b", "a"))
        assertTrue(m.contains("token"))
    }

    @Test
    fun `falha de rede nao promete que o codigo sobreviveu`() {
        // Não dá para saber se o servidor chegou a atender antes de a resposta se
        // perder. Prometer "o código continua valendo" seria mentira metade das
        // vezes; a frase manda tentar de novo e explica o que significa se então
        // vier "já usado".
        val m = Pareamento.mensagem(Pareamento.Resultado.Falhou("timeout")).lowercase()
        assertTrue(m.contains("tente de novo"))
        assertTrue(m.contains("já foi usado"))
        assertTrue(m.contains("timeout"))
    }
}
