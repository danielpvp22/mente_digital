"""Figuras dos livros — Fase 5a/5b (2026-07-25).

Sem PDF/PIL reais nos testes puros; a conversão WebP e a rota são exercitadas com
uma imagem minúscula gerada na hora. O que se trava: o filtro de decoração, o
casamento de legendas, a distribuição por capítulo, o bloco markdown, e — o ponto
sensível — a rota de imagem NÃO servir arquivo de fora do vault nem .md/.db.
"""
import pytest

from mente_digital import figuras
from mente_digital.config import settings


# ---------- puro ---------------------------------------------------------------
def test_vale_a_pena_corta_decoracao():
    assert figuras.vale_a_pena(400, 300, 200)
    assert not figuras.vale_a_pena(400, 12, 200)     # filete horizontal
    assert not figuras.vale_a_pena(16, 16, 200)      # ícone


def test_nome_arquivo_e_estavel_e_ordenavel():
    assert figuras.nome_arquivo("raven", 59, 1) == "raven_p0059_f1.webp"
    # Reprocessar o mesmo livro sobrescreve em vez de duplicar.
    assert figuras.nome_arquivo("raven", 59, 1) == figuras.nome_arquivo("raven", 59, 1)


def test_casar_legendas_varia_formatos():
    txt = ("Figura 4.1 — Ciclo de Calvin nas plantas C3\n"
           "texto solto no meio\n"
           "Fig. 12: Corte transversal da raiz\n"
           "FIGURA 3-2 - Estômato aberto\n"
           "Figurinha qualquer sem número")
    legs = figuras.casar_legendas(txt)
    # A LEGENDA de verdade tem precedência sobre a menção no corpo.
    assert legs["4.1"].startswith("Ciclo de Calvin")
    assert legs["12"].startswith("Corte transversal")
    assert legs["3-2"].startswith("Estômato")


def test_casar_legendas_cai_para_a_mencao_no_corpo():
    """Caso REAL do Raven: a legenda está dentro da imagem e a camada de texto só
    traz a referência. A frase que cita a figura vira o contexto de busca (medido:
    1/60 figuras com legenda-linha; 18/60 contando as menções)."""
    txt = "fazer uso direto da energia solar — isto é, o processo de fotossíntese (Figura 1.5)."
    legs = figuras.casar_legendas(txt)
    assert "1.5" in legs and "fotossíntese" in legs["1.5"]


def test_legenda_para_pareia_ou_junta():
    legs = ["Ciclo de Calvin", "Corte da raiz"]
    # Tantas legendas quanto figuras -> pareia na ordem de leitura.
    assert figuras.legenda_para(legs, 0, 2) == "Ciclo de Calvin"
    assert figuras.legenda_para(legs, 1, 2) == "Corte da raiz"
    # Números divergentes -> devolve todas (contexto impreciso > contexto nenhum).
    assert figuras.legenda_para(legs, 0, 3) == "Ciclo de Calvin | Corte da raiz"
    assert figuras.legenda_para([], 0, 2) == ""


def test_figuras_do_intervalo_respeita_o_capitulo():
    todas = [{"pagina": 5}, {"pagina": 12}, {"pagina": 30}]
    assert figuras.figuras_do_intervalo(todas, 10, 20) == [{"pagina": 12}]
    assert figuras.figuras_do_intervalo(todas, 5, 5) == [{"pagina": 5}]   # inclusivo
    assert figuras.figuras_do_intervalo(todas, 40, 50) == []


def test_wikilink_separa_por_livro():
    """Uma pasta por livro no vault (pedido do dono): o Obsidian não vira um
    depósito único com milhares de imagens misturadas."""
    md = figuras.bloco_markdown(
        [{"arquivo": "raven/raven_p0059_f1.webp", "pagina": 59, "legenda": "Ciclo"}],
        "Figuras")
    assert "![[Figuras/raven/raven_p0059_f1.webp]]" in md


def test_bloco_markdown_aponta_para_a_PASTA_DO_LIVRO():
    """Defeito medido em 2026-07-28: as 307 notas-síntese saíram com TODAS as
    figuras quebradas ('não foi encontrado' no Obsidian), porque o job carrega
    só o NOME do arquivo e o extrator grava uma pasta por livro."""
    md = figuras.bloco_markdown(
        [{"arquivo": "raven_p0059_f1.webp", "pagina": 59, "legenda": "L"}], "Figuras")
    assert "![[Figuras/raven/raven_p0059_f1.webp]]" in md
    assert "![[Figuras/raven_p0059_f1.webp]]" not in md


def test_bloco_markdown_nao_quebra_com_nome_fora_da_convencao():
    md = figuras.bloco_markdown([{"arquivo": "solta.webp", "pagina": 1}], "Figuras")
    assert "![[Figuras/solta.webp]]" in md      # sem slug derivável: fica como está


def test_bloco_markdown_usa_wikilink_e_legenda_como_texto():
    md = figuras.bloco_markdown(
        [{"arquivo": "raven_p0059_f1.webp", "pagina": 59, "legenda": "Ciclo de Calvin"}],
        "Figuras")
    # o wikilink é nativo do Obsidian E aponta para a pasta do livro (2026-07-28:
    # a asserção antiga fixava o caminho PLANO, que não resolve em disco)
    assert "![[Figuras/raven/raven_p0059_f1.webp]]" in md
    assert "Ciclo de Calvin" in md                        # legenda pesquisável pelo RAG
    assert figuras.bloco_markdown([], "Figuras") == ""    # sem figura, sem seção


def test_para_webp_encolhe_e_recusa_lixo():
    PIL = pytest.importorskip("PIL")
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), (120, 30, 200)).save(buf, format="PNG")
    png = buf.getvalue()
    webp = figuras.para_webp(png, qualidade=80, max_lado=1600)
    assert webp and len(webp) < len(png)                  # WebP menor que o PNG
    assert Image.open(io.BytesIO(webp)).size[0] == 1600   # reamostrado pelo max_lado
    assert figuras.para_webp(b"nao sou imagem", 80, 1600) is None   # sujeira do PDF
    assert PIL is not None


# ---------- rota /api/imagem (Fase 5b) -----------------------------------------
@pytest.fixture()
def cliente(monkeypatch, tmp_path):
    """App real com o vault apontado para tmp — o ponto é a rota, não o pipeline."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    vault = tmp_path / "vault"
    (vault / "Figuras").mkdir(parents=True)
    (vault / "Figuras" / "ok.webp").write_bytes(b"RIFF0000WEBPVP8 ")
    (vault / "segredo.md").write_text("nota privada", encoding="utf-8")
    (tmp_path / "fora.webp").write_bytes(b"fora do vault")
    monkeypatch.setattr(settings, "caminho_obsidian", str(vault))
    monkeypatch.setattr(settings, "access_token", "")     # loopback já autoriza
    import main
    return fastapi_testclient.TestClient(main.app), tmp_path


def test_rota_serve_figura_do_vault(cliente):
    c, _ = cliente
    r = c.get("/api/imagem/Figuras/ok.webp")
    assert r.status_code == 200 and r.content.startswith(b"RIFF")


def test_rota_bloqueia_traversal_e_arquivo_nao_imagem(cliente):
    c, _ = cliente
    # Path traversal: sairia do vault e vazaria arquivo do disco.
    for tentativa in ("../fora.webp", "..%2Ffora.webp", "Figuras/../../fora.webp"):
        assert c.get(f"/api/imagem/{tentativa}").status_code == 404
    # Extensão fora da allowlist: nada de servir .md/.db por aqui.
    assert c.get("/api/imagem/segredo.md").status_code == 404
    assert c.get("/api/imagem/Figuras/nao_existe.webp").status_code == 404
