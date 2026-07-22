"""
Ferramentas: parse do JSON do roteador, calculadora segura, gate lexical de ação,
e o registry. Tudo puro (sem GPU/LLM).
"""
import tools


# --- e_efemero: gate de INGESTÃO (mais largo que o de roteamento) ------------
def test_efemero_pega_as_perguntas_reais_que_poluiram_o_vault():
    # Medido: 48 dos 177 átomos auto-colhidos eram previsão do tempo/cotação. Estas
    # são as perguntas REAIS do histórico que os geraram.
    for q in (
        "Qual a previsão do tempo de Lisboa para amanhã?",
        "Qual a previsão do tempo para Lisboa semana que vem?",
        "como será o clima em lisboa para semana que vem?",
        "como será o clima em lisboa para amanhã?",
        "Olá, qual é a cotação do dólar hoje?",
    ):
        assert tools.e_efemero(q), q


def test_efemero_e_mais_largo_que_o_gate_de_rota():
    # O ponto do gate separado: "clima em lisboa amanhã" NÃO é time-sensitive pela
    # lista de rota (que tem "clima hoje"), mas é tão perecível quanto. Reusar o gate
    # de rota vazaria este caso — que é justamente um dos que poluíram o vault.
    q = "como será o clima em lisboa para amanhã?"
    assert tools.talvez_tempo_real(q) is False
    assert tools.e_efemero(q) is True


def test_efemero_nao_pega_conhecimento_duravel():
    # Falso positivo aqui custa "não aprender um fato" (barato), mas ainda assim o
    # gate não pode engolir o conhecimento técnico que É o ponto do vault.
    for q in (
        "como é o uso de tensor RT em yolo?",
        "o que são notas atômicas Zettelkasten?",
        "crie uma função recursiva em python",
        "quanto tempo leva para treinar o YOLO?",
        "como funciona o flash attention?",
    ):
        assert not tools.e_efemero(q), q


# --- pergunta_definicional: conhecimento GERAL -> web-first (Part A) ----------
def test_definicional_pega_perguntas_gerais():
    # O caso que motivou (Tarkov): "o que é RAG" casava a nota-piada pessoal em vez do
    # conceito. Definição geral vai pra web.
    for q in (
        "o que é RAG?",
        "o que são embeddings?",
        "o que significa overfitting?",
        "quem foi Alan Turing?",
        "me explica o transformer",
        "defina entropia",
        "significado de idempotência",
        # Imperativo NU (teste real 2507): "explica RAG" (sem "me") caía no local e
        # devolvia a nota-piada do Tarkov. "me explica" já pegava; "explica"/"explique" não.
        "explica RAG",
        "explique overfitting",
        "explica o que é um transformer",
    ):
        assert tools.pergunta_definicional(q), q


def test_definicional_exclui_pergunta_pessoal():
    # "reservando local pra pessoais": marcador de posse/1ª pessoa mantém no vault.
    for q in (
        "o que é o meu projeto?",
        "me explica o meu código",
        "o que eu anotei sobre RAG?",
        "o que é a nossa arquitetura?",
    ):
        assert tools.pergunta_definicional(q) is False, q


def test_definicional_nao_pega_nao_definicional():
    # Sem gatilho definicional -> segue a cascata local normal (falso NEGATIVO é barato).
    for q in (
        "como funciona o flash attention?",
        "o que estava acontecendo no servidor?",   # trailing-space guard: != "o que e "
        "qual a cotação do dólar hoje?",
        "resuma a reunião de ontem",
        "explicando melhor a ideia",   # trailing-space guard: "explica " exige espaço após
    ):
        assert tools.pergunta_definicional(q) is False, q


# --- parse_decisao ---------------------------------------------------------
def test_parse_decisao_json_limpo():
    d = tools.parse_decisao('{"tool":"calcular","args":{"expressao":"2+2"}}')
    assert d is not None
    assert d.tool == "calcular"
    assert d.args == {"expressao": "2+2"}


def test_parse_decisao_tolera_lixo_em_volta():
    d = tools.parse_decisao('```json\n{"tool":"hora_atual","args":{}}\n```')
    assert d is not None and d.tool == "hora_atual"


def test_parse_decisao_args_ausente_vira_dict_vazio():
    d = tools.parse_decisao('{"tool":"responder"}')
    assert d is not None and d.tool == "responder" and d.args == {}


def test_parse_decisao_texto_solto_e_none():
    assert tools.parse_decisao("não sei o que fazer") is None
    assert tools.parse_decisao("") is None
    assert tools.parse_decisao('{"args":{}}') is None  # sem 'tool'


# --- calcular_seguro -------------------------------------------------------
def test_calcular_operacoes():
    assert tools.calcular_seguro("240*0.15") == "36"
    assert tools.calcular_seguro("2+3*4") == "14"
    assert tools.calcular_seguro("10/4") == "2.5"
    assert tools.calcular_seguro("2**10") == "1024"
    assert tools.calcular_seguro("-5 + 8") == "3"


def test_calcular_rejeita_codigo_malicioso():
    # nada de eval: nomes, chamadas e atributos são recusados com mensagem, não crash
    assert tools.calcular_seguro("__import__('os')") == "não consegui calcular essa expressão"
    assert tools.calcular_seguro("abrir()") == "não consegui calcular essa expressão"
    assert tools.calcular_seguro("a + 1") == "não consegui calcular essa expressão"


# --- talvez_acao (gate lexical) -------------------------------------------
def test_talvez_acao_detecta_comandos():
    assert tools.talvez_acao("salva uma nota dizendo que a reunião é amanhã") is True
    assert tools.talvez_acao("quanto é 15% de 240") is True          # 'quanto e'
    assert tools.talvez_acao("que horas são agora") is True
    assert tools.talvez_acao("procura na web sobre python 3.13") is True


def test_talvez_acao_ignora_perguntas():
    assert tools.talvez_acao("o que é retrieval augmented generation") is False
    assert tools.talvez_acao("qual a capital da mongólia") is False
    assert tools.talvez_acao("me explica como funciona o RAG") is False


# --- registry --------------------------------------------------------------
def test_registry_tem_todas_as_ferramentas():
    reg = tools.criar_registry()
    for nome in ("calcular", "hora_atual", "salvar_nota", "listar_notas", "ler_nota", "buscar_web"):
        assert reg.get(nome) is not None
    assert reg.get("inexistente") is None
    # o menu tem uma linha por ferramenta
    assert len(reg.menu().splitlines()) == len(reg.nomes())


def test_ferramentas_terminais_vs_encadeaveis():
    reg = tools.criar_registry()
    assert reg.get("calcular").terminal is True
    assert reg.get("salvar_nota").terminal is True
    assert reg.get("buscar_web").terminal is False   # pode encadear
    assert reg.get("ler_nota").terminal is False


# --- executor puro (calcular não usa ctx) ----------------------------------
async def test_executor_calcular():
    obs = await tools._t_calcular({"expressao": "6*7"}, ctx=None)
    assert "42" in obs


async def test_executor_calcular_sem_expressao():
    assert await tools._t_calcular({}, ctx=None) == "faltou a expressão"


# --- correções da revisão -------------------------------------------------
def test_calcular_rejeita_potencia_gigante():
    # DoS: 9**9**9 congelaria o event loop (calculadora é síncrona). Deve recusar.
    assert tools.calcular_seguro("9**9**9") == "não consegui calcular essa expressão"
    assert tools.calcular_seguro("2**100000") == "não consegui calcular essa expressão"
    assert tools.calcular_seguro("2**10") == "1024"          # potência normal segue OK


def test_parse_decisao_robusto_a_chave_solta_e_dois_objetos():
    # chave solta na prosa antes do JSON real
    d = tools.parse_decisao('Claro {sorriso} {"tool":"hora_atual","args":{}}')
    assert d is not None and d.tool == "hora_atual"
    # dois objetos: pega o 1º válido com 'tool'
    d = tools.parse_decisao('{"nota":1} {"tool":"calcular","args":{"expressao":"2+2"}}')
    assert d is not None and d.tool == "calcular" and d.args == {"expressao": "2+2"}
    # chave DENTRO de string não confunde o balanceamento
    d = tools.parse_decisao('{"tool":"salvar_nota","args":{"conteudo":"a } b"}}')
    assert d is not None and d.tool == "salvar_nota" and d.args["conteudo"] == "a } b"
