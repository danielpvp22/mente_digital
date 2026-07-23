"""
Fallback de busca web: buscar_com_fallback tenta cada backend em ordem e devolve
o 1º resultado não-vazio (corrige o ponto único de falha do DuckDuckGo).
"""
import asyncio

import pytest

from mente_digital.rag import (
    _chunk_texto,
    buscar_com_fallback,
    coletar_uteis,
    rankear_por_similaridade,
)


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


def test_vazio_com_sucesso_nao_vira_excecao():
    # 'auto' responde vazio (sucesso), 'html' falha, 'lite' vazio: é "nada encontrado",
    # não erro. Não deve propagar a exceção do 'html'.
    def fetch(backend):
        if backend == "html":
            raise RuntimeError("rate-limit")
        return []

    assert buscar_com_fallback(fetch, ["auto", "html", "lite"]) == []


# --- RAG efêmero sobre resultados web (deep-fetch) ---------------------------
def test_ranking_ordena_por_cosseno_desc():
    # Vetor da consulta aponta pra [1,0]; o trecho colinear vence o ortogonal.
    ranked = rankear_por_similaridade(
        [1.0, 0.0], [("longe", [0.0, 1.0]), ("perto", [2.0, 0.0]), ("meio", [1.0, 1.0])]
    )
    assert [t for _, t in ranked] == ["perto", "meio", "longe"]
    assert ranked[0][0] == pytest.approx(1.0)   # cosseno=1 (colinear)
    assert ranked[-1][0] == pytest.approx(0.0)  # cosseno=0 (ortogonal)


def test_ranking_nao_quebra_com_vetor_nulo():
    # Norma zero não pode virar divisão por zero (embedding degenerado).
    ranked = rankear_por_similaridade([0.0, 0.0], [("x", [0.0, 0.0])])
    assert ranked[0][0] == 0.0


def test_chunk_texto_fatia_e_limpa():
    chunks = _chunk_texto("palavra " * 400, chunk_size=600, chunk_overlap=80)
    assert len(chunks) >= 2
    assert all(c.strip() == c and c for c in chunks)  # sem borda em branco, sem vazio


def test_chunk_texto_vazio():
    assert _chunk_texto("   ", 600, 80) == []


# --- RACE-FIRST-K no deep-fetch (coletar_uteis) ------------------------------
async def test_coletar_uteis_pega_os_mais_rapidos_e_cancela_o_resto():
    # A ideia do dono: consulta N sites ao mesmo tempo, aceita os K primeiros que
    # responderem com texto útil, e CANCELA os lentos (não espera as fontes mortas).
    cancelados = []

    async def fetch(nome, atraso, resultado):
        try:
            await asyncio.sleep(atraso)
            return resultado
        except asyncio.CancelledError:
            cancelados.append(nome)
            raise

    coros = [
        fetch("rapido", 0.01, "A"),
        fetch("medio", 0.03, "B"),
        fetch("lento", 0.5, "C"),   # nunca deve terminar — é cancelado ao atingir K
    ]
    uteis, n_cancelados = await coletar_uteis(coros, aceitar=2, timeout=1.0)

    assert uteis == ["A", "B"]          # os 2 mais rápidos, em ordem de conclusão
    assert n_cancelados == 1
    assert cancelados == ["lento"]      # o lento foi de fato cancelado


async def test_coletar_uteis_timeout_por_pagina_descarta_a_travada():
    async def fetch(atraso, resultado):
        await asyncio.sleep(atraso)
        return resultado

    coros = [fetch(0.01, "A"), fetch(0.5, "B")]
    # B (0.5s) estoura o timeout por-página de 0.05s -> vira None (não contribui).
    uteis, n_cancelados = await coletar_uteis(coros, aceitar=2, timeout=0.05)

    assert uteis == ["A"]              # só a rápida entrou; K não foi atingido (degrada)
    assert n_cancelados == 0           # ambas resolveram (B via timeout), nada pendente


async def test_coletar_uteis_ignora_resultado_nao_util():
    async def fetch(atraso, resultado):
        await asyncio.sleep(atraso)
        return resultado

    # A 1ª volta None (página sem corpo após trafilatura): não conta; pega a próxima útil.
    coros = [fetch(0.01, None), fetch(0.02, "B")]
    uteis, _n = await coletar_uteis(coros, aceitar=1, timeout=1.0)

    assert uteis == ["B"]


async def test_coletar_uteis_nada_util_devolve_vazio():
    async def fetch(resultado):
        return resultado

    uteis, n_cancelados = await coletar_uteis([fetch(None), fetch("")], aceitar=2)
    assert uteis == []                 # nenhuma página teve corpo -> chamador cai nos snippets
    assert n_cancelados == 0
