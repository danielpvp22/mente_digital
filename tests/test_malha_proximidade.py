"""
G5′ (Onda 3): filtro de proximidade do vizinho da malha à PERGUNTA. A expansão traz o
vizinho pelo conceito raro COMPARTILHADO, mas medido: vem "do assunto certo, da pergunta
errada". Agora o vizinho só entra se a similaridade de cosseno do seu texto com a pergunta
for >= malha_sim_min. Sem embeddings, o filtro é pulado (fail-open).
"""
from config import settings
from rag import VectorStore

from conftest import FakeDoc, FakeStore


class _EmbInstance:
    """embed_query/embed_documents deterministas a partir de um mapa texto->vetor."""

    def __init__(self, mapa, qvec):
        self._mapa = mapa
        self._qvec = qvec

    def embed_query(self, _texto):
        return self._qvec

    def embed_documents(self, textos):
        return [self._mapa[t] for t in textos]


class _EmbProvider:
    def __init__(self, mapa, qvec):
        self._inst = _EmbInstance(mapa, qvec)

    @property
    def instance(self):
        return self._inst


# Semente s.md + vizinhos que compartilham o conceito raro 'Raro'. 'perto' é colinear com
# a pergunta ([1,0]); 'longe' é ortogonal ([0,1]).
_CORPUS = [
    ("s.md", "|Raro|", "semente"),
    ("near.md", "|Raro|", "vizinho perto"),
    ("far.md", "|Raro|", "vizinho longe"),
    ("g.md", "|Outro|", "filler"),          # tira o idf de 'Raro' de zero
]
_VETORES = {"vizinho perto": [1.0, 0.0], "vizinho longe": [0.0, 1.0]}


def _vs_com_semente(emb):
    vs = VectorStore(embeddings=emb)
    vs._store = FakeStore([(FakeDoc("semente", {"source": "s.md", "confidence": 1.0}), 0.4)])
    docs = [txt for _, _, txt in _CORPUS]
    metas = [{"source": s, "conceitos": c} for s, c, _ in _CORPUS]
    vs.malha.construir(docs, metas)
    return vs


async def test_vizinho_longe_da_pergunta_e_cortado(monkeypatch):
    monkeypatch.setattr(settings, "malha_expandir", True)
    monkeypatch.setattr(settings, "malha_idf_min", 0.1)   # deixa 'Raro' (idf ~0.29) passar
    monkeypatch.setattr(settings, "malha_sim_min", 0.5)
    vs = _vs_com_semente(_EmbProvider(_VETORES, qvec=[1.0, 0.0]))
    res = await vs.search("tema")
    assert "vizinho perto" in res.texto        # colinear com a pergunta -> entra
    assert "vizinho longe" not in res.texto    # ortogonal -> cortado pelo filtro


async def test_filtro_desligado_mantem_todos(monkeypatch):
    monkeypatch.setattr(settings, "malha_expandir", True)
    monkeypatch.setattr(settings, "malha_idf_min", 0.1)
    monkeypatch.setattr(settings, "malha_sim_min", 0.0)   # filtro off
    vs = _vs_com_semente(_EmbProvider(_VETORES, qvec=[1.0, 0.0]))
    res = await vs.search("tema")
    assert "vizinho perto" in res.texto
    assert "vizinho longe" in res.texto        # sem filtro, o ortogonal também entra


async def test_sem_embeddings_nao_filtra(monkeypatch):
    monkeypatch.setattr(settings, "malha_expandir", True)
    monkeypatch.setattr(settings, "malha_idf_min", 0.1)
    monkeypatch.setattr(settings, "malha_sim_min", 0.5)
    vs = _vs_com_semente(emb=None)             # sem embeddings (fail-open)
    res = await vs.search("tema")
    assert "vizinho perto" in res.texto
    assert "vizinho longe" in res.texto        # sem embedding não dá pra rankear -> mantém
