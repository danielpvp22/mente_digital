"""
Fingerprint do índice (painel 2026-07): a coleção carimba com que embedding/prefixos
foi construída; mismatch dispara o rebuild (#33). Troca same-dimension é o caso 100%
silencioso — só o carimbo detecta. Testes com fakes, sem Chroma real.
"""
from __future__ import annotations

from mente_digital.config import settings
from mente_digital.rag import EmbeddingProvider, VectorStore


class _Col:
    def __init__(self, metadata):
        self.metadata = metadata
        self.modificado = None

    def modify(self, metadata):
        self.metadata = metadata
        self.modificado = metadata


class _Store:
    def __init__(self, metadata):
        self._collection = _Col(metadata)


def _vs(monkeypatch) -> VectorStore:
    monkeypatch.setattr(settings, "embedding_model", "intfloat/multilingual-e5-base")
    monkeypatch.setattr(settings, "embedding_query_prefix", "query: ")
    monkeypatch.setattr(settings, "embedding_passage_prefix", "passage: ")
    return VectorStore(EmbeddingProvider())


def test_carimbo_igual_passa(monkeypatch):
    vs = _vs(monkeypatch)
    store = _Store({
        "hnsw:space": "cosine",
        "emb_model": "intfloat/multilingual-e5-base",
        "emb_query_prefix": "query: ",
        "emb_passage_prefix": "passage: ",
    })
    assert vs._fingerprint_ok(store) is True


def test_modelo_diferente_falha(monkeypatch):
    # O foot-gun original: trocar MENTE_EMBEDDING_MODEL sem apagar o banco.
    vs = _vs(monkeypatch)
    store = _Store({
        "hnsw:space": "cosine",
        "emb_model": "paraphrase-multilingual-MiniLM-L12-v2",
        "emb_query_prefix": "query: ",
        "emb_passage_prefix": "passage: ",
    })
    assert vs._fingerprint_ok(store) is False


def test_so_o_prefixo_diferente_tambem_falha(monkeypatch):
    # O caso mais provável apontado na revisão: mesmo modelo, prefixos mudados.
    vs = _vs(monkeypatch)
    store = _Store({
        "hnsw:space": "cosine",
        "emb_model": "intfloat/multilingual-e5-base",
        "emb_query_prefix": "",
        "emb_passage_prefix": "",
    })
    assert vs._fingerprint_ok(store) is False


def test_colecao_legada_e_carimbada_com_uniao(monkeypatch):
    # Sem carimbo = base pré-fingerprint: carimba a config ATUAL preservando o
    # hnsw:space (modify substitui o dict INTEIRO — a união é obrigatória).
    vs = _vs(monkeypatch)
    store = _Store({"hnsw:space": "cosine"})
    assert vs._fingerprint_ok(store) is True
    assert store._collection.modificado is not None
    assert store._collection.modificado["hnsw:space"] == "cosine"
    assert store._collection.modificado["emb_model"] == "intfloat/multilingual-e5-base"


def test_api_ilegivel_nunca_derruba_o_boot(monkeypatch):
    # Upgrade da lib que mude _collection não pode impedir o open: na dúvida, OK.
    vs = _vs(monkeypatch)

    class _SemCollection:
        pass

    assert vs._fingerprint_ok(_SemCollection()) is True
