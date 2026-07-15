"""
Memória de sessão e cache — todas limitadas por tamanho (sem creep de RAM).
"""
from config import settings
from state import LruCache, SessionMemory


def test_lru_evicta_o_mais_antigo():
    c = LruCache(maxsize=2)
    c.put("a", "1")
    c.put("b", "2")
    c.put("c", "3")          # excede -> "a" (mais antigo) sai
    assert c.get("a") is None
    assert c.get("b") == "2"
    assert c.get("c") == "3"


def test_lru_get_promove_para_recente():
    c = LruCache(maxsize=2)
    c.put("a", "1")
    c.put("b", "2")
    c.get("a")               # "a" vira o mais recente
    c.put("c", "3")          # agora "b" é o mais antigo -> sai
    assert c.get("b") is None
    assert c.get("a") == "1"


def test_session_memory_respeita_maxlen():
    mem = SessionMemory(settings)
    limite = settings.max_chat_history
    for i in range(limite + 10):
        mem.registrar_turno(f"p{i}", f"r{i}")
    assert len(mem.chat_history) == limite
    # o mais antigo foi descartado; o mais novo permanece
    assert mem.chat_history[-1] == (f"p{limite + 9}", f"r{limite + 9}")


def test_drenar_etl_esvazia_a_fila():
    mem = SessionMemory(settings)
    mem.enfileirar_etl("tema1", "dados1")
    mem.enfileirar_etl("tema2", "dados2")
    itens = mem.drenar_etl()
    assert itens == [("tema1", "dados1"), ("tema2", "dados2")]
    assert len(mem.fila_etl) == 0
    # drenar de novo devolve vazio
    assert mem.drenar_etl() == []
