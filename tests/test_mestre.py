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


# -- trilha de auditoria (#27) -------------------------------------------------
def test_auditoria_parse():
    for c in ["o que você fez hoje?", "trilha de auditoria", "quais suas ações hoje"]:
        acoes = mestre.parse_rapido(c, AGORA)
        assert acoes and acoes[0].tool == "auditoria_hoje"


# -- desfazer (#8): detecção -------------------------------------------------
def test_comando_desfazer_reconhece():
    for c in ["desfaça", "desfaz isso", "desfazer", "volta atrás", "anula",
              "cancela isso", "cancela a última", "esquece isso"]:
        assert mestre.comando_desfazer(c) is True, c


def test_comando_desfazer_nao_colide_com_acao_regular():
    # "cancela o lembrete 3" é uma AÇÃO (cancelar_lembrete), não um desfazer.
    assert mestre.comando_desfazer("cancela o lembrete 3") is False
    assert mestre.comando_desfazer("adiciona pão na lista") is False
    assert mestre.comando_desfazer("o que é RAG") is False
    # E o parse_rapido segue resolvendo o cancelamento por número normalmente.
    acoes = mestre.parse_rapido("cancela o lembrete 3", AGORA)
    assert acoes == [mestre.tools.Decisao("cancelar_lembrete", {"id": "3"})]


# -- desfazer (#8): cálculo da reversão --------------------------------------
def test_reverter_add_vira_remove():
    add = mestre.tools.Decisao("adicionar_item", {"lista": "compras", "item": "pão"})
    rev = mestre.reverter([(add, "adicionei 'pão' à lista de compras.")])
    assert rev == [mestre.tools.Decisao("remover_item", {"lista": "compras", "item": "pão"})]


def test_reverter_remove_vira_add():
    rem = mestre.tools.Decisao("remover_item", {"lista": "compras", "item": "leite"})
    rev = mestre.reverter([(rem, "removi 'leite' da lista de compras.")])
    assert rev == [mestre.tools.Decisao("adicionar_item", {"lista": "compras", "item": "leite"})]


def test_reverter_lembrete_vira_cancelar_pelo_id():
    cri = mestre.tools.Decisao("criar_lembrete", {"quando": "daqui 4h", "mensagem": "Alarme"})
    rev = mestre.reverter([(cri, "lembrete #7 criado para 17/07 às 18:30: Alarme")])
    assert rev == [mestre.tools.Decisao("cancelar_lembrete", {"id": "7"})]


def test_reverter_ignora_acao_que_falhou():
    # Remover um item inexistente NÃO deve gerar um "re-adicionar" fantasma.
    rem = mestre.tools.Decisao("remover_item", {"lista": "compras", "item": "xyz"})
    assert mestre.reverter([(rem, "não achei 'xyz' na lista de compras.")]) is None
    # Lembrete que falhou (sem #id) também não é reversível.
    cri = mestre.tools.Decisao("criar_lembrete", {"quando": "?", "mensagem": "x"})
    assert mestre.reverter([(cri, "não entendi o horário '?'.")]) is None


def test_reverter_ordem_inversa():
    # Duas adições -> a reversão remove na ORDEM INVERSA (desfaz da última p/ a primeira).
    a1 = mestre.tools.Decisao("adicionar_item", {"lista": "compras", "item": "pão"})
    a2 = mestre.tools.Decisao("adicionar_item", {"lista": "compras", "item": "ovos"})
    rev = mestre.reverter([(a1, "adicionei 'pão' à lista de compras."),
                           (a2, "adicionei 'ovos' à lista de compras.")])
    assert [r.args["item"] for r in rev] == ["ovos", "pão"]


def test_reverter_nao_reverte_leitura():
    ler = mestre.tools.Decisao("ler_lista", {"lista": "compras"})
    assert mestre.reverter([(ler, "# Lista: compras\n- pão\n")]) is None


# -- corta-e-corrige (#9): detecção + extração do valor certo -----------------
def test_tem_correcao():
    # inclui o imperativo "corrija" (corri-J-a) e "correção" — radicais != "corrig".
    for c in ["corrige para leite", "corrija para leite", "faz a correção para leite",
              "na verdade era leite", "quis dizer leite", "me enganei, era leite"]:
        assert mestre.tem_correcao(c) is True, c
    for c in ["adiciona pão na lista", "o que tem na lista", "desfaça"]:
        assert mestre.tem_correcao(c) is False, c


def test_parse_correcao_para():
    assert mestre.parse_correcao("corrige para leite") == "leite"
    assert mestre.parse_correcao("corrija pra leite integral") == "leite integral"
    assert mestre.parse_correcao("troca por leite") is None   # sem marcador de correção


def test_parse_correcao_descarta_negado():
    # "era X, não Y" -> o valor certo é X (o Y negado é descartado); preserva acento.
    assert mestre.parse_correcao("corrige: era leite, não pão") == "leite"
    assert mestre.parse_correcao("corrige para leite não pão") == "leite"


def test_parse_correcao_na_verdade_e_quis_dizer():
    assert mestre.parse_correcao("na verdade era café") == "café"
    assert mestre.parse_correcao("quis dizer café") == "café"


def test_parse_correcao_sem_valor():
    assert mestre.parse_correcao("corrige") is None
    assert mestre.parse_correcao("adiciona pão na lista") is None   # não é correção


def test_refazer_com_troca_o_item():
    add = mestre.tools.Decisao("adicionar_item", {"lista": "compras", "item": "pão"})
    redo = mestre.refazer_com([add], "leite")
    assert redo == [mestre.tools.Decisao("adicionar_item", {"lista": "compras", "item": "leite"})]


def test_refazer_com_sem_acao_de_valor():
    # Só um cancelar_lembrete na forward -> nada de item a corrigir.
    canc = mestre.tools.Decisao("cancelar_lembrete", {"id": "3"})
    assert mestre.refazer_com([canc], "leite") is None
    assert mestre.refazer_com([], "leite") is None


# -- cofre de confirmação (#25) -----------------------------------------------
def test_comando_confirmar():
    for c in ["confirma", "confirmo", "pode confirmar", "isso mesmo", "pode fazer"]:
        assert mestre.comando_confirmar(c) is True, c
    assert mestre.comando_confirmar("adiciona pão") is False


def test_comando_abortar():
    for c in ["não", "deixa pra lá", "esquece", "melhor não", "não precisa"]:
        assert mestre.comando_abortar(c) is True, c
    # "cancela"/"para" NÃO abortam (colidem com "cancela o lembrete 5").
    assert mestre.comando_abortar("cancela o lembrete 5") is False
    assert mestre.comando_abortar("confirma") is False


def test_descrever_acao():
    d = mestre.tools.Decisao("cancelar_lembrete", {"id": "3"})
    assert mestre.descrever_acao(d) == "cancelar o lembrete 3"
    r = mestre.tools.Decisao("remover_item", {"lista": "compras", "item": "pão"})
    assert "remover" in mestre.descrever_acao(r) and "pão" in mestre.descrever_acao(r)
