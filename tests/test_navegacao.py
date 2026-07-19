"""
Navegação por Voz (#14): parse_navegacao mapeia comando falado -> ação de UI, e
casar_conversa resolve "abre a conversa sobre X" num id. Ambos puros.
"""
import fio
import mestre


# ----------------------- parser de navegação -----------------------
def test_nova_conversa():
    assert mestre.parse_navegacao("mestre, nova conversa")["acao"] == "nova_conversa"
    assert mestre.parse_navegacao("começa uma conversa")["acao"] == "nova_conversa"


def test_abrir_e_fechar_historico():
    assert mestre.parse_navegacao("mostra o histórico")["acao"] == "abrir_historico"
    assert mestre.parse_navegacao("minhas conversas")["acao"] == "abrir_historico"
    assert mestre.parse_navegacao("fecha o histórico")["acao"] == "fechar_historico"


def test_carregar_por_tema():
    nav = mestre.parse_navegacao("abre a conversa sobre quantização")
    assert nav["acao"] == "carregar_conversa"
    assert nav["tema"] == "quantizacao"  # normalizado (sem acento)


def test_carregar_vence_abrir_historico():
    # "abre a conversa sobre X" tem argumento -> carregar, não confundir com abrir menu
    nav = mestre.parse_navegacao("carrega a conversa de tensorrt")
    assert nav["acao"] == "carregar_conversa"
    assert "tensorrt" in nav["tema"]


def test_nao_e_navegacao():
    assert mestre.parse_navegacao("qual a capital da Itália?") is None
    assert mestre.parse_navegacao("") is None


# ----------------------- casar tema -> conversa -----------------------
def test_casa_por_keyword_do_titulo():
    conversas = [
        {"id": "c3", "titulo": "sobre o clima de hoje", "n": 3},
        {"id": "c2", "titulo": "quantização de modelos LLM", "n": 5},
        {"id": "c1", "titulo": "receita de bolo", "n": 2},
    ]
    assert fio.casar_conversa(conversas, "quantização")["id"] == "c2"


def test_sem_casamento_retorna_none():
    conversas = [{"id": "c1", "titulo": "receita de bolo", "n": 2}]
    assert fio.casar_conversa(conversas, "tensorrt") is None


def test_empate_pega_a_mais_recente():
    # ambas casam "whisper"; conversas vêm em ordem de recência -> a 1ª vence
    # (nota: keywords passam pela STOP list do RAG — 'modelo'/'dados' são cortadas)
    conversas = [
        {"id": "c2", "titulo": "whisper novo", "n": 3},
        {"id": "c1", "titulo": "whisper antigo", "n": 3},
    ]
    assert fio.casar_conversa(conversas, "whisper")["id"] == "c2"
