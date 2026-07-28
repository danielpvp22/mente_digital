"""Figura ATTACH-ONLY: sai da busca, volta pela co-locação (2026-07-27).

O achado que sustenta isto, medido em 2026-07-27 sobre o acervo real: figura sem
legenda de verdade é INVISÍVEL (17,4% dos arquivos, 3,3% das entregas, 1,9% dos
erros) — e é justamente por ser invisível que ela não faz mal. Tentar torná-la
encontrável foi o que produziu as notas genéricas e o caso "tricomas", em que o
acervo não cobria o tema e a busca entregou a figura "menos ruim".

Então ela nunca disputa vaga: some do espaço de busca e só aparece acompanhando
o átomo da PRÓPRIA página dela — co-locação exata, que a página sintética da
importação web dá por construção.
"""
from mente_digital import prompts, rag
from mente_digital.config import settings

from conftest import FakeDoc

CORPO_ANEXO = f"## Figura da página 3\n![[Figuras/l/l_p0003_f1.webp]]\n{prompts.TAG_FIGURA_ANEXO}"
ORIGEM = "Livro 'Enciclopédia' — Soil (p. 3-3)"


def _fig(nome: str, texto: str, attach: bool = False, origem: str = "") -> FakeDoc:
    meta = {"source": f"/vault/Figuras/livro/{nome}.md", "tipo": "figura"}
    if attach:
        meta["attach_only"] = True
    if origem:
        meta["origem"] = origem
    return FakeDoc(texto, meta)


def _txt(nome: str, texto: str, origem: str = "") -> FakeDoc:
    meta = {"source": f"/vault/{nome}.md", "tipo": "texto", "confidence": 0.8}
    if origem:
        meta["origem"] = origem
    return FakeDoc(texto, meta)


class StoreComFiltro:
    """Chroma que filtra por metadado ANTES de cortar em k, e sabe responder ao
    `get(where=...)` com `$and`/`$in` que o anexo por co-locação usa."""

    def __init__(self, resultados):
        self._res = resultados
        self.filtros = []

    def similarity_search_with_score(self, consulta, k=4, filter=None):  # noqa: A002
        self.filtros.append(filter)
        itens = self._res
        if filter:
            itens = [(d, s) for d, s in itens
                     if all((d.metadata or {}).get(c) == v for c, v in filter.items())]
        return itens[:k]

    def get(self, where=None, include=None):
        def casa(md: dict) -> bool:
            for cond in (where or {}).get("$and", []):
                for chave, valor in cond.items():
                    if isinstance(valor, dict) and "$in" in valor:
                        if md.get(chave) not in valor["$in"]:
                            return False
                    elif md.get(chave) != valor:
                        return False
            return True

        return {"metadatas": [d.metadata for d, _ in self._res if casa(d.metadata or {})]}


# --- o gate: attach-only não concorre ---------------------------------------
async def test_attach_only_nao_entra_na_busca_de_figuras():
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_fig("boa", "clorose entre as nervuras"), 0.10),
        (_fig("muda", "planta em vaso", attach=True), 0.11),   # melhor que muita coisa
    ])
    aprovadas = await vs._buscar_figuras("clorose", set())
    assert [d.metadata["source"] for _s, d in aprovadas] == \
        ["/vault/Figuras/livro/boa.md"]


async def test_attach_only_nao_ganha_nem_sendo_a_MELHOR(monkeypatch):
    """O caso 'tricomas': o acervo não cobre o tema e a busca entrega a menos
    ruim. Se a menos ruim for attach-only, não há entrega nenhuma — que é o
    comportamento certo."""
    monkeypatch.setattr(settings, "figuras_margem_melhor", 0.0)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([(_fig("qualquer", "foto do capítulo", attach=True), 0.05)])
    assert await vs._buscar_figuras("tricomas", set()) == []


async def test_figura_sem_o_campo_continua_buscavel():
    """As ~1.735 notas já indexadas não têm `attach_only` — a ausência tem de
    significar 'buscável', senão a busca de figura zeraria de uma vez."""
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([(_fig("velha", "clorose entre nervuras"), 0.10)])
    assert len(await vs._buscar_figuras("clorose", set())) == 1


def test_metadado_attach_only_vem_da_TAG_no_corpo():
    meta = rag.metadados_da_nota("/v/Figuras/l/l_p0003_f1.md", CORPO_ANEXO, 1.0)
    assert meta["tipo"] == "figura" and meta["attach_only"] is True


def test_nota_de_figura_comum_nao_ganha_o_campo():
    meta = rag.metadados_da_nota("/v/Figuras/l/a.md", "## T\ncorpo", 1.0)
    assert "attach_only" not in meta


def test_tag_em_nota_de_TEXTO_nao_marca_attach_only():
    """A marca só faz sentido para figura; num átomo de texto seria confusão."""
    meta = rag.metadados_da_nota("/v/Notas/a.md", CORPO_ANEXO, 1.0)
    assert "attach_only" not in meta


# --- a volta: co-locação pela âncora de página ------------------------------
async def test_anexo_traz_a_figura_da_pagina_que_respondeu():
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "o solo vivo alimenta a planta", origem=ORIGEM), 0.10),
        (_fig("l_p0003_f1", "Figura da página 3", attach=True, origem=ORIGEM), 0.90),
    ])
    res = await vs.search("solo vivo")
    assert res.relevante
    assert res.anexos == ["/vault/Figuras/livro/l_p0003_f1.md"]
    # e ela NÃO virou fonte: não entra no contexto do LLM nem dispara promoção
    assert res.anexos[0] not in res.fontes
    assert "Figura da página 3" not in res.texto


async def test_anexo_ignora_figura_de_OUTRA_pagina():
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "o solo vivo alimenta a planta", origem=ORIGEM), 0.10),
        (_fig("outra", "Figura da página 90", attach=True,
              origem="Livro 'Enciclopédia' — Light (p. 90-90)"), 0.90),
    ])
    assert (await vs.search("solo vivo")).anexos == []


async def test_anexo_respeita_o_teto(monkeypatch):
    monkeypatch.setattr(settings, "figuras_anexo_max", 1)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "o solo vivo alimenta a planta", origem=ORIGEM), 0.10),
        (_fig("l_p0003_f1", "a", attach=True, origem=ORIGEM), 0.90),
        (_fig("l_p0003_f2", "b", attach=True, origem=ORIGEM), 0.91),
    ])
    assert len((await vs.search("solo vivo")).anexos) == 1


async def test_anexo_desligado_pelo_botao(monkeypatch):
    monkeypatch.setattr(settings, "figuras_anexo_max", 0)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "o solo vivo alimenta a planta", origem=ORIGEM), 0.10),
        (_fig("l_p0003_f1", "a", attach=True, origem=ORIGEM), 0.90),
    ])
    assert (await vs.search("solo vivo")).anexos == []


async def test_anexo_e_fail_soft_com_store_antigo():
    """Store sem `get(where=...)`: some o anexo, nunca a resposta."""
    class Antigo:
        def similarity_search_with_score(self, consulta, k=4, filter=None):  # noqa: A002
            if filter and filter.get("tipo") == "figura":
                return []
            return [(_txt("nota", "o solo vivo alimenta a planta"), 0.10)]

    vs = rag.VectorStore(embeddings=None)
    vs._store = Antigo()
    res = await vs.search("solo vivo")
    assert res.relevante and res.anexos == []
