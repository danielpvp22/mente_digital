"""A INTERFACE do mensageiro em templates/index.html — o front é código, então é testável.

Por que um teste de HTML existe neste projeto: o canal usuário->mestre nasceu completo no
servidor (rota, tabela, entrega, ack, regra pura de privacidade) e MUDO na tela. O card
`{"tipo":"mensagem"}` chegava ao navegador, nenhum ramo o desenhava, e a suíte ficava
100% verde — o mesmo tipo de furo silencioso das 1.817 figuras que sumiriam da busca com
a suíte inteira passando. O que não é afirmado em lugar nenhum volta.

O que se trava aqui:

1. **O card é DESENHADO.** Um `else if` a menos e o recado vira nada, sem erro nenhum.
2. **O texto de outra pessoa não vira markup.** É o único conteúdo do app escrito por
   quem não é o dono nem o modelo. A bolha da IA passa por `parseMarkdown` (que já recusa
   imagem de URL externa de propósito); o recado não passa por markdown NENHUM.
3. **As rotas existem dos dois lados.** Front e `main.py` são arquivos distintos e nada
   os obriga a concordar: renomear uma rota deixaria o botão dando 404 em silêncio.
4. **Nada de rede de terceiros.** O `index.html` é 100% local (fontes do sistema, ícones
   inline) — um `<script src="https://cdn...">` acrescentado sem querer contradiz o pilar
   e não quebra teste nenhum enquanto houver internet.

Estes testes leem TEXTO, e isso tem limite: eles não executam o JavaScript, então provam
que a fiação existe, não que ela funciona. É a mesma régua do `test_marca.py` — afirmar o
que o diff esconde, não substituir o teste ao vivo.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
INDEX = RAIZ / "templates" / "index.html"
MAIN = RAIZ / "main.py"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def main_py() -> str:
    return MAIN.read_text(encoding="utf-8")


def corpo_da_funcao(js: str, nome: str) -> str:
    """O corpo de `function <nome>(...) { ... }`, casando as chaves.

    Grosseiro de propósito: um parser de JS aqui seria mais código para manter do que o
    que ele testa. Funciona porque as funções em questão não têm chave dentro de string
    nem template literal — e se um dia tiverem, o teste QUEBRA em vez de passar torto
    (o corpo sai truncado e as asserções de conteúdo falham).
    """
    inicio = js.index(f"function {nome}(")
    abre = js.index("{", inicio)
    nivel, i = 0, abre
    while i < len(js):
        if js[i] == "{":
            nivel += 1
        elif js[i] == "}":
            nivel -= 1
            if nivel == 0:
                return js[abre:i + 1]
        i += 1
    raise AssertionError(f"função {nome} sem fechamento — o corpo não pôde ser lido")


# --------------------------------------------------------------------------- #
# 1. O card chega e é desenhado                                                #
# --------------------------------------------------------------------------- #
def test_o_front_trata_o_card_de_mensagem(html):
    """O ramo que faltava. Sem ele o recado chega ao navegador e morre no `onmessage`."""
    assert 'data.tipo === "mensagem"' in html
    assert "mostrarRecado(" in html


def test_o_recado_nao_e_desenhado_como_bolha_do_assistente(html):
    """`appendMsg` desenha bolha de IA/usuário e passa o texto pelo markdown. Um recado
    por ali seria lido como o assistente falando — que é pior que não ter o canal."""
    corpo = corpo_da_funcao(html, "mostrarRecado")
    assert "appendMsg(" not in corpo
    assert "msg-row recado" in corpo


def test_a_fala_gemea_do_card_e_suprimida_mas_o_ack_continua(html):
    """O servidor manda card + bolha falada para o MESMO recado. Desenhar os dois o
    mostraria duas vezes, a segunda com a cara do assistente; engolir o ack junto faria o
    pendente ser reentregue para sempre, porque é ele que conclui a linha na fila."""
    assert "ehEcoDeRecado(" in html
    corpo = corpo_da_funcao(html, "ehEcoDeRecado")
    assert "recFalaPendente = null" in corpo          # consumo único
    # O ack sobrevive à supressão: procurado no ramo do proativo, junto do eco.
    ramo = html[html.index("if (ehEcoDeRecado(data.texto))"):]
    assert 'ack_proativo' in ramo[:600]


def test_o_rotulo_do_card_vem_de_classe_e_nao_do_envelope(html):
    """`card.tipo` vale "mensagem" (é o envelope que o WebSocket roteia); o rótulo que o
    mestre tria é `card.classe`. Ler o envelope daria "Recado" para todo relato de bug —
    ver `_card_mensagem` no scheduler, onde as duas chaves colidiam."""
    corpo = corpo_da_funcao(html, "mostrarRecado")
    assert "rotuloRecado(card.classe)" in corpo
    assert "rotuloRecado(card.tipo)" not in corpo


def test_rotulo_desconhecido_nao_chega_cru_na_tela(html):
    """O rótulo sai de uma lista FECHADA. Um `tipo` novo (ou inventado por um cliente)
    vira "Recado" em vez de ser impresso como veio."""
    corpo = corpo_da_funcao(html, "rotuloRecado")
    assert "REC_ROTULO[tipo] || REC_ROTULO.livre" in corpo


# --------------------------------------------------------------------------- #
# 2. Texto de outra pessoa é entrada hostil                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("funcao", ("mostrarRecado", "itemRecado"))
def test_o_texto_da_mensagem_entra_por_textContent(html, funcao):
    """A regra inteira em uma linha: `textContent`, nunca `innerHTML`/`parseMarkdown`.

    O texto vem de OUTRO USUÁRIO. O front já recusa de propósito markdown de imagem com
    URL externa (nota envenenada faria o navegador buscar servidor de fora); passar um
    recado pelo mesmo parser abriria de graça tudo o que aquela regra fecha."""
    corpo = corpo_da_funcao(html, funcao)
    assert "parseMarkdown" not in corpo
    assert "innerHTML" not in corpo
    assert re.search(r"corpo\.textContent = (card|m)\.texto", corpo)


def test_o_nome_de_quem_escreveu_tambem_e_texto(html):
    """O remetente é dado do banco, mas é dado que o CLIENTE originou. Ele entra pelo
    mesmo caminho do corpo — um `<img onerror>` num nome de usuário seria a mesma falha
    por uma porta menor."""
    for funcao, campo in (("mostrarRecado", "card.remetente"), ("itemRecado", "m.remetente")):
        corpo = corpo_da_funcao(html, funcao)
        assert campo in corpo
        assert f"innerHTML = {campo}" not in corpo


def test_a_lista_de_recados_e_montada_por_createElement(html):
    """`renderRecados` só usa innerHTML para o vazio/erro (texto do PRÓPRIO front); cada
    mensagem é montada em `itemRecado`, que é o testado acima. O que não pode voltar é
    uma linha montando o item por concatenação de string."""
    corpo = corpo_da_funcao(html, "renderRecados")
    assert "itemRecado(m)" in corpo
    assert "m.texto" not in corpo          # o texto não passa por aqui, e sim pelo item


# --------------------------------------------------------------------------- #
# 3. Front e servidor falam das MESMAS rotas                                   #
# --------------------------------------------------------------------------- #
# (fragmento que o front usa, decorador que o main.py declara)
ROTAS = [
    ("'/api/mensagens'", '@app.post("/api/mensagens"'),
    ("'/api/mensagens?limite=", '@app.get("/api/mensagens"'),
    ("'/api/mensagens/'", '@app.post("/api/mensagens/{msg_id}/lida"'),
    ("'/api/mestre/mensagens?limite=", '@app.get("/api/mestre/mensagens"'),
    ("'/api/mestre/mensagens/'", '@app.post("/api/mestre/mensagens/{msg_id}/responder"'),
]


@pytest.mark.parametrize("no_front,no_servidor", ROTAS)
def test_a_rota_que_o_front_chama_existe_no_servidor(html, main_py, no_front, no_servidor):
    """Front e `main.py` são arquivos diferentes e nada os obriga a concordar. Renomear
    uma rota deixa o botão dando 404 — e, como o front trata erro de HTTP, o sintoma é
    um toast educado em vez de uma falha visível em teste."""
    assert no_front in html, f"o front deixou de chamar {no_front}"
    assert no_servidor in main_py, f"o servidor não declara mais {no_servidor}"


def test_o_front_nao_inventa_destinatario(html):
    """`para`/`destinatario` no corpo do POST reabriria o canal usuário->usuário que o
    desenho fechou: o servidor deriva as pontas (mensageiro.destinatario_padrao e
    mensageiro.responder). O front manda texto e tipo, e nada mais."""
    assert "apiPost('/api/mensagens', { texto: txt, tipo: recTipo })" in html
    corpo = corpo_da_funcao(html, "caixaResposta")
    assert "destinatario" not in corpo and "para:" not in corpo


def test_erro_de_http_nao_vira_caixa_vazia(html):
    """O defeito de 2026-08-03 outra vez: 401 lido como "não há nada". A leitura passa
    pelo `apiGet` (que checa `res.ok`) e o catch mostra o `motivoFalha` — nunca a mesma
    tela de "nenhum recado" que um servidor saudável mostraria."""
    corpo = corpo_da_funcao(html, "carregarRecados")
    assert "apiGet(" in corpo and "fetch(" not in corpo
    assert "motivoFalha(err)" in corpo
    # E o crachá APAGA em vez de guardar um número que o servidor não confirmou.
    assert "atualizarCrachaRecados(null)" in corpo


def test_o_403_do_mestre_e_resposta_e_nao_falha(html):
    """É assim que o front descobre de que lado está, sem uma rota nova só para perguntar
    "eu sou o mestre?". Qualquer OUTRO status é falha de verdade e tem de subir."""
    corpo = corpo_da_funcao(html, "carregarRecados")
    assert "err.status !== 403" in corpo and "throw err" in corpo


def test_a_escrita_passa_por_apiPost_que_confere_o_status(html):
    """Sem checar `res.ok`, um 401 vira "enviado" e o relato de bug some sem rastro —
    exatamente o que o `enviarNotaEscrita` já teve de consertar uma vez."""
    corpo = corpo_da_funcao(html, "apiPost")
    assert "if (!res.ok) throw await erroHttp(res);" in corpo


# --------------------------------------------------------------------------- #
# 4. Fiação da interface                                                       #
# --------------------------------------------------------------------------- #
def test_toda_aba_do_painel_tem_a_secao_correspondente(html):
    """`abrirAba(nome)` acende a seção `av-<nome>`. Uma aba sem seção é um botão que
    apaga o painel inteiro e não mostra nada — e a 5ª aba (Recados) é justamente a nova."""
    abas = set(re.findall(r'data-aba="([a-z]+)"', html))
    secoes = set(re.findall(r'id="av-([a-z]+)"', html))
    assert "recados" in abas
    assert abas - secoes == set(), f"aba(s) sem seção: {abas - secoes}"


def test_o_cracha_de_nao_lidas_existe_e_e_lido_na_abertura(html):
    """O indicador de "há recado esperando". Ele mora na FAIXA (sempre visível): dentro
    do painel fechado, recado esperando é indistinguível de recado nenhum. E é pintado no
    boot, senão quem recebeu com o app fechado só descobriria abrindo o painel."""
    assert 'id="recados-cracha"' in html
    assert "carregarRecados(true);" in html
    corpo = corpo_da_funcao(html, "atualizarCrachaRecados")
    assert "el.hidden = !tem" in corpo


def test_o_botao_de_recados_vem_antes_do_avancado_na_faixa(html):
    """MEDIDO no navegador com a janela nativa (430 px) e a medida real de energia: como
    4º botão, o de recados termina em 449 px — o crachá fica FORA da tela, e um aviso que
    não se vê é o mesmo que não existir. Como 3º, termina em 414 px e cabe. Quem passa a
    rolar é o "Avançado", que não tem estado nenhum a mostrar."""
    assert html.index('id="sw-recados"') < html.index('id="sw-avancado"')


def test_o_cracha_conta_so_o_que_chegou_para_mim(html):
    """A mesma conta do `mensageiro.nao_lidas`: o que EU escrevi nunca está "não lido"
    para mim, e contá-lo faria o crachá pedir uma leitura que não existe."""
    corpo = corpo_da_funcao(html, "carregarRecados")
    assert "msgs.filter(m => !m.lida && m.destinatario === eu).length" in corpo


def test_o_botao_de_bug_abre_o_mesmo_canal_ja_como_problema(html):
    """Pedido do dono. Um formulário separado teria de reaprender entrega, não-lidas e
    resposta — o botão é um atalho para ESTE canal, não um segundo canal."""
    assert "reportarProblema()" in html
    corpo = corpo_da_funcao(html, "reportarProblema")
    assert "abrirRecados('bug')" in corpo


def test_responder_e_oferecido_so_a_quem_o_servidor_reconheceu_como_mestre(html):
    """A rota de resposta é só do mestre. `recEuSouMestre` começa `null` (= ainda não
    perguntei), e a comparação estrita é o que impede o botão aparecer nessa janela."""
    corpo = corpo_da_funcao(html, "itemRecado")
    assert "recEuSouMestre === true" in corpo


# --------------------------------------------------------------------------- #
# 5. 100% local                                                                #
# --------------------------------------------------------------------------- #
def test_a_pagina_nao_busca_nada_de_terceiros(html):
    """Fontes do sistema e ícones SVG inline, por decisão de 2026-07-24 (o
    fonts.googleapis.com via cada abertura da UI, e offline os ícones viravam texto cru).
    O `createElementNS` do SVG cita http://www.w3.org/2000/svg — é NAMESPACE, não busca —,
    por isso o teste olha os atributos que fazem o navegador sair para a rede."""
    proibido = re.findall(r'(?:src|href)\s*=\s*["\']https?://|@import|url\(\s*["\']?https?://',
                          html)
    assert proibido == [], f"referência externa no index.html: {proibido}"
