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


def _casa(metadata: dict, where: dict) -> bool:
    """O pedaço da linguagem de filtro do Chroma que a produção usa: `$and`
    (lista de condições), `$in` (valor dentro de uma lista) e igualdade simples.

    Ponto ÚNICO de propósito, e não é preciosismo: enquanto o `get` entendia
    `$and`/`$in` e o `similarity_search_with_score` comparava o dict de forma
    plana, o mesmo `where` casava num método e devolvia vazio no outro. O
    resultado é que o anexo por co-locação sumia — e os testes que afirmam
    `anexos == []` ficavam VERDES à toa, provando nada. Uma semântica só.
    """
    md = metadata or {}
    for chave, valor in (where or {}).items():
        if chave == "$and":
            if not all(_casa(md, cond) for cond in valor):
                return False
        elif isinstance(valor, dict) and "$in" in valor:
            if md.get(chave) not in valor["$in"]:
                return False
        elif md.get(chave) != valor:
            return False
    return True


class StoreComFiltro:
    """Chroma que filtra por metadado ANTES de cortar em k, e sabe responder aos
    `$and`/`$in` que o anexo por co-locação usa — nos DOIS métodos."""

    def __init__(self, resultados):
        self._res = resultados
        self.filtros = []

    def similarity_search_with_score(self, consulta, k=4, filter=None):  # noqa: A002
        self.filtros.append(filter)
        itens = [(d, s) for d, s in self._res if _casa(d.metadata, filter or {})]
        # Por distância crescente, como o Chroma real — o ranking das figuras da
        # página depende disso: é ele que decide qual das 13 fotos ilustra melhor.
        itens.sort(key=lambda ds: ds[1])
        return itens[:k]

    def get(self, where=None, include=None):
        return {"metadatas": [d.metadata for d, _ in self._res
                              if _casa(d.metadata, where or {})]}


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


# --- a figura COM legenda também volta por aqui (2026-07-31) ----------------
async def test_anexo_traz_figura_COM_legenda_da_pagina(monkeypatch):
    """O outro lado do caso "tricomas": das 41 figuras de tricoma do acervo, UMA
    diz "tricoma" em português — as outras 40 dizem "glândulas resiníferas", que
    é como o autor chama a estrutura. Nenhum embedding parte da palavra que o
    dono digitou e chega nelas, e tabela de sinônimo não escala para um livro
    inteiro. A página que respondeu já garante o ASSUNTO, então a figura dela
    entra sem repetir a palavra — mesmo tendo legenda de verdade (sem
    `attach_only`), que antes a deixava de fora deste caminho.

    A figura aqui está a 1,20 e sem palavra em comum: ela é REPROVADA na busca
    (`_buscar_figuras`) de propósito, para que só a co-locação possa entregá-la."""
    monkeypatch.setattr(settings, "figuras_anexo_so_attach_only", False)
    monkeypatch.setattr(settings, "figuras_anexo_max", 2)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "os tricomas amadurecem no fim da floração", origem=ORIGEM), 0.10),
        (_fig("l_p0003_f1", "Glândulas resiníferas maduras", origem=ORIGEM), 1.20),
    ])
    res = await vs.search("tricomas")
    assert res.anexos == ["/vault/Figuras/livro/l_p0003_f1.md"]
    # e segue valendo o contrato do anexo: ilustra, não responde
    assert res.anexos[0] not in res.fontes
    assert "Glândulas resiníferas maduras" not in res.texto


async def test_entre_duas_figuras_da_pagina_vence_a_MAIS_PROXIMA(monkeypatch):
    """Uma página do livro tem até 13 fotos de assuntos diferentes e o teto deixa
    passar poucas. Escolher por ordem de NOME entregava a f1 sempre — foi o
    "mesmo rótulo de f1 a f13" que o dono viu ao vivo. Aqui a f1 é a pior e a f9
    é a que ilustra a pergunta: quem tem de sair é a f1."""
    monkeypatch.setattr(settings, "figuras_anexo_max", 1)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "os tricomas amadurecem no fim da floração", origem=ORIGEM), 0.10),
        (_fig("l_p0003_f1", "Uma tempestade deitou esse jardim", origem=ORIGEM), 1.20),
        (_fig("l_p0003_f9", "Glândulas resiníferas maduras", origem=ORIGEM), 1.10),
    ])
    res = await vs.search("tricomas")
    assert res.anexos == ["/vault/Figuras/livro/l_p0003_f9.md"]


async def test_corte_relativo_descarta_a_segunda_figura_muito_pior(monkeypatch):
    """Medido em 2026-07-31: "o que são tricomas?" trazia a foto certa e, atrás
    dela, "uma tempestade de chuva forte deitou esse jardim" — o teto de 2
    preenchia o segundo slot com a próxima da fila mesmo quando ela era muito
    pior. O corte é relativo à MELHOR da própria página (o mesmo remédio do
    `figuras_margem_melhor`), então o par BOM continua passando: é o que o teste
    de cima já mostra, com as duas a 1,10 e 1,20 dentro da margem."""
    monkeypatch.setattr(settings, "figuras_anexo_max", 2)
    monkeypatch.setattr(settings, "figuras_margem_melhor", 1.10)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "os tricomas amadurecem no fim da floração", origem=ORIGEM), 0.10),
        (_fig("l_p0003_f9", "Glândulas resiníferas maduras", origem=ORIGEM), 1.00),
        (_fig("l_p0003_f13", "Uma tempestade deitou esse jardim", origem=ORIGEM), 1.40),
    ])
    # cabiam DUAS; a segunda está a 1,40x da melhor e o corte a descarta
    assert (await vs.search("tricomas")).anexos == ["/vault/Figuras/livro/l_p0003_f9.md"]


async def test_ancora_pula_origem_que_nao_e_pagina_de_livro(monkeypatch):
    """O caso real que originou a mudança (2026-07-31, "o que são tricomas?"): o
    MELHOR átomo era um auto-colhido, cuja `origem` é o BALDE de onde ele veio
    ('TemaQuente') e não uma página. A âncora parava nele, e as 49 figuras das
    páginas 53-54 — que de fato responderam — ficavam todas de fora.

    Com `figuras_anexo_paginas=1` a prova fica exata: há vaga para UMA origem só,
    e ela tem de ser a página de livro que vem ATRÁS do balde na fila."""
    monkeypatch.setattr(settings, "figuras_anexo_paginas", 1)
    monkeypatch.setattr(settings, "figuras_anexo_max", 1)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        # o melhor átomo (0,05) é o balde: sem página, não pode ancorar nada
        (_txt("quente", "tricomas, em geral", origem="TemaQuente"), 0.05),
        (_txt("pagina", "os tricomas amadurecem no fim da floração", origem=ORIGEM), 0.10),
        (_fig("l_p0003_f1", "Glândulas resiníferas maduras", origem=ORIGEM), 1.20),
    ])
    assert (await vs.search("tricomas")).anexos == ["/vault/Figuras/livro/l_p0003_f1.md"]


async def test_botao_so_attach_only_volta_ao_comportamento_antigo(monkeypatch):
    """O botão de retorno. Ligado, a figura COM legenda volta a ficar de fora do
    anexo (ela tem a busca para chegar) e só a attach-only — que não tem outra
    porta — acompanha a página."""
    monkeypatch.setattr(settings, "figuras_anexo_so_attach_only", True)
    monkeypatch.setattr(settings, "figuras_anexo_max", 2)
    vs = rag.VectorStore(embeddings=None)
    vs._store = StoreComFiltro([
        (_txt("nota", "os tricomas amadurecem no fim da floração", origem=ORIGEM), 0.10),
        (_fig("l_p0003_f1", "Glândulas resiníferas maduras", origem=ORIGEM), 1.20),
        (_fig("l_p0003_f2", "Figura da página 3", attach=True, origem=ORIGEM), 1.30),
    ])
    assert (await vs.search("tricomas")).anexos == ["/vault/Figuras/livro/l_p0003_f2.md"]


async def test_anexo_e_fail_soft_com_store_antigo():
    """Store que não entende o `where` do anexo: some o anexo, nunca a resposta.

    A nota PRECISA ter `origem` de página. Sem ela a âncora desiste antes de
    tocar no store e o teste fica verde sem provar fail-soft nenhum — foi
    exatamente o que aconteceu quando a co-locação passou a rankear as figuras
    com `$and` em vez de chamar `get`."""
    class Antigo:
        def similarity_search_with_score(self, consulta, k=4, filter=None):  # noqa: A002
            if filter and "$and" in filter:
                raise ValueError("operador desconhecido")  # o que o Chroma faz
            if filter and filter.get("tipo") == "figura":
                return []
            return [(_txt("nota", "o solo vivo alimenta a planta", origem=ORIGEM), 0.10)]

    vs = rag.VectorStore(embeddings=None)
    vs._store = Antigo()
    res = await vs.search("solo vivo")
    assert res.relevante and res.anexos == []
