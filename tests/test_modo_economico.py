"""
Modo Econômico (#30): meta-comando de sessão que BYPASSA o gate de relevância
(responde do vault em vez de escalar pra web). Parser puro + o bypass no search.
"""
import mestre
from rag import NENHUM, VectorStore

from conftest import FakeDoc, FakeStore


# ----------------------- parser puro -----------------------
def test_liga():
    assert mestre.comando_economico("mestre, modo econômico") is True
    assert mestre.comando_economico("modo economia") is True
    assert mestre.comando_economico("não use a web") is True


def test_desliga():
    assert mestre.comando_economico("modo econômico off") is False
    assert mestre.comando_economico("pode usar a web") is False


def test_desliga_vence_liga():
    # "sai do modo economico" contém "modo economico" — o off tem que ganhar.
    assert mestre.comando_economico("sai do modo econômico") is False


def test_nao_e_comando():
    assert mestre.comando_economico("qual a capital da França?") is None
    assert mestre.comando_economico("") is None


# ----------------------- o bypass do gate -----------------------
def _store_com(resultados):
    vs = VectorStore(embeddings=None)
    vs._store = FakeStore(resultados)
    return vs


async def test_sem_economico_gate_rejeita_e_vai_pra_web():
    # dist 1.2: não aterrado, não confiante (>0.8) -> gate REJEITA (comportamento normal)
    doc = FakeDoc("Uma receita de bolo de cenoura", {"confidence": 1.0})
    vs = _store_com([(doc, 1.2)])
    res = await vs.search("tensorflow", economico=False)
    assert res.relevante is False
    assert res.texto == NENHUM


async def test_com_economico_o_mesmo_match_fraco_e_aceito():
    # MESMO doc/dist: no modo econômico o gate é bypassado -> vira contexto (não web)
    doc = FakeDoc("Uma receita de bolo de cenoura", {"confidence": 1.0})
    vs = _store_com([(doc, 1.2)])
    res = await vs.search("tensorflow", economico=True)
    assert res.relevante is True
    assert res.texto != NENHUM
    assert "bolo de cenoura" in res.texto


async def test_economico_ainda_respeita_o_score_max():
    # bypass do gate NÃO significa ingerir lixo: acima do rag_score_max segue fora.
    doc = FakeDoc("qualquer coisa", {"confidence": 1.0})
    vs = _store_com([(doc, 2.0)])   # > rag_score_max (1.5) -> nem é "válido"
    res = await vs.search("tensorflow", economico=True)
    assert res.relevante is False
    assert res.texto == NENHUM
