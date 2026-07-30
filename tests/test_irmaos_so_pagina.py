"""
Expansão por página SÓ quando a `origem` é página de verdade.

Medido no índice real em 2026-07-30 (24.918 chunks): `origem` é a âncora de
co-locação, mas só o `livro.origem_do_job` escreve página. O átomo auto-colhido
guarda ali o BALDE de onde veio, e os baldes são enormes — 'Orquestração-de-IA-…json'
com 1.947 átomos, 'Sintese' com 684, 'Conversa' com 148. 54,2% do índice cai num
balde maior que `rag_max_irmaos`, e nesses o "irmão de página" era só a ordem do
Chroma: 12 átomos de assunto nenhum a ver, os MESMOS em perguntas diferentes.

Foi essa a mecânica que montou o contexto de "o que é topping" com notas de YOLO —
a palavra inglesa só aparece nesses baldes (0 vezes nas páginas do livro, contra 60
chunks de 'poda').
"""
import pytest

from mente_digital import livro
from mente_digital.config import settings
from mente_digital.rag import VectorStore


class _Doc:
    def __init__(self, texto, origem):
        self.page_content = texto
        self.metadata = {"origem": origem, "source": f"{texto}.md"}


class StoreEspiao:
    """Devolve um irmão por origem PEDIDA e registra o `where` recebido."""

    def __init__(self):
        self.wheres = []

    def get(self, where=None, include=None):
        self.wheres.append(where)
        pedidas = ((where or {}).get("origem") or {}).get("$in") or []
        return {
            "documents": [f"irmão de {o}" for o in pedidas],
            "metadatas": [{"origem": o, "source": f"{o}.md"} for o in pedidas],
        }


PAGINA = "Livro 'The Cannabis Encyclopedia' — Outdoors – Chapter 12 (p. 84-84)"
BALDE = "Otimização-do-Módulo-de-Visão-YOLO.json"


def _vs(store):
    vs = VectorStore.__new__(VectorStore)   # sem tocar disco/embedding
    vs._store = store
    return vs


# --- a régua pura -------------------------------------------------------------
def test_reconhece_pagina_de_livro():
    assert livro.e_origem_de_pagina(PAGINA) is True
    # o formato vem do próprio escritor da string (ponto único)
    assert livro.e_origem_de_pagina(
        livro.origem_do_job({"livro": "X", "titulo_cap": "cap", "pagina_inicio": 3, "pagina_fim": 4})
    ) is True


@pytest.mark.parametrize("origem", [BALDE, "Sintese", "Conversa", "Proativa", "TemaQuente", ""])
def test_balde_nao_e_pagina(origem):
    assert livro.e_origem_de_pagina(origem) is False


# --- o wiring na busca --------------------------------------------------------
async def test_balde_nao_expande():
    store = StoreEspiao()
    irmaos = await _vs(store)._irmaos_de_pagina([_Doc("átomo de yolo", BALDE)], set())
    assert irmaos == []
    assert store.wheres == []       # nem chega a consultar o Chroma (era ~2k docs)


async def test_pagina_de_livro_continua_expandindo():
    store = StoreEspiao()
    irmaos = await _vs(store)._irmaos_de_pagina([_Doc("átomo do livro", PAGINA)], set())
    assert len(irmaos) == 1
    assert irmaos[0][0] is None                     # irmão não promove nem vota no gate
    assert store.wheres == [{"origem": {"$in": [PAGINA]}}]


async def test_mistura_leva_so_a_pagina():
    store = StoreEspiao()
    docs = [_Doc("a", BALDE), _Doc("b", PAGINA), _Doc("c", "Sintese")]
    irmaos = await _vs(store)._irmaos_de_pagina(docs, set())
    assert [d.metadata["origem"] for _s, d in irmaos] == [PAGINA]


async def test_kill_switch_volta_a_expandir_balde(monkeypatch):
    monkeypatch.setattr("mente_digital.rag.settings.rag_irmaos_so_pagina", False)
    store = StoreEspiao()
    irmaos = await _vs(store)._irmaos_de_pagina([_Doc("átomo de yolo", BALDE)], set())
    assert len(irmaos) == 1                          # comportamento antigo, integral


async def test_teto_zero_continua_desligando():
    monkey = settings.rag_max_irmaos
    try:
        settings.rag_max_irmaos = 0
        store = StoreEspiao()
        assert await _vs(store)._irmaos_de_pagina([_Doc("x", PAGINA)], set()) == []
    finally:
        settings.rag_max_irmaos = monkey
