"""
Hubs primeiro na Síntese sob Demanda (G7, Onda 3): `VectorStore.buscar_conteudos`
reordena os átomos por centralidade na malha (backbone do tema primeiro), para o núcleo
cair nos primeiros lotes do map-reduce. Sem malha construída, mantém a ordem vetorial.
"""
from mente_digital.config import settings
from mente_digital.rag import VectorStore

from conftest import FakeDoc, FakeStore


def _vs(resultados):
    vs = VectorStore(embeddings=None)
    vs._store = FakeStore(resultados)
    return vs


def _malha(vs, corpus):
    """corpus: list de (source, conceitos_str). Constrói a malha."""
    docs = [f"corpo de {s}" for s, _ in corpus]
    metas = [{"source": s, "conceitos": c} for s, c in corpus]
    vs.malha.construir(docs, metas)


# Ordem vetorial: B, A, C. Na malha, A e C compartilham 'Raro'; B só tem 'Comum' (hub).
_DOC_B = FakeDoc("conteudo do atomo B", {"source": "b.md"})
_DOC_A = FakeDoc("conteudo do atomo A", {"source": "a.md"})
_DOC_C = FakeDoc("conteudo do atomo C", {"source": "c.md"})
_CORPUS = [
    ("a.md", "|Raro|X|"), ("c.md", "|Raro|Y|"), ("b.md", "|Comum|"),
    ("f1.md", "|Comum|"), ("f2.md", "|Comum|"), ("f3.md", "|Comum|"),
]


async def test_hubs_reordena_backbone_primeiro(monkeypatch):
    monkeypatch.setattr(settings, "sintese_hubs_primeiro", True)
    vs = _vs([(_DOC_B, 0.1), (_DOC_A, 0.2), (_DOC_C, 0.3)])
    _malha(vs, _CORPUS)
    out = await vs.buscar_conteudos("tema", k=10)
    # A e C (centrais pelo conceito raro compartilhado) sobem à frente de B (hub Comum).
    assert out[0] == "conteudo do atomo A"     # era o 2º na ordem vetorial
    assert out[-1] == "conteudo do atomo B"    # era o 1º; caiu por não ser central


async def test_hubs_desligado_mantem_ordem_vetorial(monkeypatch):
    monkeypatch.setattr(settings, "sintese_hubs_primeiro", False)
    vs = _vs([(_DOC_B, 0.1), (_DOC_A, 0.2), (_DOC_C, 0.3)])
    _malha(vs, _CORPUS)
    out = await vs.buscar_conteudos("tema", k=10)
    assert out == ["conteudo do atomo B", "conteudo do atomo A", "conteudo do atomo C"]


async def test_hubs_sem_malha_mantem_ordem(monkeypatch):
    monkeypatch.setattr(settings, "sintese_hubs_primeiro", True)
    vs = _vs([(_DOC_B, 0.1), (_DOC_A, 0.2), (_DOC_C, 0.3)])   # malha NÃO construída
    out = await vs.buscar_conteudos("tema", k=10)
    assert out == ["conteudo do atomo B", "conteudo do atomo A", "conteudo do atomo C"]
