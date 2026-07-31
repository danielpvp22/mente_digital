"""
Irmão de página NÃO é vizinho de malha — o rótulo tem de distinguir (2026-07-31).

O BUG HISTÓRICO: `rotular_contexto` usava `score is None` como marca de "veio da
malha" e emitia "[Malha - relacionado]". Só que DUAS portas entram sem distância — o
vizinho de conceito (`malha_expandir`) e o irmão de página (`_irmaos_de_pagina`) — e
as duas devolviam `(None, doc)`. Resultado medido sobre 205 perguntas reais do
`chat_history` com `malha_expandir=false` (ou seja, com ZERO vizinhos de malha
existindo no sistema): o contexto ainda levava **3,96 blocos por pergunta** rotulados
"[Malha - relacionado]", todos irmãos de página.

POR QUE IMPORTA: as duas fontes carregam força de evidência oposta. O vizinho de
conceito é o elo mais fraco do pipeline — medido em 2026-07-31, o ranking por IDF
ganha 2,3% das vagas contra o próximo match vetorial. O irmão de página é co-locação
POR CONSTRUÇÃO: mesma página do mesmo livro do átomo que respondeu. O rótulo existe
justamente para dizer ao modelo qual é qual (ver a docstring em `rag.rotular_contexto`),
e estava dizendo o contrário.

Estes testes falham se os dois rótulos voltarem a ser o mesmo, e falham também se a
marca de proveniência parar de ser posta na origem (que é o jeito silencioso de o bug
voltar: o rótulo continua existindo, mas ninguém mais o alcança).
"""
import asyncio

from mente_digital import rag

from conftest import FakeDoc


def _doc(texto: str, **extra) -> FakeDoc:
    return FakeDoc(texto, {"source": "/vault/a.md", "tipo": "texto", **extra})


class StoreDaPagina:
    """Chroma reduzido ao que `_irmaos_de_pagina` usa: `get(where=..., include=...)`."""

    def __init__(self, docs):
        self._docs = docs

    def get(self, where=None, include=None):  # noqa: A002
        return {"documents": [t for t, _md in self._docs],
                "metadatas": [md for _t, md in self._docs]}


class _StoreDono:
    """`self` mínimo: `_irmaos_de_pagina` só toca em `self._store`."""

    def __init__(self, store):
        self._store = store


# --- o rótulo separa as duas portas -----------------------------------------
def test_irmao_de_pagina_nao_e_anunciado_como_vizinho_de_malha():
    rotulo = rag.rotular_contexto(None, _doc("corpo", **{rag.IRMAO_DE_PAGINA: True}))
    assert "Mesma página" in rotulo
    assert "Malha" not in rotulo


def test_vizinho_de_malha_continua_com_o_rotulo_de_sempre():
    # Sem a marca, `score is None` segue significando malha — nada regrediu.
    rotulo = rag.rotular_contexto(None, _doc("corpo"))
    assert "[Malha - relacionado]" in rotulo
    assert "Mesma página" not in rotulo


def test_os_dois_rotulos_sao_DIFERENTES():
    # O teste que impede o conserto de ser desfeito por "simplificação": se alguém
    # colapsar os dois de volta num só, isto quebra.
    irmao = rag.rotular_contexto(None, _doc("x", **{rag.IRMAO_DE_PAGINA: True}))
    malha = rag.rotular_contexto(None, _doc("x"))
    assert irmao != malha


def test_o_match_real_nao_foi_afetado():
    # A marca só age no ramo sem distância; quem tem score continua com Confiança.
    assert "Confiança" in rag.rotular_contexto(
        0.1, _doc("corpo", confidence=0.8, **{rag.IRMAO_DE_PAGINA: True}))


# --- e a marca é REALMENTE posta na origem ----------------------------------
def _irmaos(monkeypatch, docs_no_store, semente_origem):
    monkeypatch.setattr(rag.settings, "rag_max_irmaos", 12)
    monkeypatch.setattr(rag.settings, "rag_irmaos_so_pagina", True)
    dono = _StoreDono(StoreDaPagina(docs_no_store))
    semente = [FakeDoc("o que respondeu", {"origem": semente_origem})]
    return asyncio.run(rag.VectorStore._irmaos_de_pagina(dono, semente, set()))


def test_irmaos_de_pagina_marca_a_proveniencia(monkeypatch):
    # `(p. N` é o que `livro.e_origem_de_pagina` exige para aceitar a âncora.
    origem = "Livro 'The Cannabis Encyclopedia' — Soil – Chapter 21 (p. 300-301)"
    fora = _irmaos(monkeypatch, [("irmão da mesma página", {"origem": origem})], origem)
    assert len(fora) == 1
    score, doc = fora[0]
    assert score is None                                   # segue entrando sem distância
    assert (doc.metadata or {}).get(rag.IRMAO_DE_PAGINA)   # ...mas agora se identifica


def test_o_irmao_que_o_store_devolve_chega_rotulado_como_pagina(monkeypatch):
    # O teste de ponta a ponta: é a composição das duas metades que o bug quebrava.
    # Sem a marca posta em `_irmaos_de_pagina`, isto volta a dizer "[Malha]".
    origem = "Livro 'Marijuana Horticulture' — Air – Chapter 16 (p. 12-13)"
    fora = _irmaos(monkeypatch, [("o resto da página", {"origem": origem})], origem)
    assert "Mesma página" in rag.rotular_contexto(*fora[0])
