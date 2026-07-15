"""
Ferramentas: parse do JSON do roteador, calculadora segura, gate lexical de ação,
e o registry. Tudo puro (sem GPU/LLM).
"""
import tools


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
