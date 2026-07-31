"""Figura reprovada na revalidação não disputa vaga na busca (2026-07-31).

A revalidação do idle (`EtlProcessor.revalidar_acervo_web`) marca `acervo_confere: NAO`
no frontmatter da nota cuja IMAGEM não é o que ela afirma ser — 5 das 45 notas do
acervo web real: um calendário do EBT da Flórida indexado como 'polvo', uma miniatura
de Rainbow Six Siege como 'solo argiloso' (o buscador casou o "solo" de *solo queue*),
um formulário de currículo em espanhol como 'estômato'.

Marcar era metade do trabalho: ninguém LIA a marca, então as 5 seguiam indexadas e
anexáveis a uma resposta. É o mesmo estágio que o `attach_only` já viveu ("declaração
e não comportamento") — e por isso este arquivo segue o molde de test_figura_attach_only.
"""
from mente_digital import rag
from mente_digital.config import settings

from conftest import FakeDoc

from test_figura_attach_only import StoreComFiltro


def _fig(nome: str, texto: str, suspeita: bool = False, origem: str = "") -> FakeDoc:
    meta = {"source": f"/vault/Figuras/_web/{nome}.md", "tipo": "figura"}
    if suspeita:
        meta["acervo_suspeito"] = True
    if origem:
        meta["origem"] = origem
    return FakeDoc(texto, meta)


# --- o metadado sai do frontmatter -------------------------------------------
def test_acervo_confere_NAO_vira_metadado():
    meta = rag.metadados_da_nota(
        "/v/Figuras/_web/polvo.md",
        "---\ntipo: figura\nacervo_confere: NAO\n---\n## Polvo\n![[x.webp]]\n", 1.0)
    assert meta["acervo_suspeito"] is True


def test_acervo_confere_sim_nao_ganha_o_campo():
    meta = rag.metadados_da_nota(
        "/v/Figuras/_web/folha.md",
        "---\ntipo: figura\nacervo_confere: sim\n---\n## Folha\n![[x.webp]]\n", 1.0)
    assert "acervo_suspeito" not in meta


def test_nota_sem_o_campo_nao_ganha_o_metadado():
    """As ~1.751 figuras de LIVRO nunca passam pela revalidação (ela só olha o acervo
    web). A ausência do campo tem de significar 'buscável', senão a busca zeraria."""
    meta = rag.metadados_da_nota("/v/Figuras/livro/a.md", "## T\ncorpo", 1.0)
    assert "acervo_suspeito" not in meta


# --- o gate: suspeita não concorre -------------------------------------------
async def test_suspeita_nao_entra_na_busca_de_figuras():
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_fig("boa", "polvo no fundo do mar"), 0.10),
        (_fig("ebt", "florida ebt reload schedule", suspeita=True), 0.09),  # MELHOR score
    ])
    aprovadas = await vs._buscar_figuras("polvo", set())
    assert [d.metadata["source"] for _s, d in aprovadas] == ["/vault/Figuras/_web/boa.md"]


async def test_suspeita_nao_ganha_nem_sendo_a_UNICA(monkeypatch):
    """Sem figura boa, o certo é não entregar nada — entregar a reprovada é dar a
    figura errada com a confiança da certa."""
    monkeypatch.setattr(settings, "figuras_margem_melhor", 0.0)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([(_fig("r6", "rainbow six siege console tourney",
                                      suspeita=True), 0.05)])
    assert await vs._buscar_figuras("solo argiloso", set()) == []


async def test_figura_sem_a_marca_continua_buscavel():
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([(_fig("velha", "polvo no fundo do mar"), 0.10)])
    assert len(await vs._buscar_figuras("polvo", set())) == 1


# --- o OUTRO caminho de anexo tem de honrar a mesma marca ---------------------
async def test_suspeita_nao_volta_pela_co_locacao(monkeypatch):
    """O anexo por co-locação entra pela PÁGINA, sem precisar casar a palavra — seria
    a porta dos fundos perfeita para a figura que a busca acabou de barrar."""
    monkeypatch.setattr(settings, "figuras_anexo_so_attach_only", False)
    # Formato de PÁGINA de livro: a co-locação só aceita origem que seja página
    # (é o gate "irmão de página só quando é página"), então é ele que exercita o filtro.
    origem = "Livro 'Enciclopédia' — Soil (p. 3-3)"
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_fig("ok", "polvo no fundo do mar", origem=origem), 0.10),
        (_fig("ebt", "florida ebt reload schedule", suspeita=True, origem=origem), 0.08),
    ])
    # `docs` são os átomos de TEXTO que responderam — a co-locação tira a origem deles.
    respondeu = FakeDoc("o polvo tem oito braços",
                        {"source": "/vault/polvo.md", "tipo": "texto", "origem": origem})
    saida = await vs._anexos_colocados([respondeu], consulta="polvo")
    assert saida == ["/vault/Figuras/_web/ok.md"]
