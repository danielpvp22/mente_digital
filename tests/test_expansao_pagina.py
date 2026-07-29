"""Expansão por página: os irmãos do átomo que casou entram atrás dele.

A re-atomização com o 8B fatiou o livro mais fino (8.296 átomos onde o 4B fez
4.739 sobre o mesmo texto) e separou o fato do seu contexto. Medido pelo caminho
real, o ganho é TODO nas perguntas cujo contexto vinha pela metade: poda apical
5.726 -> 9.742 chars, oídio 6.947 -> 11.192.
"""
import asyncio

from mente_digital import rag
from mente_digital.config import settings


class _Store:
    """Store mínimo: só o `get(where=...)` que a expansão usa."""

    def __init__(self, docs):
        self.docs = docs          # [(texto, metadata)]
        self.chamadas = []

    def get(self, where=None, include=None):
        origens = set((where or {}).get("origem", {}).get("$in", []))
        self.chamadas.append(origens)
        sel = [(t, m) for t, m in self.docs if m.get("origem") in origens]
        return {"documents": [t for t, _m in sel], "metadatas": [m for _m, m in
                                                                 [(m, m) for _t, m in sel]]}


def _vs(docs):
    vs = rag.VectorStore.__new__(rag.VectorStore)
    vs._store = _Store(docs)
    return vs


PAGINA = "Livro 'X' — cap (p. 3-3)"
OUTRA = "Livro 'X' — cap (p. 9-9)"


def _casou(origem=PAGINA):
    return rag._DocSimples("átomo que casou", {"origem": origem})


def test_traz_irmaos_da_mesma_pagina():
    vs = _vs([("irmão 1", {"origem": PAGINA}), ("irmão 2", {"origem": PAGINA}),
              ("de outra página", {"origem": OUTRA})])
    fora = asyncio.run(vs._irmaos_de_pagina([_casou()], set()))
    assert [d.page_content for _s, d in fora] == ["irmão 1", "irmão 2"]


def test_nao_repete_o_que_ja_esta_no_contexto():
    vs = _vs([("irmão 1", {"origem": PAGINA}), ("irmão 2", {"origem": PAGINA})])
    fora = asyncio.run(vs._irmaos_de_pagina([_casou()], {"irmão 1"}))
    assert [d.page_content for _s, d in fora] == ["irmão 2"]


def test_figura_nao_entra_pela_porta_do_irmao():
    """Ela tem porta própria (`_buscar_figuras`) e teto próprio; entrar por aqui
    devolveria o problema que o teto resolveu."""
    vs = _vs([("- ![[Figuras/x/y.webp]] — legenda", {"origem": PAGINA}),
              ("texto irmão", {"origem": PAGINA})])
    fora = asyncio.run(vs._irmaos_de_pagina([_casou()], set()))
    assert [d.page_content for _s, d in fora] == ["texto irmão"]


def test_respeita_o_teto_de_irmaos(monkeypatch):
    monkeypatch.setattr(settings, "rag_max_irmaos", 2)
    vs = _vs([(f"irmão {i}", {"origem": PAGINA}) for i in range(5)])
    assert len(asyncio.run(vs._irmaos_de_pagina([_casou()], set()))) == 2


def test_teto_zero_desliga(monkeypatch):
    monkeypatch.setattr(settings, "rag_max_irmaos", 0)
    vs = _vs([("irmão", {"origem": PAGINA})])
    assert asyncio.run(vs._irmaos_de_pagina([_casou()], set())) == []


def test_atomo_sem_origem_nao_consulta_o_banco():
    vs = _vs([("irmão", {"origem": PAGINA})])
    doc = rag._DocSimples("sem proveniência", {})
    assert asyncio.run(vs._irmaos_de_pagina([doc], set())) == []
    assert vs._store.chamadas == []


def test_erro_no_store_nao_derruba_a_busca():
    """Fail-soft: expansão é bônus, nunca pode derrubar a resposta."""
    class _Quebrado:
        def get(self, **_kw):
            raise RuntimeError("chroma sem `where`")

    vs = rag.VectorStore.__new__(rag.VectorStore)
    vs._store = _Quebrado()
    assert asyncio.run(vs._irmaos_de_pagina([_casou()], set())) == []
