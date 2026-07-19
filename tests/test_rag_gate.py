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


def _construir_malha(vs, corpus):
    """corpus: list de (texto, source). Popula o índice de palavras (df) da malha."""
    documentos = [t for t, _ in corpus]
    metadatas = [{"source": s} for _, s in corpus]
    vs.malha.construir(documentos, metadatas)


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


# --- Aterramento ponderado por IDF (G3) --------------------------------------
# Com a malha construída, uma keyword HUB (idf baixo) deixa de aterrar; uma keyword
# RARA (idf alto) continua aterrando. Sem malha (n_atomos==0), volta ao OR simples.
# Corpus fixo: "base" em 9 de 10 átomos (hub) e "tensorrt" em 1 (raro).
_CORPUS_IDF = [(f"a base fica no servidor {i}", f"n{i}.md") for i in range(9)]
_CORPUS_IDF.append(("tensorrt acelera a inferencia", "n9.md"))


async def test_idf_keyword_hub_deixa_de_aterrar(monkeypatch):
    # "base" (hub, idf ~0.1) é a ÚNICA keyword e o score não é confiante -> não aterra.
    monkeypatch.setattr(settings, "aterramento_idf_min", 1.0)
    doc = FakeDoc("A base de dados fica no servidor", {"confidence": 1.0})
    vs = _store_com([(doc, 1.2)])
    _construir_malha(vs, _CORPUS_IDF)
    res = await vs.search("base")
    assert res.relevante is False       # aterrava só por hub -> agora vai pra web
    assert res.texto == NENHUM


async def test_idf_keyword_rara_ainda_aterra(monkeypatch):
    # "tensorrt" (raro, idf ~2.3) segue valendo como evidência de aterramento.
    monkeypatch.setattr(settings, "aterramento_idf_min", 1.0)
    doc = FakeDoc("tensorrt acelera a inferencia do modelo", {"confidence": 1.0})
    vs = _store_com([(doc, 1.2)])
    _construir_malha(vs, _CORPUS_IDF)
    res = await vs.search("tensorrt")
    assert res.relevante is True
    assert "tensorrt" in res.texto


async def test_idf_sem_malha_mantem_or_simples(monkeypatch):
    # Guarda de segurança: sem malha construída (n_atomos==0), o hub ainda aterra —
    # nunca fica mais rígido do que dá para calibrar (ex.: testes/boot sem índice).
    monkeypatch.setattr(settings, "aterramento_idf_min", 1.0)
    doc = FakeDoc("A base de dados fica no servidor", {"confidence": 1.0})
    vs = _store_com([(doc, 1.2)])       # malha NÃO construída
    res = await vs.search("base")
    assert res.relevante is True        # OR booleano original preservado


async def test_idf_desligado_por_zero(monkeypatch):
    # idf_min<=0 desliga a ponderação: volta ao OR simples mesmo com a malha montada.
    monkeypatch.setattr(settings, "aterramento_idf_min", 0.0)
    doc = FakeDoc("A base de dados fica no servidor", {"confidence": 1.0})
    vs = _store_com([(doc, 1.2)])
    _construir_malha(vs, _CORPUS_IDF)
    res = await vs.search("base")
    assert res.relevante is True


# --- Dedup near-duplicate do contexto (G6) -----------------------------------
# Dois átomos com o MESMO conjunto de tokens (paráfrase/reordenação) contam como um —
# o segundo não gasta orçamento de contexto. Distintos entram os dois. Usa confiança
# semântica (score < 0.8) para os candidatos, então o teste não depende do aterramento.
async def test_dedup_near_remove_parafrase(monkeypatch):
    monkeypatch.setattr(settings, "rag_dedup_near_jaccard", 0.9)
    d1 = FakeDoc("o gato preto subiu no telhado alto", {"confidence": 1.0})
    d2 = FakeDoc("no telhado alto subiu o gato preto", {"confidence": 1.0})  # mesmos tokens
    vs = _store_com([(d1, 0.30), (d2, 0.31)])
    res = await vs.search("tema")
    assert res.texto.count("[Local") == 1       # só um dos dois entrou


async def test_dedup_near_mantem_distintos(monkeypatch):
    monkeypatch.setattr(settings, "rag_dedup_near_jaccard", 0.9)
    d1 = FakeDoc("o gato preto dorme durante o dia", {"confidence": 1.0})
    d2 = FakeDoc("o cachorro branco corre pela manha", {"confidence": 1.0})
    vs = _store_com([(d1, 0.30), (d2, 0.40)])
    res = await vs.search("tema")
    assert res.texto.count("[Local") == 2       # átomos distintos: os dois entram


async def test_dedup_near_desligado_mantem_ambos(monkeypatch):
    monkeypatch.setattr(settings, "rag_dedup_near_jaccard", 0.0)   # desligado
    d1 = FakeDoc("o gato preto subiu no telhado alto", {"confidence": 1.0})
    d2 = FakeDoc("no telhado alto subiu o gato preto", {"confidence": 1.0})
    vs = _store_com([(d1, 0.30), (d2, 0.31)])
    res = await vs.search("tema")
    assert res.texto.count("[Local") == 2       # sem near-dedup, ambos entram


def test_defaults_de_calibracao_intactos():
    # Trava os DEFAULTS DO CÓDIGO, não o valor efetivo: se mudarem sem querer, o gate
    # inteiro se desloca. Ignora o .env de propósito — o usuário PODE sobrescrever no
    # runtime (ex.: MENTE_RAG_SCORE_CONFIDENT=0.45); o contrato aqui é o default limpo.
    from config import Settings

    padrao = Settings(_env_file=None)
    assert padrao.rag_score_confident == 0.8
    assert padrao.rag_score_max == 1.5
    assert padrao.aterramento_idf_min == 1.5        # G3
    assert padrao.rotear_definicional_web is True   # Part A
    assert padrao.definicional_min_atomos == 3      # lever B
    assert padrao.rag_dedup_near_jaccard == 0.9     # G6
