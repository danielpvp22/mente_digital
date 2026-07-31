"""
Busca EXATA de figura em memória, no lugar do índice aproximado (2026-07-31).

O BUG: o dono pediu a foto de uma capivara e recebeu figuras de cannabis. A nota certa
está indexada, com o conteúdo certo, a **0,1319** da pergunta — e o Chroma devolvia
0,1673, com a capivara fora até do **top-300** entre 1.861 figuras.

A causa é mecânica, não de ranking: o hnswlib usa `ef = max(ef_search, n_results)`, e
`figuras_top_k=12` explora o grafo raso demais para alcançar um ponto ISOLADO — uma
capivara num acervo de cannabis tem poucos vizinhos no grafo HNSW. Com `n_results=1000`
a mesma nota reaparece em 1º lugar. Medido:

    n_results=12 (o que estava no ar) .... recall@10 90,5% · top-1 70% · 18,6 ms
    n_results=500 ....................... recall@10 98,0% · top-1 90% · 50,4 ms
    n_results=2000 (exaustivo) .......... recall@10 100%  · top-1 100% · 140,1 ms
    busca exata em memória .............. recall@10 100%  · top-1 100% · 0,22 ms

⚠ O docstring de `_buscar_figuras` AFIRMAVA "recall exato dentro do subconjunto (20/20
contra força bruta nos 1.735 vetores)". Era verdade e deixou de ser quando a base
cresceu. A lição está no arquivo: afirmação medida tem data de validade — e uma
medição antiga no comentário é pior que nenhuma, porque impede a próxima.

A média enganava: recall@10 de 90,5% parece bom, mas **as perdas se concentram no
primeiro colocado**. Perder o 8º de dez não muda resposta nenhuma; perder o 1º é
perder a figura que responde.
"""
import asyncio

import pytest

pytest.importorskip("numpy")
import numpy as np  # noqa: E402

from mente_digital import rag  # noqa: E402
from mente_digital.config import settings  # noqa: E402


def _indice(vetores):
    m = np.asarray(vetores, dtype="float32")
    m /= (np.linalg.norm(m, axis=1, keepdims=True) + 1e-12)
    docs = [rag._DocSimples(f"figura {i}", {"source": f"/v/Figuras/f{i}.md"})
            for i in range(len(vetores))]
    return rag._IndiceFiguras(m, docs)


# --- a matemática --------------------------------------------------------------
def test_acha_a_mais_proxima_mesmo_sendo_um_ponto_ISOLADO():
    """O caso da capivara: um vetor sozinho, longe do bolo, é o mais próximo da query.

    É exatamente o que o HNSW errava — o ponto isolado tem poucos vizinhos no grafo e
    a travessia rasa nunca chega nele. Força bruta não tem esse problema por
    construção, e é isso que este teste trava.
    """
    bolo = [[1.0, 0.0, 0.05 * i] for i in range(1, 40)]       # 39 vetores agrupados
    isolado = [0.0, 1.0, 0.0]                                  # o "capivara"
    idx = _indice(bolo + [isolado])
    achados = idx.buscar([0.0, 1.0, 0.0], k=3)
    assert achados[0][1].page_content == f"figura {len(bolo)}"
    assert achados[0][0] < 1e-5


def test_devolve_em_ordem_crescente_de_distancia():
    idx = _indice([[1, 0, 0], [0.9, 0.1, 0], [0, 1, 0]])
    d = [dist for dist, _doc in idx.buscar([1, 0, 0], k=3)]
    assert d == sorted(d)


def test_k_maior_que_o_acervo_nao_estoura():
    idx = _indice([[1, 0, 0], [0, 1, 0]])
    assert len(idx.buscar([1, 0, 0], k=99)) == 2


def test_vetor_nulo_nao_divide_por_zero():
    idx = _indice([[1, 0, 0]])
    assert idx.buscar([0, 0, 0], k=1) == []


# --- montagem: fail-soft, nunca derruba a busca --------------------------------
class _Col:
    def __init__(self, got):
        self._got = got

    def get(self, where=None, include=None):  # noqa: A002
        if isinstance(self._got, Exception):
            raise self._got
        return self._got


class _Store:
    def __init__(self, got):
        self._collection = _Col(got)


def test_base_sem_figura_devolve_none():
    assert rag.montar_indice_figuras(_Store({"embeddings": [], "metadatas": [],
                                             "documents": []})) is None


def test_erro_no_chroma_devolve_none_em_vez_de_explodir():
    assert rag.montar_indice_figuras(_Store(RuntimeError("chroma caiu"))) is None


def test_desalinhamento_entre_vetor_e_documento_devolve_none():
    # Meia figura é pior que figura nenhuma: entregaria a legenda de uma com o vetor
    # de outra, e o erro seria invisível.
    got = {"embeddings": [[1, 0], [0, 1]], "metadatas": [{}], "documents": ["só um"]}
    assert rag.montar_indice_figuras(_Store(got)) is None


def test_monta_com_o_que_veio():
    got = {"embeddings": [[1.0, 0.0], [0.0, 1.0]],
           "metadatas": [{"source": "/v/a.md"}, {"source": "/v/b.md"}],
           "documents": ["legenda A", "legenda B"]}
    idx = rag.montar_indice_figuras(_Store(got))
    assert len(idx) == 2
    assert idx.buscar([1.0, 0.0], k=1)[0][1].page_content == "legenda A"


# --- a fiação ------------------------------------------------------------------
class _StoreANN:
    """Store que só sabe o caminho ANTIGO — serve para provar que o fallback existe."""

    def __init__(self):
        self.chamou_ann = False
        self._collection = _Col(RuntimeError("sem coleção"))

    def similarity_search_with_score(self, consulta, k=4, filter=None):  # noqa: A002
        self.chamou_ann = True
        return [(rag._DocSimples("via ANN", {"source": "/v/x.md"}), 0.2)]


def _vs(store):
    vs = rag.VectorStore.__new__(rag.VectorStore)
    vs._store = store
    vs._idx_figuras = None
    emb = type("E", (), {"instance": type("I", (), {
        "embed_query": staticmethod(lambda t: [1.0, 0.0])})()})()
    vs._embeddings = emb
    return vs


def test_cai_no_ANN_quando_nao_da_para_montar_o_indice(monkeypatch):
    monkeypatch.setattr(settings, "figuras_busca_exata", True)
    store = _StoreANN()
    res = asyncio.run(_vs(store)._figuras_candidatas("capivara"))
    assert store.chamou_ann and res[0][0].page_content == "via ANN"


def test_flag_desligada_usa_o_ANN(monkeypatch):
    monkeypatch.setattr(settings, "figuras_busca_exata", False)
    store = _StoreANN()
    asyncio.run(_vs(store)._figuras_candidatas("capivara"))
    assert store.chamou_ann


def test_com_indice_exato_o_ANN_nem_e_chamado(monkeypatch):
    monkeypatch.setattr(settings, "figuras_busca_exata", True)
    monkeypatch.setattr(settings, "figuras_top_k", 5)
    store = _StoreANN()
    vs = _vs(store)
    vs._idx_figuras = _indice([[1.0, 0.0], [0.0, 1.0]])
    res = asyncio.run(vs._figuras_candidatas("capivara"))
    assert not store.chamou_ann
    assert res[0][0].page_content == "figura 0"      # o mais próximo de [1, 0]


def test_sync_invalida_o_indice():
    """Sem isto a figura NOVA fica invisível até o restart — falha silenciosa.

    O `sync` tem várias saídas antecipadas ("nada novo", erro, lock); por isso a
    invalidação mora na ENTRADA, e não junto de onde ele grava. Este teste falha se
    alguém mover a linha para dentro de um dos ramos.
    """
    vs = rag.VectorStore.__new__(rag.VectorStore)
    vs._store = _StoreANN()
    vs._write_lock = asyncio.Lock()
    vs._idx_figuras = _indice([[1.0, 0.0]])
    asyncio.run(vs.sync())            # fail-soft: o store falso derruba dentro do try
    assert vs._idx_figuras is None


def test_o_formato_devolvido_e_o_mesmo_do_chroma(monkeypatch):
    # `_buscar_figuras` desempacota como (doc, score); inverter a ordem aqui passaria
    # nos testes de unidade e quebraria a busca inteira em produção.
    monkeypatch.setattr(settings, "figuras_busca_exata", True)
    monkeypatch.setattr(settings, "figuras_top_k", 1)
    vs = _vs(_StoreANN())
    vs._idx_figuras = _indice([[1.0, 0.0]])
    doc, score = asyncio.run(vs._figuras_candidatas("x"))[0]
    assert hasattr(doc, "page_content") and isinstance(score, float)
