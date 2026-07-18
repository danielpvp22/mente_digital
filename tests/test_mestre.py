"""
Testes do fluxo da palavra-mestre (mestre.py): detecção + parser rápido determinístico.
Puro/testável — `agora` injetado, sem LLM/DB.
"""
from datetime import datetime

import mestre

AGORA = datetime(2026, 7, 17, 14, 30, 0)


# -- separar (detecção da palavra-mestre como 1ª palavra) ----------------------
def test_separar_reconhece_primeira_palavra():
    assert mestre.separar("mestre, adiciona pão", "mestre") == "adiciona pão"
    assert mestre.separar("Mestre adiciona pão", "mestre") == "adiciona pão"
    assert mestre.separar("MESTRE: liste", "mestre") == "liste"


def test_separar_so_a_palavra():
    assert mestre.separar("mestre", "mestre") == ""
    assert mestre.separar("mestre.", "mestre") == ""


def test_separar_nao_ativa_no_meio_nem_prefixo():
    assert mestre.separar("qual a capital?", "mestre") is None
    assert mestre.separar("mestrado é legal", "mestre") is None       # não é a palavra
    assert mestre.separar("o mestre mandou", "mestre") is None        # não é a 1ª palavra


def test_separar_palavra_configuravel():
    assert mestre.separar("roberto, liste", "roberto") == "liste"
    assert mestre.separar("mestre, liste", "roberto") is None


# -- parse_rapido: LISTAS ------------------------------------------------------
def test_lista_add_multiplos_itens():
    acoes = mestre.parse_rapido("adicionar a lista de compras leite, farinha e ovos", AGORA)
    assert [a.tool for a in acoes] == ["adicionar_item"] * 3
    assert [a.args["item"] for a in acoes] == ["leite", "farinha", "ovos"]
    assert all(a.args["lista"] == "compras" for a in acoes)


def test_lista_add_item_antes_da_lista():
    acoes = mestre.parse_rapido("adiciona pão na lista", AGORA)
    assert len(acoes) == 1
    assert acoes[0].tool == "adicionar_item"
    assert acoes[0].args["item"] == "pão"
    assert acoes[0].args["lista"] == "compras"


def test_lista_nome_customizado():
    acoes = mestre.parse_rapido("adiciona comprar leite na lista de tarefas", AGORA)
    assert acoes[0].args["lista"] == "tarefas"


def test_lista_ler_sem_verbo():
    acoes = mestre.parse_rapido("o que tem na lista de compras", AGORA)
    assert acoes == [mestre.tools.Decisao("ler_lista", {"lista": "compras"})]


def test_lista_remover():
    acoes = mestre.parse_rapido("remove leite da lista de compras", AGORA)
    assert acoes[0].tool == "remover_item"
    assert acoes[0].args["item"] == "leite"
    assert acoes[0].args["lista"] == "compras"


# -- parse_rapido: LEMBRETES ---------------------------------------------------
def test_alarme_so_horario_determinístico():
    acoes = mestre.parse_rapido("adicione alarme para daqui 4 horas", AGORA)
    assert len(acoes) == 1 and acoes[0].tool == "criar_lembrete"
    assert acoes[0].args["mensagem"] == "Alarme"


def test_lembrete_com_mensagem_defere_ao_llm():
    # Tem assunto ("ligar dentista") -> extração melhor no LLM -> None.
    assert mestre.parse_rapido("me lembra de ligar pro dentista amanhã às 9h", AGORA) is None


def test_cancelar_lembrete_por_numero():
    acoes = mestre.parse_rapido("cancela o lembrete 3", AGORA)
    assert acoes == [mestre.tools.Decisao("cancelar_lembrete", {"id": "3"})]


def test_listar_lembretes():
    assert mestre.parse_rapido("meus lembretes", AGORA)[0].tool == "listar_lembretes"


# -- parse_rapido: casos que DEFEREM ao LLM ------------------------------------
def test_watcher_defere_ao_llm():
    assert mestre.parse_rapido("me avise quando o dólar passar de 5,50", AGORA) is None


def test_composto_defere_ao_llm():
    # Lista + lembrete na mesma frase: uma ação por vez -> defere.
    cmd = "adicionar leite na lista e me lembrar de comprar amanhã"
    assert mestre.parse_rapido(cmd, AGORA) is None


def test_pergunta_conhecimento_nao_e_acao():
    assert mestre.parse_rapido("o que é RAG?", AGORA) is None


# -- parse_rapido: CAPTURA RÁPIDA ----------------------------------------------
def test_captura_com_dois_pontos():
    acoes = mestre.parse_rapido("anota rápido: comprar presente pra ana", AGORA)
    assert acoes == [mestre.tools.Decisao("capturar_nota", {"texto": "comprar presente pra ana"})]


def test_captura_isso():
    acoes = mestre.parse_rapido("captura isso: ideia de post sobre RAG", AGORA)
    assert acoes[0].tool == "capturar_nota"
    assert acoes[0].args["texto"] == "ideia de post sobre RAG"


def test_captura_preserva_na_no_meio():
    acoes = mestre.parse_rapido("anota comprar café na feira", AGORA)
    assert acoes[0].args["texto"] == "comprar café na feira"


def test_captura_joga_na_inbox():
    acoes = mestre.parse_rapido("joga na inbox testar o cache de voz", AGORA)
    assert acoes[0].args["texto"] == "testar o cache de voz"


def test_captura_vazia_defere():
    # Só o gatilho, sem conteúdo -> nada a capturar -> defere (não cria nota vazia).
    assert mestre.parse_rapido("anota", AGORA) is None


# -- parse_rapido: HEALTH-CHECK ------------------------------------------------
def test_status_diagnostico():
    assert mestre.parse_rapido("diagnóstico", AGORA) == [mestre.tools.Decisao("status_sistema", {})]


def test_status_frases():
    for c in ["status do sistema", "você está funcionando?", "faz um autoteste"]:
        acoes = mestre.parse_rapido(c, AGORA)
        assert acoes and acoes[0].tool == "status_sistema"


# -- modo confidencial (#5) ----------------------------------------------------
def test_modo_confidencial_liga():
    for c in ["modo sigiloso", "ativar modo confidencial", "entra em modo privado"]:
        assert mestre.modo_confidencial(c) is True


def test_modo_confidencial_desliga():
    for c in ["modo normal", "sair do sigilo", "pode registrar de novo"]:
        assert mestre.modo_confidencial(c) is False


def test_modo_confidencial_nao_e_comando():
    assert mestre.modo_confidencial("adiciona pão na lista") is None
    assert mestre.modo_confidencial("que horas são") is None
