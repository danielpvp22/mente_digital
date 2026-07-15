"""
Fallback de busca web: buscar_com_fallback tenta cada backend em ordem e devolve
o 1º resultado não-vazio (corrige o ponto único de falha do DuckDuckGo).
"""
import pytest

from rag import buscar_com_fallback


def test_usa_primeiro_backend_que_retorna():
    chamados = []

    def fetch(backend):
        chamados.append(backend)
        return [{"r": backend}] if backend == "html" else []

    res = buscar_com_fallback(fetch, ["auto", "html", "lite"])
    assert res == [{"r": "html"}]
    assert chamados == ["auto", "html"]   # parou ao achar; nem tentou 'lite'


def test_pula_backend_que_lanca_excecao():
    def fetch(backend):
        if backend == "auto":
            raise RuntimeError("rate limit")
        if backend == "html":
            return [{"ok": True}]
        return []

    res = buscar_com_fallback(fetch, ["auto", "html", "lite"])
    assert res == [{"ok": True}]


def test_todos_vazios_retorna_lista_vazia():
    res = buscar_com_fallback(lambda b: [], ["auto", "html", "lite"])
    assert res == []


def test_todos_falham_propaga_ultimo_erro():
    def fetch(backend):
        raise RuntimeError(f"falhou-{backend}")

    with pytest.raises(RuntimeError, match="falhou-lite"):
        buscar_com_fallback(fetch, ["auto", "html", "lite"])
