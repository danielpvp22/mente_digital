"""Filtros do navegador do vault (pasta / origem / data).

O que estes testes travam, e por que cada um existe:

- A taxonomia de ORIGEM saiu de medir o vault do dono (191 valores distintos que
  caem em 8 famílias cobrindo 100% das 24.838 notas). Um `familia_de_origem` que
  passasse a devolver "outro" para livro ou importado moveria 23.431 notas de
  gaveta sem nada falhar.
- A data usada é `colhido_em`, do frontmatter — NÃO o `mtime`. Medido: nenhuma
  nota do vault tem mtime acima de 30 dias, porque as passadas de manutenção
  reescrevem tudo. Um filtro por mtime responderia "quando um script passou",
  que não é a pergunta.
"""
from __future__ import annotations

import pytest

from mente_digital import vault_filtros as vf


# --------------------------------------------------------------------------- #
# Família de origem                                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("origem, esperado", [
    ("Livro 'The Cannabis Encyclopedia (Jorge Cervantes)' — Soil (p. 1-1)", "livro"),
    ("Divulgação-Inicial-de-Ideias-de-Negócios.json", "importado"),
    ("IA-para-Táticas-de-Combate-CQB.JSON", "importado"),      # caixa não importa
    ("Sintese", "sintese"),
    ("Síntese", "sintese"),                                    # com acento também
    ("Proativa", "proativa"),
    ("Conversa", "conversa"),
    ("TemaQuente", "temaquente"),
    ("Consolidação de 11 átomos (Sintese_...)", "outro"),
    ("", "sem_origem"),
    ("   ", "sem_origem"),
])
def test_familia_de_origem(origem, esperado):
    assert vf.familia_de_origem(origem) == esperado


def test_livro_vence_o_sufixo_json():
    """Ordem dos testes dentro da função: um título de livro poderia terminar em
    .json, e o contrário não acontece."""
    assert vf.familia_de_origem("Livro 'Alguma coisa.json'") == "livro"


def test_toda_familia_tem_rotulo_de_tela():
    """Uma família sem rótulo apareceria na lista suspensa com o nome interno."""
    assert set(vf.FAMILIAS) == set(vf.ROTULOS)


# --------------------------------------------------------------------------- #
# Pasta                                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("caminho, esperado", [
    ("Conhecimento_Novo/Sintese_esp32_1.md", "Conhecimento_Novo"),
    ("Importado_Gemini/sub/pasta/nota.md", "Importado_Gemini"),
    ("Figuras/obra/fig.md", "Figuras"),
    ("Inbox_Captura.md", vf.RAIZ),
    ("Conhecimento_Novo\\barra\\invertida.md", "Conhecimento_Novo"),
])
def test_pasta_de(caminho, esperado):
    assert vf.pasta_de(caminho) == esperado


# --------------------------------------------------------------------------- #
# Leitura do cabeçalho                                                         #
# --------------------------------------------------------------------------- #
def _nota(tmp_path, texto: str):
    alvo = tmp_path / "n.md"
    alvo.write_text(texto, encoding="utf-8")
    return alvo


def test_le_origem_e_data_do_frontmatter(tmp_path):
    p = _nota(tmp_path, "---\norigem: Sintese\ncolhido_em: 2026-07-17\n---\n## Titulo\ncorpo\n")
    assert vf.ler_cabecalho(p) == {"origem": "Sintese", "colhido_em": "2026-07-17"}


def test_origem_com_dois_pontos_no_valor_nao_e_cortada(tmp_path):
    """O título do livro tem travessão, parênteses e pode ter ':'. O split é no
    PRIMEIRO ':' — sem isso, metade da origem sumiria."""
    p = _nota(tmp_path, "---\norigem: Livro 'X': cap 2\ncolhido_em: 2026-07-27\n---\n")
    assert vf.ler_cabecalho(p)["origem"] == "Livro 'X': cap 2"


def test_nota_sem_frontmatter(tmp_path):
    p = _nota(tmp_path, "# Inbox\n\n- [2026-07-18] teste\n")
    assert vf.ler_cabecalho(p) == {"origem": "", "colhido_em": ""}


def test_arquivo_inexistente_nao_levanta(tmp_path):
    """Nota some entre o rglob e o open o tempo todo num vault que o próprio app
    escreve. Um filtro de navegação não pode derrubar o painel."""
    assert vf.ler_cabecalho(tmp_path / "nao_existe.md") == {"origem": "", "colhido_em": ""}


def test_nao_le_o_corpo_inteiro(tmp_path):
    """O cabeçalho é lido com teto de linhas: uma nota grande cujo corpo tivesse
    'origem:' não pode contaminar o campo."""
    corpo = "\n".join(["linha"] * 200 + ["origem: mentira"])
    p = _nota(tmp_path, f"---\norigem: Sintese\n---\n{corpo}")
    assert vf.ler_cabecalho(p)["origem"] == "Sintese"


# --------------------------------------------------------------------------- #
# O filtro                                                                     #
# --------------------------------------------------------------------------- #
def _meta(pasta="Conhecimento_Novo", familia="sintese", colhido="2026-07-17"):
    return {"pasta": pasta, "familia": familia, "colhido_em": colhido}


def test_sem_filtro_tudo_passa():
    assert vf.passa(_meta()) is True
    assert vf.passa({}) is True


def test_filtro_de_pasta():
    assert vf.passa(_meta(), pasta="Conhecimento_Novo") is True
    assert vf.passa(_meta(), pasta="Figuras") is False


def test_filtro_de_familia():
    assert vf.passa(_meta(), familia="sintese") is True
    assert vf.passa(_meta(), familia="livro") is False


def test_filtro_de_data_compara_iso_lexicalmente():
    assert vf.passa(_meta(colhido="2026-07-17"), desde="2026-07-01") is True
    assert vf.passa(_meta(colhido="2026-07-17"), desde="2026-07-17") is True   # inclusivo
    assert vf.passa(_meta(colhido="2026-06-30"), desde="2026-07-01") is False


def test_data_ausente_nao_passa_no_filtro_de_data():
    """Escolha declarada: deixar passar faria 'colhido nos últimos 7 dias'
    devolver as notas cuja data ninguém conhece, sem o dono saber."""
    assert vf.passa(_meta(colhido=""), desde="2026-07-01") is False
    assert vf.passa(_meta(colhido="2026-07"), desde="2026-07-01") is False    # truncada


def test_data_ausente_passa_quando_nao_ha_filtro_de_data():
    assert vf.passa(_meta(colhido=""), familia="sintese") is True


def test_filtros_sao_conjuncao():
    m = _meta(pasta="Figuras", familia="livro", colhido="2026-07-27")
    assert vf.passa(m, pasta="Figuras", familia="livro", desde="2026-07-01") is True
    assert vf.passa(m, pasta="Figuras", familia="sintese", desde="2026-07-01") is False


# --------------------------------------------------------------------------- #
# Descrição e facetas                                                          #
# --------------------------------------------------------------------------- #
def test_descrever_junta_caminho_e_cabecalho():
    d = vf.descrever("Figuras/obra/f.md",
                     {"origem": "Livro 'X'", "colhido_em": "2026-07-27T10:00"})
    assert d["pasta"] == "Figuras"
    assert d["familia"] == "livro"
    assert d["colhido_em"] == "2026-07-27"        # truncado para o dia


def test_facetas_contam_e_ordenam():
    metas = [_meta(), _meta(), _meta(pasta="Figuras", familia="livro")]
    f = vf.contar_facetas(metas)
    assert f["total"] == 3
    assert f["pastas"][0] == {"valor": "Conhecimento_Novo", "n": 2}   # maior primeiro
    assert [x["valor"] for x in f["familias"]] == ["livro", "sintese"]   # ordem de FAMILIAS


def test_facetas_nao_oferecem_familia_vazia():
    """Oferecer uma família sem nota nenhuma é oferecer um filtro que devolve
    zero — o usuário conclui que a busca quebrou."""
    f = vf.contar_facetas([_meta(familia="sintese")])
    assert [x["valor"] for x in f["familias"]] == ["sintese"]


def test_facetas_de_vault_vazio():
    f = vf.contar_facetas([])
    assert f == {"pastas": [], "familias": [], "total": 0}


# --------------------------------------------------------------------------- #
# As rotas (/api/notas com filtro, /api/vault/facetas)                         #
# --------------------------------------------------------------------------- #
@pytest.fixture
def cliente(tmp_path, monkeypatch):
    """Vault de mentira + TestClient sem lifespan (não carrega modelo nenhum)."""
    from fastapi.testclient import TestClient

    import main
    from mente_digital.config import settings

    vault = tmp_path / "vault"
    for pasta, nome, origem, data in [
        ("Conhecimento_Novo", "a", "Sintese", "2026-07-17"),
        ("Conhecimento_Novo", "b", "Proativa", "2026-06-01"),
        ("Figuras", "c", "Livro 'X' — cap 1", "2026-07-27"),
        ("Importado_Gemini", "d", "Conversa-Antiga.json", "2026-05-02"),
    ]:
        alvo = vault / pasta / f"{nome}.md"
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(f"---\norigem: {origem}\ncolhido_em: {data}\n---\n## {nome}\n",
                        encoding="utf-8")
    (vault / "solta.md").write_text("# sem frontmatter\n", encoding="utf-8")

    monkeypatch.setattr(settings, "caminho_obsidian", str(vault))
    monkeypatch.setattr(settings, "access_token", "")        # loopback já autoriza
    # O cache é de MÓDULO: sem zerar, um teste herdaria a varredura do outro.
    monkeypatch.setattr(main, "_vault_cache", {"t": 0.0, "itens": [], "por_caminho": {}})
    return TestClient(main.app)


def test_facetas_saem_dos_dados(cliente):
    f = cliente.get("/api/vault/facetas").json()
    assert f["total"] == 5
    assert dict((p["valor"], p["n"]) for p in f["pastas"]) == {
        "Conhecimento_Novo": 2, "Figuras": 1, "Importado_Gemini": 1, vf.RAIZ: 1}
    assert [x["valor"] for x in f["familias"]] == [
        "livro", "importado", "sintese", "proativa", "sem_origem"]


def test_sem_filtro_lista_tudo(cliente):
    r = cliente.get("/api/notas").json()
    assert r["modo"] == "recentes" and r["total_filtrado"] == 5


def test_filtro_de_pasta_na_rota(cliente):
    r = cliente.get("/api/notas", params={"pasta": "Conhecimento_Novo"}).json()
    assert {i["nome"] for i in r["itens"]} == {"a", "b"}


def test_filtro_de_familia_na_rota(cliente):
    r = cliente.get("/api/notas", params={"origem": "livro"}).json()
    assert [i["nome"] for i in r["itens"]] == ["c"]


def test_filtro_de_dias_usa_colhido_em_nao_mtime(cliente):
    """O ponto do recurso: os cinco arquivos foram ESCRITOS agora (mtime de
    segundos atrás), mas `colhido_em` os espalha por meses. Um filtro por mtime
    devolveria os cinco em qualquer janela — este devolve dois."""
    from datetime import date

    dias = (date.today() - date(2026, 7, 1)).days
    r = cliente.get("/api/notas", params={"dias": dias}).json()
    assert {i["nome"] for i in r["itens"]} == {"a", "c"}


def test_filtros_combinam_na_rota(cliente):
    r = cliente.get("/api/notas",
                    params={"pasta": "Conhecimento_Novo", "origem": "proativa"}).json()
    assert [i["nome"] for i in r["itens"]] == ["b"]


def test_itens_trazem_familia_e_data_para_a_tela(cliente):
    item = next(i for i in cliente.get("/api/notas").json()["itens"] if i["nome"] == "c")
    assert item["familia"] == "livro" and item["colhido_em"] == "2026-07-27"


def test_filtro_sem_resultado_devolve_lista_vazia_e_nao_erro(cliente):
    r = cliente.get("/api/notas", params={"pasta": "Nao_Existe"})
    assert r.status_code == 200 and r.json()["itens"] == []
