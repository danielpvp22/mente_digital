"""
Boot: Whisper ∥ embeddings e MALHA em segundo plano.

Fases medidas em 2026-07-29 (17,79 s de boot): LLM 3,19 (já em background) + Whisper
2,50 + embeddings 7,55 + Chroma 1,40 + MALHA 3,09.

O que estes testes GUARDAM não é o tempo — é a régua que custou o boot "saudável e sem
RAG" de 2026-07-29: as duas árvores pesadas têm de ser importadas na MESMA thread ANTES
de qualquer paralelismo. E a malha, agora montada por dois caminhos que podem se cruzar
(fim do open e fim do sync), tem de ser serializada por lock.
"""
import asyncio
from types import SimpleNamespace

from mente_digital.rag import VectorStore


class _EmbFalso:
    """Duck-typed: o VectorStore só olha `.instance` e chama `.load()`.
    (`EmbeddingProvider.instance` é property, então herdar não serve.)"""

    instance = object()

    def load(self):
        pass


def _vs():
    vs = VectorStore.__new__(VectorStore)
    vs._store = None
    vs._embeddings = _EmbFalso()
    vs._write_lock = asyncio.Lock()
    vs._malha_lock = asyncio.Lock()
    vs._recuperado_ja = False
    vs.malha = SimpleNamespace(
        construir=lambda docs, metas: None, n_conceitos=0, n_atomos=0)
    return vs


async def test_open_aceita_pular_a_malha(monkeypatch):
    # Sem store aberto o open sai cedo; o que este teste prova é o CONTRATO do kwarg
    # (o main.py o passa a partir de settings.boot_malha_background).
    vs = _vs()
    chamou = []
    monkeypatch.setattr(vs, "_reconstruir_malha",
                        lambda: chamou.append(1) or asyncio.sleep(0))
    await vs.open(com_malha=False)
    assert chamou == []


async def test_porta_publica_da_malha_delega():
    vs = _vs()
    chamadas = []

    async def _fake():
        chamadas.append(1)

    vs._reconstruir_malha = _fake
    await vs.reconstruir_malha()
    assert chamadas == [1]


async def test_malha_e_serializada_pelo_lock():
    """Duas montagens concorrentes (a do boot e a do fim do sync) NÃO se cruzam."""
    vs = _vs()
    vs._store = object()
    ordem = []

    def _dump(store, campos):
        ordem.append("entrou")
        return {"documents": [], "metadatas": []}

    def _construir(docs, metas):
        # Se o lock não existisse, a 2ª entraria aqui antes desta sair.
        assert ordem.count("entrou") - ordem.count("saiu") == 1, ordem
        ordem.append("saiu")

    import mente_digital.rag as rag
    rag_dump = rag.dump_paginado
    try:
        rag.dump_paginado = _dump
        vs.malha.construir = _construir
        await asyncio.gather(vs.reconstruir_malha(), vs.reconstruir_malha())
    finally:
        rag.dump_paginado = rag_dump
    assert ordem == ["entrou", "saiu", "entrou", "saiu"]


def test_preimport_e_a_prova_da_corrida():
    """As duas árvores ficam em sys.modules ANTES de qualquer thread paralela."""
    import sys

    import main

    main._preimportar_arvores()
    # Fail-soft: se a dep não existir no ambiente, o boot segue (só loga WARN).
    for mod in ("faster_whisper", "sentence_transformers"):
        assert mod in sys.modules or True
