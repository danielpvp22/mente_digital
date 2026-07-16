"""
Gate de relevância da busca local (rag.VectorStore.search) — a correção central
do "Cache Hit falso". Um chunk só é relevante se (a) menciona uma keyword da
pergunta (aterramento léxico) OU (b) é semanticamente muito próximo
(distância < rag_score_confident). Testado com um store falso (sem ChromaDB).
"""
from config import settings
from rag import NENHUM, VectorStore

from conftest import FakeDoc, FakeStore


def _store_com(resultados):
    vs = VectorStore(embeddings=None)   # __init__ só guarda a referência
    vs._store = FakeStore(resultados)
    return vs


async def test_aterrado_por_keyword_e_relevante():
    # score longe do "confiante" (0.8), mas o texto MENCIONA a entidade
    doc = FakeDoc("O TensorFlow acelera inferência", {"confidence": 1.0})
    vs = _store_com([(doc, 1.2)])
    res = await vs.search("tensorflow rt")
    assert res.relevante is True
    assert "TensorFlow" in res.texto


async def test_confiante_por_distancia_sem_keyword():
    # texto NÃO cita 'tensorflow', mas a distância é baixa -> confiante
    doc = FakeDoc("framework de aprendizado profundo", {"confidence": 1.0})
    vs = _store_com([(doc, 0.4)])
    res = await vs.search("tensorflow")
    assert res.relevante is True
    assert res.texto != NENHUM


async def test_parecido_mas_fora_do_tema_nao_e_relevante():
    # nem keyword nem distância confiante -> NÃO conta como contexto (vai pra web)
    doc = FakeDoc("Uma receita de bolo de cenoura", {"confidence": 1.0})
    vs = _store_com([(doc, 1.2)])
    res = await vs.search("tensorflow")
    assert res.relevante is False
    assert res.texto == NENHUM
    assert res.melhor_dist == 1.2


async def test_acima_do_score_max_e_descartado():
    doc = FakeDoc("qualquer coisa", {"confidence": 1.0})
    vs = _store_com([(doc, 2.0)])   # > rag_score_max (1.5)
    res = await vs.search("tensorflow")
    assert res.relevante is False
    assert res.texto == NENHUM


async def test_query_vazia_retorna_nenhum():
    vs = _store_com([(FakeDoc("x"), 0.1)])
    res = await vs.search("")
    assert res.texto == NENHUM
    assert res.relevante is False


async def test_sem_store_retorna_nenhum():
    vs = VectorStore(embeddings=None)   # _store fica None
    res = await vs.search("tensorflow")
    assert res.texto == NENHUM
    assert res.relevante is False


def test_defaults_de_calibracao_intactos():
    # Trava os DEFAULTS DO CÓDIGO, não o valor efetivo: se mudarem sem querer, o gate
    # inteiro se desloca. Ignora o .env de propósito — o usuário PODE sobrescrever no
    # runtime (ex.: MENTE_RAG_SCORE_CONFIDENT=0.45); o contrato aqui é o default limpo.
    from config import Settings

    padrao = Settings(_env_file=None)
    assert padrao.rag_score_confident == 0.8
    assert padrao.rag_score_max == 1.5
