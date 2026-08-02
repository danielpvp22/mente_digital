"""Grafo LOCAL da malha (grafo.vizinhanca_local) + a rota /api/vault/grafo.

O que estes testes existem para impedir:

- **O novelo.** Sem o corte por IDF, o primeiro salto a partir de qualquer nota
  traz todo mundo que menciona um conceito-hub, e o desenho fica idêntico para
  qualquer semente. É a mesma régua que a expansão de contexto já aplica
  (rag.py:1616, `malha_idf_min=4.0`), e ela precisa valer aqui também.
- **O grafo que muda sozinho.** `por_conceito` guarda `set`, cuja ordem varia
  entre execuções. Sem ordenação explícita, reabrir a mesma nota desenharia
  outra figura — e o teste passaria só às vezes.
- **O corte silencioso.** Um grafo que descarta metade dos vizinhos sem dizer se
  lê como "é só isso que existe".
"""
from __future__ import annotations

import math

import pytest

from mente_digital import grafo


# --------------------------------------------------------------------------- #
# Uma malha de brinquedo                                                       #
# --------------------------------------------------------------------------- #
# 6 átomos. "raro"/"raro2" ligam pares; "hub" está em 5 dos 6 — genérico, mas NÃO
# universal, de propósito: um conceito presente em 100% da base tem idf=0 e é
# descartado por outra guarda (ver test_conceito_universal_nunca_conta), o que
# esconderia o efeito do `idf_min` que estes testes querem medir.
CONCEITOS_DE = {
    "A.md": ["raro", "hub"],
    "B.md": ["raro", "hub"],
    "C.md": ["raro2", "hub"],
    "D.md": ["raro2", "hub"],
    "E.md": ["hub"],
    "F.md": ["so_dele"],
}
POR_CONCEITO = {
    "raro": {"A.md", "B.md"},                                   # idf = log(6/2) = 1,10
    "raro2": {"C.md", "D.md"},                                  # idf = 1,10
    "hub": {"A.md", "B.md", "C.md", "D.md", "E.md"},            # idf = log(6/5) = 0,18
    "so_dele": {"F.md"},                                        # idf = 1,79, mas sozinho
}
N = len(CONCEITOS_DE)


def _idf(c: str) -> float:
    df = len(POR_CONCEITO.get(c, ()))
    return math.log(N / df) if df else 0.0


def _viz(semente="A.md", saltos=1, max_nos=40, idf_min=0.7):
    return grafo.vizinhanca_local(POR_CONCEITO, CONCEITOS_DE, _idf,
                                  semente, saltos, max_nos, idf_min)


def _ids(g):
    return [n["id"] for n in g["nos"]]


# --------------------------------------------------------------------------- #
# O corte por IDF — o que separa grafo de novelo                               #
# --------------------------------------------------------------------------- #
def test_hub_nao_gera_vizinhanca():
    """"hub" está nos 6 átomos (idf=0): não é evidência de vizinhança nenhuma.
    Com ele valendo, A puxaria a base inteira."""
    g = _viz("A.md")
    assert _ids(g) == ["A.md", "B.md"]          # só o vínculo por "raro"


def test_sem_corte_o_hub_arrasta_todo_mundo():
    """O contraponto que prova que o teste acima mede o CORTE, e não outra coisa:
    baixando o `idf_min`, o mesmo "hub" puxa os quatro portadores de uma vez."""
    g = _viz("A.md", idf_min=0.0)
    assert set(_ids(g)) == {"A.md", "B.md", "C.md", "D.md", "E.md"}   # o novelo


def test_conceito_universal_nunca_conta():
    """Semântica separada do `idf_min`, e que vale mesmo com ele em zero: um
    conceito presente em TODO átomo tem idf = log(N/N) = 0 e não carrega
    informação nenhuma. Marcar todo mundo com [[IA]] não cria vizinhança."""
    conceitos = {"A.md": ["todos"], "B.md": ["todos"], "C.md": ["todos"]}
    por_conceito = {"todos": {"A.md", "B.md", "C.md"}}
    idf = lambda c: math.log(3 / len(por_conceito.get(c, ())))       # noqa: E731
    g = grafo.vizinhanca_local(por_conceito, conceitos, idf, "A.md", 1, 40, 0.0)
    assert _ids(g) == ["A.md"]


def test_atomo_so_com_hub_fica_sozinho():
    g = _viz("E.md")
    assert _ids(g) == ["E.md"] and g["arestas"] == []


# --------------------------------------------------------------------------- #
# Saltos                                                                       #
# --------------------------------------------------------------------------- #
def test_um_salto_nao_alcanca_o_segundo_anel():
    assert "C.md" not in _ids(_viz("A.md", saltos=1))


def test_dois_saltos_atravessam_pelo_atomo_do_meio():
    """A-B por 'raro'; B não leva a C. Aqui o caminho tem de existir por um átomo
    que compartilhe os dois conceitos — sem isso, 2 saltos = 1 salto."""
    conceitos = dict(CONCEITOS_DE, **{"B.md": ["raro", "raro2", "hub"]})
    por_conceito = {**POR_CONCEITO, "raro2": {"B.md", "C.md", "D.md"}}
    g = grafo.vizinhanca_local(por_conceito, conceitos, _idf, "A.md", 2, 40, 0.7)
    assert set(_ids(g)) >= {"A.md", "B.md", "C.md", "D.md"}
    assert dict((n["id"], n["salto"]) for n in g["nos"])["C.md"] == 2


def test_salto_registra_a_distancia():
    g = _viz("A.md", saltos=2)
    assert dict((n["id"], n["salto"]) for n in g["nos"])["A.md"] == 0


# --------------------------------------------------------------------------- #
# Arestas                                                                      #
# --------------------------------------------------------------------------- #
def test_par_de_notas_gera_UMA_aresta():
    """Duas notas que compartilham cinco conceitos não viram cinco linhas
    paralelas — informam o mesmo e escondem o resto do desenho."""
    conceitos = {"A.md": ["r1", "r2"], "B.md": ["r1", "r2"]}
    por_conceito = {"r1": {"A.md", "B.md"}, "r2": {"A.md", "B.md"}}
    g = grafo.vizinhanca_local(por_conceito, conceitos, lambda c: 2.0, "A.md", 1, 40, 0.7)
    assert len(g["arestas"]) == 1


def test_aresta_guarda_o_vinculo_mais_forte():
    conceitos = {"A.md": ["fraco", "forte"], "B.md": ["fraco", "forte"]}
    por_conceito = {"fraco": {"A.md", "B.md"}, "forte": {"A.md", "B.md"}}
    pesos = {"fraco": 1.0, "forte": 9.0}
    g = grafo.vizinhanca_local(por_conceito, conceitos, pesos.get, "A.md", 1, 40, 0.7)
    assert g["arestas"][0]["conceito"] == "forte" and g["arestas"][0]["peso"] == 9.0


def test_aresta_e_nao_direcionada_e_normalizada():
    """O par é ordenado alfabeticamente: sem isso A->B e B->A virariam duas."""
    a = _viz("A.md")["arestas"][0]
    assert (a["de"], a["para"]) == ("A.md", "B.md")


# --------------------------------------------------------------------------- #
# Determinismo, teto e bordas                                                  #
# --------------------------------------------------------------------------- #
def test_mesma_entrada_mesmo_desenho():
    """`por_conceito` guarda `set`. Sem ordenação explícita a tela mudaria
    sozinha ao reabrir a mesma nota."""
    assert [_ids(_viz("A.md", saltos=2)) for _ in range(5)].count(_ids(_viz("A.md", saltos=2))) == 5


def test_teto_de_nos_corta_e_AVISA():
    g = _viz("A.md", idf_min=0.0, max_nos=3)
    assert len(g["nos"]) == 3
    assert g["truncado"] is True
    assert g["alcancados"] > 2          # a tela precisa poder dizer "3 de N"


def test_sem_teto_nao_marca_truncado():
    assert _viz("A.md", idf_min=0.0)["truncado"] is False


def test_semente_fora_da_malha_devolve_vazio():
    g = _viz("NAO_EXISTE.md")
    assert g == {"nos": [], "arestas": [], "truncado": False, "alcancados": 0,
                 "arestas_totais": 0}


# --------------------------------------------------------------------------- #
# Teto de arestas — o novelo                                                   #
# --------------------------------------------------------------------------- #
def _malha_densa(n=12):
    """Todos ligados a todos por um conceito raro — a forma que um tema denso tem
    de verdade. Medido no vault: "fotossíntese" dá 39 vizinhos e ~700 arestas."""
    ids = [f"n{i:02d}.md" for i in range(n)]
    return ({"junto": set(ids)}, {i: ["junto"] for i in ids}, ids)


def test_tema_denso_gera_o_novelo_sem_teto():
    """O contraponto: sem teto, o número é verdadeiro e a figura é um disco."""
    por_c, conc, ids = _malha_densa()
    g = grafo.vizinhanca_local(por_c, conc, lambda c: 2.0, ids[0], 2, 40, 0.7)
    assert len(g["arestas"]) == 12 * 11 // 2       # grafo completo


def test_teto_de_arestas_corta_e_AVISA():
    por_c, conc, ids = _malha_densa()
    g = grafo.vizinhanca_local(por_c, conc, lambda c: 2.0, ids[0], 2, 40, 0.7,
                               max_arestas=20)
    assert len(g["arestas"]) == 20
    assert g["arestas_totais"] == 66               # a tela diz "20 das 66"


def test_teto_de_arestas_preserva_as_da_semente():
    """As ligações da nota ABERTA são a resposta à pergunta que abriu a tela.
    Cortá-las para caber deixaria a semente desconectada do próprio grafo."""
    por_c, conc, ids = _malha_densa()
    semente = ids[0]
    g = grafo.vizinhanca_local(por_c, conc, lambda c: 2.0, semente, 2, 40, 0.7,
                               max_arestas=15)
    da_semente = [a for a in g["arestas"] if semente in (a["de"], a["para"])]
    assert len(da_semente) == 11                   # TODAS as 11 sobreviveram


def test_sem_teto_de_arestas_o_total_bate_com_o_mostrado():
    por_c, conc, ids = _malha_densa()
    g = grafo.vizinhanca_local(por_c, conc, lambda c: 2.0, ids[0], 2, 40, 0.7)
    assert g["arestas_totais"] == len(g["arestas"])


def test_malha_vazia_nao_levanta():
    assert grafo.vizinhanca_local({}, {}, _idf, "A.md", 1, 40, 0.7)["nos"] == []


def test_max_nos_degenerado_devolve_vazio():
    assert _viz("A.md", max_nos=1)["nos"] == []


# --------------------------------------------------------------------------- #
# A rota                                                                       #
# --------------------------------------------------------------------------- #
class _MalhaFake:
    n_atomos = len(CONCEITOS_DE)

    def __init__(self) -> None:
        self.chamadas: list = []

    # Assinatura ESPELHADA de MalhaIndex.vizinhanca (rag.py). Um fake que aceita
    # menos parâmetros que o objeto real não simplifica o teste: esconde o
    # caminho, e só avisa no dia em que a chamada muda.
    def vizinhanca(self, semente, saltos, max_nos, idf_min, max_arestas=0):
        self.chamadas.append((semente, saltos, max_nos, idf_min, max_arestas))
        return grafo.vizinhanca_local(POR_CONCEITO, CONCEITOS_DE, _idf,
                                      semente, saltos, max_nos, idf_min, max_arestas)


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import main
    from mente_digital.config import settings

    vault = tmp_path / "vault"
    vault.mkdir()
    for nome in CONCEITOS_DE:
        (vault / nome).write_text(f"---\norigem: Sintese\ncolhido_em: 2026-07-17\n---\n## {nome}\n",
                                  encoding="utf-8")
    monkeypatch.setattr(settings, "caminho_obsidian", str(vault))
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(settings, "malha_idf_min", 0.7)
    monkeypatch.setattr(main, "_vault_cache", {"t": 0.0, "itens": [], "por_caminho": {}})

    malha = _MalhaFake()
    ctx = type("Ctx", (), {"vectorstore": type("VS", (), {"malha": malha})()})()
    main.app.state.ctx = ctx
    yield TestClient(main.app), malha
    del main.app.state.ctx


def test_rota_devolve_o_grafo_decorado(cliente):
    c, _ = cliente
    d = c.get("/api/vault/grafo", params={"caminho": "A.md"}).json()
    assert d["malha_pronta"] is True
    assert {n["id"] for n in d["nos"]} == {"A.md", "B.md"}
    # decoração: a tela precisa de nome e família para rotular e colorir
    assert all(n["nome"] and n["familia"] == "sintese" for n in d["nos"])


def test_rota_usa_o_corte_calibrado_do_projeto(cliente):
    """O `idf_min` do grafo é o MESMO da expansão de contexto. Divergir criaria
    duas noções de "vizinho forte" com o mesmo nome."""
    c, malha = cliente
    c.get("/api/vault/grafo", params={"caminho": "A.md"})
    assert malha.chamadas[-1][3] == 0.7        # settings.malha_idf_min


def _malha_absoluta(vault, monkeypatch):
    """Uma malha com as chaves NA FORMA DE PRODUÇÃO: caminho absoluto, barras
    normais. Medido no vault do dono em 2026-08-02 (rag.py:273 grava `path` cru,
    e ele chega absoluto) — eu tinha suposto relativo, e a rota devolvia zero nós."""
    import main

    raiz = str(vault).replace("\\", "/")
    a, b = f"{raiz}/A.md", f"{raiz}/B.md"
    conceitos = {a: ["raro"], b: ["raro"]}
    por_conceito = {"raro": {a, b}}
    malha = _MalhaFake()
    malha.vizinhanca = lambda s, sa, mx, idf, ma=0: grafo.vizinhanca_local(
        por_conceito, conceitos, lambda c: 2.0, s, sa, mx, idf, ma)
    main.app.state.ctx.vectorstore.malha = malha
    return malha


def test_rota_acha_a_nota_pelo_caminho_RELATIVO(cliente, tmp_path, monkeypatch):
    """O caso que quebrou de verdade. O modo "recentes" da tela devolve caminho
    RELATIVO (montado do rglob) e o índice guarda ABSOLUTO — clicar numa nota
    vinda dali dava `nos: 0`, e a tela dizia "esta nota não tem vizinhos fortes".
    Falha silenciosa e CONVINCENTE: a mensagem é plausível, então ninguém
    desconfia do índice."""
    c, _ = cliente
    _malha_absoluta(tmp_path / "vault", monkeypatch)
    d = c.get("/api/vault/grafo", params={"caminho": "A.md"}).json()
    assert len(d["nos"]) == 2


def test_rota_acha_a_nota_pelo_caminho_ABSOLUTO(cliente, tmp_path, monkeypatch):
    """A outra metade: o modo semântico devolve o `source` cru, absoluto. As duas
    formas circulam na mesma tela e as duas têm de funcionar."""
    c, _ = cliente
    _malha_absoluta(tmp_path / "vault", monkeypatch)
    alvo = str(tmp_path / "vault").replace("\\", "/") + "/A.md"
    d = c.get("/api/vault/grafo", params={"caminho": alvo}).json()
    assert len(d["nos"]) == 2


def test_rota_devolve_caminho_RELATIVO_para_a_tela(cliente, tmp_path, monkeypatch):
    """Clicar num vizinho tem de reabrir a nota. O id vem absoluto do índice; a
    tela precisa da forma que as outras rotas aceitam."""
    c, _ = cliente
    _malha_absoluta(tmp_path / "vault", monkeypatch)
    d = c.get("/api/vault/grafo", params={"caminho": "A.md"}).json()
    assert sorted(n["caminho"] for n in d["nos"]) == ["A.md", "B.md"]
    assert all(n["familia"] == "sintese" for n in d["nos"])   # decoração achou o cache


def test_rota_distingue_malha_nao_montada_de_nota_sem_vizinho(cliente):
    """A malha vive em RAM e é remontada em segundo plano no boot. Dizer "não tem
    vizinho" enquanto ela sobe seria mentira, e o dono concluiria que a nota está
    solta na base."""
    import main

    c, _ = cliente
    main.app.state.ctx.vectorstore.malha = type("Vazia", (), {"n_atomos": 0})()
    d = c.get("/api/vault/grafo", params={"caminho": "A.md"}).json()
    assert d["malha_pronta"] is False and d["nos"] == []


def test_rota_capa_saltos_e_nos(cliente):
    c, malha = cliente
    c.get("/api/vault/grafo", params={"caminho": "A.md", "saltos": 9, "max_nos": 9999})
    _semente, saltos, max_nos, _idf, max_arestas = malha.chamadas[-1]
    assert saltos == 2 and max_nos == 120
    assert max_arestas == 300              # 2,5x o teto de nós — trama sem mancha


def test_rota_com_nota_desconhecida_nao_e_erro(cliente):
    c, _ = cliente
    r = c.get("/api/vault/grafo", params={"caminho": "nao_existe.md"})
    assert r.status_code == 200 and r.json()["nos"] == []
