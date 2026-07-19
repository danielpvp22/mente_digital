"""
Fio da Conversa (#35): escolher_fio pega a conversa anterior COM SUBSTÂNCIA (não a
atual, não um 'oi' solto). Parser puro + o gatilho-mestre.
"""
import fio
import mestre


def test_pega_a_mais_recente_que_nao_e_a_atual():
    conversas = [
        {"id": "c3", "titulo": "atual", "n": 5},
        {"id": "c2", "titulo": "sobre RAG", "n": 4},
        {"id": "c1", "titulo": "sobre YOLO", "n": 8},
    ]
    escolha = fio.escolher_fio(conversas, cid_atual="c3", min_turnos=2)
    assert escolha["id"] == "c2"          # a mais recente ANTERIOR à atual


def test_pula_conversas_curtas():
    conversas = [
        {"id": "c3", "titulo": "atual", "n": 5},
        {"id": "c2", "titulo": "oi", "n": 1},        # curta demais -> não é fio
        {"id": "c1", "titulo": "sobre quantização", "n": 6},
    ]
    escolha = fio.escolher_fio(conversas, cid_atual="c3", min_turnos=2)
    assert escolha["id"] == "c1"


def test_nada_a_retomar():
    conversas = [{"id": "c1", "titulo": "atual", "n": 3}]
    assert fio.escolher_fio(conversas, cid_atual="c1", min_turnos=2) is None
    assert fio.escolher_fio([], cid_atual=None, min_turnos=2) is None


def test_sem_conversa_atual_pega_a_mais_recente():
    conversas = [{"id": "c2", "titulo": "tema", "n": 3}, {"id": "c1", "titulo": "outro", "n": 3}]
    assert fio.escolher_fio(conversas, cid_atual=None, min_turnos=2)["id"] == "c2"


def test_comando_retomar_fio():
    assert mestre.comando_retomar_fio("mestre, onde paramos?") is True
    assert mestre.comando_retomar_fio("retoma o fio da conversa") is True
    assert mestre.comando_retomar_fio("qual a raiz de 144?") is False
