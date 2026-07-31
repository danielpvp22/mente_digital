"""Figuras no contexto: busca em espaço próprio + gate adaptativo (2026-07-26).

Por que este arquivo existe, em números medidos no banco real (33.396 chunks):
- disputando as mesmas vagas, a figura PERDIA — uma a 0,1239 ficava fora de um
  top-40 cujo pior era 0,1373, porque a busca aproximada não a alcançava;
- quando ganhava, ganhava demais: "como podar a planta" tomava 16 das 40 vagas.

A correção não é cota fixa (forçaria imagem em pergunta que não pede e travaria a
que precisa de mais): é buscar à parte e deixar o GATE decidir quantas entram.
"""
from mente_digital import rag
from mente_digital.config import settings

from conftest import FakeDoc


def _fig(nome: str, texto: str) -> FakeDoc:
    return FakeDoc(texto, {"source": f"/vault/Figuras/livro/{nome}.md", "tipo": "figura"})


def _txt(nome: str, texto: str) -> FakeDoc:
    return FakeDoc(texto, {"source": f"/vault/{nome}.md", "tipo": "texto", "confidence": 0.8})


class StoreComFiltro:
    """Chroma que aplica `filter` por metadado ANTES de cortar em k — o que a
    medição mostrou que o Chroma real faz (recall exato dentro do subconjunto)."""

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


# --- `tipo` no metadado: o que torna o filtro possível ----------------------
def test_e_figura_olha_a_PASTA_nao_o_nome_do_arquivo():
    assert rag._e_figura("/vault/Figuras/livro/x_p0001_f1.md")
    # a armadilha do `subpasta in path`: nota de TEXTO com 'Figuras' no nome
    assert not rag._e_figura("/vault/Notas/Figuras_do_livro.md")
    assert not rag._e_figura("/vault/Notas/comum.md")


def test_e_figura_aceita_barra_do_windows():
    assert rag._e_figura(r"D:\vault\Figuras\livro\x_p0001_f1.md")


def test_metadado_marca_os_DOIS_lados():
    # 'texto' precisa ser afirmativo: filtro sobre chave AUSENTE não casa no Chroma
    assert rag.metadados_da_nota("/v/Figuras/l/a.md", "## T\ncorpo", 1.0)["tipo"] == "figura"
    assert rag.metadados_da_nota("/v/Notas/a.md", "## T\ncorpo", 1.0)["tipo"] == "texto"


# --- rótulo: é ele que faz a imagem chegar à tela ---------------------------
def test_rotulo_da_figura_avisa_que_a_imagem_ja_vai_aparecer():
    # Antes este rótulo mandava COPIAR o wikilink. O teste real mostrou que não
    # funciona (resposta cabia em 90 tokens, o embed gasta ~40) e que, se
    # funcionasse, duplicaria a imagem — o servidor já anexa.
    r = rag.rotular_contexto(0.1, _fig("f1", "![[Figuras/livro/f1.webp]] folha com clorose"))
    assert "já é mostrada" in r and "NÃO repita o link" in r


def test_rotulo_distingue_texto_e_vizinho_da_malha():
    assert "Confiança" in rag.rotular_contexto(0.1, _txt("a", "corpo"))
    assert "Malha" in rag.rotular_contexto(None, _txt("a", "corpo"))


# --- o gate: QUANTAS entram é da distância, não de cota ---------------------
async def test_gate_admite_so_as_figuras_confiantes(monkeypatch):
    # corte relativo desligado: aqui é o limiar ABSOLUTO que está sob teste
    monkeypatch.setattr(settings, "figuras_margem_melhor", 0.0)
    # Limiar CRAVADO (mesmo idioma do teste irmão logo abaixo). Antes ele herdava o
    # `rag_score_confident` e as distâncias eram lidas contra o default do código —
    # o que amarrava este teste à escala do embedding vigente. O que está sob teste é
    # "abaixo do limiar entra, acima do rag_score_max não", não qual é o limiar.
    monkeypatch.setattr(settings, "figuras_score_confident", 0.5)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_fig("boa", "clorose entre as nervuras"), 0.10),
        (_fig("media", "planta em vaso"), 0.30),
        (_fig("ruim", "assunto totalmente outro"), 1.60),   # acima do rag_score_max
    ])
    aprovadas = await vs._buscar_figuras("clorose", set())
    assert [round(s, 2) for s, _ in aprovadas] == [0.10, 0.30]
    assert vs._store.filtros == [{"tipo": "figura"}]


async def test_gate_devolve_ZERO_quando_nada_e_relevante(monkeypatch):
    monkeypatch.setattr(settings, "figuras_score_confident", 0.15)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([(_fig("a", "assunto distante"), 0.90)])
    # nem confiança (0.90 > 0.15) nem aterramento léxico -> nenhuma figura
    assert await vs._buscar_figuras("clorose", {"clorose"}) == []


async def test_gate_aceita_por_ATERRAMENTO_mesmo_sem_confianca(monkeypatch):
    monkeypatch.setattr(settings, "figuras_score_confident", 0.15)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([(_fig("a", "foto de clorose na folha"), 0.90)])
    assert len(await vs._buscar_figuras("clorose", {"clorose"})) == 1


async def test_gate_desligado_pelo_botao(monkeypatch):
    monkeypatch.setattr(settings, "figuras_no_contexto", False)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([(_fig("a", "clorose"), 0.05)])
    assert await vs._buscar_figuras("clorose", set()) == []


async def test_gate_e_fail_soft_com_store_sem_filter():
    """Store antigo (os fakes da suíte) não aceita `filter`: some a figura, não a busca."""
    class Antigo:
        def similarity_search_with_score(self, consulta, k=4):
            raise TypeError("sem filter")

    vs = rag.VectorStore(embeddings=None)
    vs._store = Antigo()
    assert await vs._buscar_figuras("clorose", set()) == []


# --- integração com o search: ilustra, nunca ancora -------------------------
async def test_figura_entra_no_contexto_quando_o_texto_ancorou():
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "a clorose aparece nas folhas velhas"), 0.10),
        (_fig("f1", "![[Figuras/livro/f1.webp]] clorose entre nervuras"), 0.12),
    ])
    res = await vs.search("clorose")
    assert res.relevante
    assert "![[Figuras/livro/f1.webp]]" in res.texto      # o wikilink chega ao LLM
    assert "Figura do acervo" in res.texto


async def test_figura_NAO_vira_cache_hit_sozinha():
    """Sem âncora de texto a figura não entra — senão o 'Cache Hit falso' volta
    por outra porta, agora pela imagem."""
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_fig("f1", "clorose entre nervuras"), 0.12),      # só figura, nenhum texto
    ])
    res = await vs.search("assunto que nenhum texto cobre")
    assert res.relevante is False
    assert "Figura do acervo" not in res.texto


async def test_figura_usada_PROMOVE_e_entra_em_fontes():
    # diferente do vizinho da malha: passou o mesmo gate do texto, é reuso real,
    # então o ciclo de maturidade (#conhecimento_novo) deve tirar a tag dela.
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "a clorose aparece nas folhas velhas"), 0.10),
        (_fig("f1", "clorose entre nervuras"), 0.12),
    ])
    res = await vs.search("clorose")
    assert any("Figuras" in f for f in res.fontes)


async def test_figura_ja_trazida_pelo_texto_nao_duplica():
    mesma = "![[Figuras/livro/f1.webp]] clorose entre nervuras"
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "a clorose aparece nas folhas velhas"), 0.10),
        (FakeDoc(mesma, {"source": "/vault/Figuras/livro/f1.md", "tipo": "figura"}), 0.12),
    ])
    res = await vs.search("clorose")
    assert res.texto.count(mesma) == 1


# --- a imagem é para os OLHOS: o TTS não pode falar o caminho do arquivo ------
# Defeito pego antes do teste real (2026-07-26): o contexto manda o modelo copiar
# o `![[...]]`, mas o mesmo texto vai para a fala. O `_STRIP_MD` só apaga colchetes
# e o `_ALLOWED` do Piper preserva letra/`_`/`-`, então a resposta sairia falada
# como "Figuras livro x p0288 f1 ponto webp".

FALA = "Veja ![[Figuras/livro/x_p0288_f1.webp]] a clorose entre as nervuras."


def test_embed_de_imagem_nao_chega_a_fala():
    from mente_digital.textutils import sem_embeds_de_imagem

    limpo = sem_embeds_de_imagem(FALA)
    assert "webp" not in limpo and "[[" not in limpo and "Figuras" not in limpo
    assert "Veja" in limpo and "clorose entre as nervuras" in limpo


def test_embed_vira_espaco_e_nao_emenda_palavras():
    from mente_digital.textutils import sem_embeds_de_imagem

    # colado, viraria "Vejaa clorose"
    assert "Vejaa" not in sem_embeds_de_imagem(FALA)


def test_normalizador_do_piper_tira_o_embed():
    from mente_digital.audio import TtsService

    assert "webp" not in TtsService()._normalizar(FALA)


def test_texto_sem_embed_passa_intacto():
    from mente_digital.textutils import sem_embeds_de_imagem

    assert sem_embeds_de_imagem("resposta comum, sem figura") == "resposta comum, sem figura"


# --- o SERVIDOR anexa a imagem (o LLM não dá conta) -------------------------
# Medido no teste real: a pergunta caiu no nível `curto` (teto de 90 tokens), a
# resposta consumiu o teto inteiro e o embed de ~105 chars não teve onde caber.
# Nenhuma imagem apareceu. Pedir ao modelo era o plano errado.

def test_embed_da_nota_converte_o_caminho():
    from mente_digital.figuras_recorte import embed_da_nota

    assert embed_da_nota("/v/Figuras/livro/x_p0288_f1.md") == \
        "![[Figuras/livro/x_p0288_f1.webp]]"
    assert embed_da_nota(r"D:\v\Figuras\livro\x_p0288_f1.md") == \
        "![[Figuras/livro/x_p0288_f1.webp]]"


def test_embed_da_nota_ignora_o_que_nao_e_figura():
    from mente_digital.figuras_recorte import embed_da_nota

    assert embed_da_nota("/v/Notas/comum.md") is None
    assert embed_da_nota("/v/Notas/Figuras_do_livro.md") is None   # nome, não pasta
    assert embed_da_nota("") is None


def test_bloco_dedup_preserva_a_ordem_da_busca():
    from mente_digital.figuras_recorte import bloco_de_figuras

    bloco = bloco_de_figuras([
        "/v/Figuras/l/b_p2_f1.md",      # a busca entrega por distância: esta é a melhor
        "/v/Notas/texto.md",            # não é figura: fora
        "/v/Figuras/l/a_p1_f1.md",
        "/v/Figuras/l/b_p2_f1.md",      # repetida
    ])
    assert bloco.split("\n") == ["![[Figuras/l/b_p2_f1.webp]]", "![[Figuras/l/a_p1_f1.webp]]"]


def test_bloco_vazio_sem_figura():
    from mente_digital.figuras_recorte import bloco_de_figuras

    assert bloco_de_figuras(["/v/Notas/a.md", "/v/Notas/b.md"]) == ""
    assert bloco_de_figuras([]) == ""


# --- corte relativo à MELHOR figura ------------------------------------------
# O limiar absoluto não sabe desistir: num acervo de 1.735 imagens sempre há alguém
# abaixo dele. Medido em 2026-07-26, já com as legendas traduzidas: na pergunta de
# magnésio as certas ficam em 1,00-1,01x da melhor e o manganês em 1,12x.

async def test_corte_relativo_derruba_a_menos_ruim(monkeypatch):
    monkeypatch.setattr(settings, "figuras_margem_melhor", 1.10)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_fig("mg1", "deficiência de magnésio"), 0.1077),
        (_fig("mg2", "fase avançada de magnésio"), 0.1088),   # 1.01x
        (_fig("mn", "deficiência de manganês"), 0.1201),      # 1.12x -> fora
        (_fig("fe", "deficiência de ferro"), 0.1319),         # 1.22x -> fora
    ])
    aprovadas = await vs._buscar_figuras("magnésio", set())
    assert [round(s, 4) for s, _ in aprovadas] == [0.1077, 0.1088]


async def test_corte_relativo_mantem_o_bloco_coeso(monkeypatch):
    # "podar": as 6 certas ficam dentro de 1,04x — nenhuma pode cair.
    monkeypatch.setattr(settings, "figuras_margem_melhor", 1.10)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_fig(f"p{i}", f"poda {i}"), d)
        for i, d in enumerate([0.1465, 0.1476, 0.1482, 0.1508, 0.1510, 0.1525])
    ])
    assert len(await vs._buscar_figuras("podar", set())) == 6


async def test_corte_relativo_desligado_por_zero(monkeypatch):
    monkeypatch.setattr(settings, "figuras_margem_melhor", 0.0)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_fig("a", "x"), 0.10),
        (_fig("b", "y"), 0.15),      # 1.5x: só sobrevive com o corte desligado
    ])
    assert len(await vs._buscar_figuras("q", set())) == 2


async def test_corte_relativo_nao_quebra_com_lista_vazia(monkeypatch):
    monkeypatch.setattr(settings, "figuras_margem_melhor", 1.10)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([])
    assert await vs._buscar_figuras("q", set()) == []
