"""Multiusuário no RAG: a fronteira é a COLEÇÃO, e o teste central é a AUSÊNCIA.

O que estes testes protegem não é uma funcionalidade — é uma NÃO-funcionalidade: a
nota pessoal de A não pode aparecer para B. Falha desse tipo não quebra nada, não
levanta exceção e não aparece no log: ela responde bonito, aterrado e com a memória
de outra pessoa. Por isso quase todo teste aqui afirma o que NÃO está no resultado.

Por que coleção separada e não `dono` no metadado + `where` (a decisão, com prova):
  (1) `_buscar_texto` é fail-open DE PROPÓSITO — filtro que erra ou vem vazio refaz a
      busca SEM filtro. Para o `tipo` isso é sobrevivência (base em meta_v<4); para o
      dono seria o vault de todo mundo. Está testado abaixo.
  (2) chave AUSENTE não casa `where` em sentido nenhum, e no índice real 5 chunks não
      têm `origem` (Inbox_Captura, Lista_compras, notas salvas à mão). O único metadado
      presente em 100% deles é o CAMINHO — que ninguém esquece de gravar, porque
      escrever o arquivo obriga a escolher a pasta.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mente_digital import identidade, rag, textutils
from mente_digital.config import settings as _settings_real

from conftest import FakeDoc, dist_confiante, dist_longe


# ==========================================================================
# O `settings` do multiusuário, sem MUTAR o objeto real
# ==========================================================================
class _Config:
    """`settings` com o contrato do multiusuário garantido por cima.

    Proxy, e não `monkeypatch.setattr(settings, ...)`, por duas razões: enquanto o
    `config.py` não traz os campos não há o que trocar (`caminho_acervo` não existe
    para ser sobrescrito), e quando trouxer eles serão property/método de CLASSE —
    sobrescrever isso numa instância pydantic é terreno movediço. Tudo o que não é do
    multiusuário é delegado ao `settings` de verdade, então os limiares do gate
    continuam sendo os reais (é o que `dist_confiante`/`dist_longe` pressupõem)."""

    def __init__(self, vault, ligado: bool = True) -> None:
        self._base = _settings_real
        self.multiusuario_habilitado = ligado
        self.caminho_obsidian = str(vault)
        self.subpasta_acervo = "Acervo"
        self.subpasta_pessoal = "Pessoal"
        self.subpasta_figuras = "Figuras"
        self.colecao_acervo = "acervo"

    @property
    def caminho_acervo(self) -> Path:
        return Path(self.caminho_obsidian) / self.subpasta_acervo

    @property
    def dir_figuras(self) -> Path:
        """PRECISA estar aqui, e não cair no `__getattr__`. O `dir_figuras` do settings
        real é property sobre o `caminho_obsidian` REAL — delegado, ele apontaria para o
        vault de produção e o teste indexaria a máquina do dono em vez do `tmp_path`."""
        return Path(self.caminho_obsidian) / self.subpasta_figuras

    def caminho_pessoal(self, dono: str) -> Path:
        return Path(self.caminho_obsidian) / self.subpasta_pessoal / dono

    def colecao_pessoal(self, dono: str) -> str:
        return f"pessoal_{dono}"

    def __getattr__(self, nome):        # só cai aqui o que não é do multiusuário
        return getattr(self._base, nome)


def _fingerprint_atual() -> dict:
    """O carimbo que `_construir_store` põe na criação — igual para TODAS as coleções.
    Elas dividem a mesma `persist_directory` e o mesmo embedding, e é isso que torna as
    distâncias de coleções diferentes comparáveis no fan-in."""
    return {
        "hnsw:space": "cosine",
        "emb_model": _settings_real.embedding_model,
        "emb_query_prefix": _settings_real.embedding_query_prefix,
        "emb_passage_prefix": _settings_real.embedding_passage_prefix,
    }


# ==========================================================================
# Coleção falsa (a semântica do Chroma que o rag.py de fato usa)
# ==========================================================================
def _casa(metadata: dict, where: dict) -> bool:
    """O pedaço da linguagem de filtro do Chroma que a produção usa: `$and`, `$in` e
    igualdade. Um lugar só — o `get` e o `similarity_search_with_score` precisam
    concordar, senão um teste fica verde afirmando lista vazia pelo motivo errado."""
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


class _Col:
    """O `_collection` semi-privado: carimbo do fingerprint + o `get` das figuras."""

    def __init__(self, dono_da_colecao: "_Colecao") -> None:
        self.metadata = _fingerprint_atual()
        self._colecao = dono_da_colecao

    def get(self, where=None, include=None):
        return self._colecao.get(where=where, include=include)

    def modify(self, metadata):
        self.metadata = metadata


class _Colecao:
    """Uma coleção do Chroma, falsa mas com a semântica que o `rag.py` usa.

    Registra o que foi perguntado A ELA (`consultas`) porque metade dos testes daqui
    afirma que uma coleção NÃO foi consultada — e "não veio no resultado" é prova mais
    fraca que "nem foi perguntado"."""

    def __init__(self, nome: str, itens=()) -> None:
        self.nome = nome
        self.itens = [(doc, float(score)) for doc, score in itens]
        self.consultas: list = []
        self.adicionados: list = []
        self.deletados: list = []
        self._collection = _Col(self)

    # --- leitura ---------------------------------------------------------
    def similarity_search_with_score(self, consulta, k=4, filter=None):  # noqa: A002
        self.consultas.append(consulta)
        res = [(d, s) for d, s in self.itens if _casa(d.metadata, filter or {})]
        res.sort(key=lambda ds: ds[1])
        return res[:k]

    def get(self, where=None, include=None, limit=None, offset=0):
        docs = [d for d, _ in self.itens if _casa(d.metadata, where or {})]
        docs = docs[offset:(offset + limit) if limit else None]
        saida = {
            "documents": [d.page_content for d in docs],
            "metadatas": [dict(d.metadata) for d in docs],
            "ids": [str(i) for i, _ in enumerate(docs)],
        }
        if include and "embeddings" in include:
            saida["embeddings"] = [d.metadata.get("_emb", [1.0, 0.0]) for d in docs]
        return saida

    # --- escrita ---------------------------------------------------------
    def add_documents(self, docs) -> None:
        self.adicionados.extend(docs)
        self.itens.extend((FakeDoc(d.page_content, dict(d.metadata)), 0.5) for d in docs)

    def delete(self, where=None) -> None:
        self.deletados.append(where)
        self.itens = [(d, s) for d, s in self.itens if not _casa(d.metadata, where or {})]


def _doc(texto: str, fonte: str, **extra) -> FakeDoc:
    meta = {"source": fonte, "tipo": "texto", "confidence": 1.0}
    meta.update(extra)
    return FakeDoc(texto, meta)


def _montar(monkeypatch, vault, acervo=(), pessoais=None, ligado=True):
    """VectorStore falando com coleções falsas. Devolve (vs, {nome: _Colecao})."""
    cfg = _Config(vault, ligado=ligado)
    monkeypatch.setattr(rag, "settings", cfg)

    lojas = {cfg.colecao_acervo: _Colecao(cfg.colecao_acervo, acervo)}
    for dono, itens in (pessoais or {}).items():
        nome = cfg.colecao_pessoal(dono)
        lojas[nome] = _Colecao(nome, itens)

    vs = rag.VectorStore(embeddings=None)

    def _construir(colecao=None):
        # `colecao=None` é o modo de um usuário só: no Chroma real isso é a coleção
        # 'langchain' — aqui, a mesma loja do acervo, que é o índice que já existe.
        return lojas.get(colecao or cfg.colecao_acervo)

    monkeypatch.setattr(vs, "_construir_store", _construir)
    vs._store = lojas[cfg.colecao_acervo]
    return vs, lojas


# ==========================================================================
# As peças puras
# ==========================================================================
def test_fundir_uma_parte_devolve_a_lista_intacta():
    """O modo de um usuário só não pode nem reordenar: o contrato dele é ser
    indistinguível do código anterior, e vários testes deste projeto dependem da
    ordem em que a loja devolveu."""
    parte = [("b", 0.9), ("a", 0.1)]
    assert rag.fundir_por_distancia([parte], k=1) == parte


def test_fundir_ordena_por_distancia_e_corta_em_k():
    acervo = [("acervo-perto", 0.10), ("acervo-longe", 0.90)]
    pessoal = [("pessoal-pertissimo", 0.05), ("pessoal-medio", 0.50)]
    assert [t for t, _ in rag.fundir_por_distancia([acervo, pessoal], k=3)] == [
        "pessoal-pertissimo", "acervo-perto", "pessoal-medio"]


def test_fundir_nao_da_cota_por_colecao():
    """O k é global, não k/N: um acervo que domina o ranking continua dominando —
    `rag_score_confident` é limiar de DISTÂNCIA, não de posição, e uma cota por
    coleção mudaria o que entra no contexto sem mudar número nenhum de config."""
    acervo = [(f"a{i}", 0.01 * i) for i in range(5)]
    pessoal = [("p", 0.99)]
    assert [t for t, _ in rag.fundir_por_distancia([acervo, pessoal], k=5)] == [
        "a0", "a1", "a2", "a3", "a4"]


def test_repartir_manda_cada_nota_para_a_raiz_mais_especifica(tmp_path):
    vault = tmp_path
    acervo, ana = vault / "Acervo", vault / "Pessoal" / "ana"
    por_raiz, sobras = rag.repartir_por_raiz(
        [str(acervo / "livro.md"), str(ana / "diario.md"), str(vault / "solta.md")],
        [str(acervo), str(ana)],
    )
    assert por_raiz[str(acervo)] == [str(acervo / "livro.md")]
    assert por_raiz[str(ana)] == [str(ana / "diario.md")]
    # A nota fora de todo escopo é o achado, não um detalhe: ela não pertence a
    # coleção nenhuma e some da busca. Voltar como "sobra" é o que permite avisar.
    assert sobras == [str(vault / "solta.md")]


def test_repartir_com_uma_raiz_so_nao_sobra_nada(tmp_path):
    """O modo de um usuário só, por construção: raiz = o vault inteiro."""
    arquivos = [str(tmp_path / "a.md"), str(tmp_path / "sub" / "b.md")]
    por_raiz, sobras = rag.repartir_por_raiz(arquivos, [str(tmp_path)])
    assert por_raiz[str(tmp_path)] == arquivos and sobras == []


def test_o_contrato_com_o_config_e_o_que_o_rag_de_fato_usa():
    """A COSTURA entre dois arquivos: o `config.py` define os campos, o `rag.py` os
    consome. Um rename de um lado só quebraria o multiusuário — e no caso do bool
    quebraria CALADO (`extra="ignore"` do pydantic engole o que não casa). O resto da
    suíte roda sobre um proxy de settings, então este é o único teste que olha o objeto
    de verdade."""
    vault = Path(_settings_real.caminho_obsidian)
    # ⚠ "NASCE desligado" se prova na CLASSE, não na instância (2026-08-04). A linha
    # anterior era `_settings_real.multiusuario_habilitado is False`, e ela media a
    # máquina de quem roda: no dia em que o dono LIGOU a flag no `.env`, este teste
    # passou a falhar sem que uma linha de código mudasse. E hoje seria pior — o
    # `_isola_do_env` do conftest devolve o campo ao default por teste, então a
    # asserção na instância passaria lendo o valor que o próprio fixture escreveu,
    # provando o fixture em vez do contrato. O default do campo é o que se quer aqui.
    assert type(_settings_real).model_fields[
        "multiusuario_habilitado"].default is False
    assert _settings_real.caminho_acervo == vault / _settings_real.subpasta_acervo
    assert _settings_real.caminho_pessoal("ana") == (
        vault / _settings_real.subpasta_pessoal / "ana")
    assert _settings_real.colecao_pessoal("ana") != _settings_real.colecao_acervo
    assert _settings_real.colecao_pessoal("ana") != _settings_real.colecao_pessoal("bob")
    assert rag.multiusuario_ligado() is False


def test_a_suite_nao_herda_a_flag_do_env_do_dono():
    """O defeito de 2026-08-04, virado teste.

    O dono ligou `MENTE_MULTIUSUARIO_HABILITADO=true` no `.env` — que é ignorado pelo
    git — e o `pytest` na máquina dele passou a dar **134 falhas** enquanto o CI
    seguia verde, porque o CI roda com o default. Suíte que muda de resultado
    conforme a máquina não é suíte: ela deixa de arbitrar qualquer coisa.

    O `_isola_do_env` do conftest devolve o campo ao default por teste. Aqui a gente
    afirma isso de fora, para que remover aquela linha do fixture quebre ALGUMA
    coisa — é o mesmo espírito do `pesquisa_agendada_intervalo_seconds`, que entrou
    naquela lista pelo mesmo motivo e sem teste nenhum guardando a decisão."""
    assert _settings_real.multiusuario_habilitado is False
    assert rag.multiusuario_ligado() is False


def test_config_sem_o_campo_avisa_alto_e_segue_desligado(monkeypatch):
    """A war story do `..._SEGUNDOS`: campo que não casa é engolido pelo pydantic e o
    dono passa meses achando que ligou algo. Aqui a ausência GRITA — uma vez."""
    monkeypatch.setattr(rag, "settings", object())
    monkeypatch.setattr(rag, "_avisou_config_incompleta", False)
    avisos: list = []
    monkeypatch.setattr(rag.telemetry, "warn", lambda mod, msg: avisos.append(msg))
    assert rag.multiusuario_ligado() is False
    assert rag.multiusuario_ligado() is False        # não vira spam
    assert len(avisos) == 1 and "multiusuario_habilitado" in avisos[0]


# ==========================================================================
# O teste central: o que NÃO se vê
# ==========================================================================
@pytest.fixture
def dois_donos(monkeypatch, tmp_path):
    """Acervo comum + um vault pessoal para cada dono, todos falando do MESMO assunto
    (é o caso difícil: se o isolamento dependesse de os assuntos não se parecerem, ele
    não seria isolamento)."""
    return _montar(
        monkeypatch, tmp_path,
        acervo=[(_doc("O gato dorme 16 horas por dia.", "/v/Acervo/gato.md"),
                 dist_confiante())],
        pessoais={
            "ana": [(_doc("A senha do meu cofre é 4231.", "/v/Pessoal/ana/cofre.md"),
                     dist_confiante(0.2))],
            "bob": [(_doc("Devo 300 reais ao Bob da padaria.", "/v/Pessoal/bob/dividas.md"),
                     dist_confiante(0.2))],
        },
    )


async def test_o_acervo_e_visto_pelos_dois_donos(dois_donos):
    vs, _lojas = dois_donos
    for dono in ("ana", "bob"):
        with identidade.usar_dono(dono):
            res = await vs.search("gato dorme")
        assert "O gato dorme 16 horas por dia." in res.texto, dono
        assert "/v/Acervo/gato.md" in res.fontes, dono


async def test_a_nota_pessoal_de_um_e_invisivel_para_o_outro(dois_donos):
    """O TESTE. Nem no texto do contexto, nem em `fontes` (que alimenta a promoção do
    #conhecimento_novo — vazar ali marcaria a nota de A como reusada por B)."""
    vs, lojas = dois_donos
    with identidade.usar_dono("bob"):
        do_bob = await vs.search("senha cofre")
    # Mais forte que "não veio no resultado": a coleção da ana NEM FOI PERGUNTADA no
    # turno do bob. Um vazamento por ordenação/dedup não teria como acontecer.
    assert lojas["pessoal_ana"].consultas == []

    with identidade.usar_dono("ana"):
        da_ana = await vs.search("senha cofre")

    assert "4231" in da_ana.texto
    assert "4231" not in do_bob.texto
    assert not any("ana" in f for f in do_bob.fontes)
    assert lojas["pessoal_ana"].consultas          # a dela, sim, foi consultada


async def test_sem_dono_no_contexto_so_o_acervo_responde(dois_donos):
    """Trabalho de fundo, boot, um turno que esqueceu de marcar o dono. A ausência de
    dono NEGA o dado pessoal — nunca herda o do último que passou por aqui."""
    vs, lojas = dois_donos
    with identidade.usar_dono("ana"):
        await vs.search("senha cofre")          # deixa a coleção da ana ABERTA no cache
    antes = len(lojas["pessoal_ana"].consultas)
    res = await vs.search("senha cofre")        # ...e agora ninguém está logado
    assert "4231" not in res.texto
    assert len(lojas["pessoal_ana"].consultas) == antes        # nem foi reconsultada


async def test_o_fan_in_ordena_por_distancia(monkeypatch, tmp_path):
    """Acervo e pessoal disputam as mesmas vagas numa lista só, ordenada por
    distância — não "primeiro o acervo, depois o resto"."""
    vs, _ = _montar(
        monkeypatch, tmp_path,
        acervo=[(_doc("terceiro", "/v/Acervo/c.md"), dist_confiante(0.9)),
                (_doc("primeiro", "/v/Acervo/a.md"), dist_confiante(0.1))],
        pessoais={"ana": [(_doc("segundo", "/v/Pessoal/ana/b.md"), dist_confiante(0.5))]},
    )
    with identidade.usar_dono("ana"):
        res = await vs._buscar_texto("qualquer coisa", k=10)
    assert [d.page_content for d, _s in res] == ["primeiro", "segundo", "terceiro"]


async def test_buscar_conteudos_tambem_respeita_o_escopo(dois_donos):
    """A Síntese sob Demanda ("o que eu sei sobre X") tem caminho PRÓPRIO no agent —
    fan-in esquecido ali vazaria por uma porta que o `search` não guarda."""
    vs, _lojas = dois_donos
    with identidade.usar_dono("ana"):
        assert any("4231" in c for c in await vs.buscar_conteudos("cofre", k=10))
    with identidade.usar_dono("bob"):
        assert not any("4231" in c for c in await vs.buscar_conteudos("cofre", k=10))


async def test_coleção_pessoal_que_nao_abre_degrada_para_menos_e_nunca_para_a_do_outro(
        monkeypatch, tmp_path):
    """Falha de IO na coleção de A não pode virar "então busca sem filtro" — que é
    exatamente o que o fail-open do `_buscar_texto` faria se o dono fosse um `where`."""
    vs, lojas = _montar(
        monkeypatch, tmp_path,
        acervo=[(_doc("do acervo", "/v/Acervo/a.md"), dist_confiante())],
        pessoais={"ana": []},
    )
    erros: list = []
    monkeypatch.setattr(rag.telemetry, "error",
                        lambda mod, msg, exc=None: erros.append(msg))
    monkeypatch.setattr(vs, "_construir_store",
                        lambda colecao=None: (_ for _ in ()).throw(OSError("disco")))
    with identidade.usar_dono("ana"):
        res = await vs.search("qualquer")
    assert "do acervo" in res.texto            # o acervo continua respondendo
    assert erros and "ana" in erros[0]         # e a falha foi dita, não engolida


async def test_abrir_a_colecao_pessoal_acontece_uma_vez_so(dois_donos):
    """Sem cache, cada pergunta pagaria a abertura de uma coleção do Chroma."""
    vs, _lojas = dois_donos
    aberturas: list = []
    construir = vs._construir_store
    vs._construir_store = lambda colecao=None: (aberturas.append(colecao),
                                                construir(colecao))[1]
    with identidade.usar_dono("ana"):
        for _ in range(3):
            await vs.search("gato")
    assert aberturas == ["pessoal_ana"]


# ==========================================================================
# O interruptor: desligado = byte a byte o de hoje
# ==========================================================================
async def test_desligado_e_uma_colecao_so_e_sem_fan_in(monkeypatch, tmp_path):
    vs, lojas = _montar(
        monkeypatch, tmp_path, ligado=False,
        acervo=[(_doc("única coleção", "/v/nota.md"), dist_confiante())],
        pessoais={"ana": [(_doc("nunca lida", "/v/Pessoal/ana/x.md"), dist_confiante(0.1))]},
    )
    with identidade.usar_dono("ana"):
        res = await vs.search("qualquer")
    assert "única coleção" in res.texto and "nunca lida" not in res.texto
    assert lojas["pessoal_ana"].consultas == []
    assert await vs._escopo() == [("", vs._store)]


def test_desligado_nao_passa_nome_de_colecao_ao_chroma(monkeypatch, tmp_path):
    """O nome ausente é o default 'langchain' do langchain-chroma — a coleção que já
    está no disco com 25.671 embeddings. Passar um nome novo reindexaria tudo."""
    vs, _ = _montar(monkeypatch, tmp_path, ligado=False)
    assert vs._colecao_base() is None
    monkeypatch.setattr(rag.settings, "multiusuario_habilitado", True)
    assert vs._colecao_base() == "acervo"


def test_todas_as_colecoes_nascem_com_o_mesmo_carimbo(monkeypatch, tmp_path):
    """Cosseno + fingerprint em TODAS: distância de coleções diferentes só é comparável
    no fan-in se o espaço e o embedding forem os mesmos, e o `_fingerprint_ok` precisa
    continuar valendo para cada uma."""
    monkeypatch.setattr(rag, "settings", _Config(tmp_path))
    capturado: dict = {}

    class _ChromaFalso:
        def __init__(self, **kw):
            capturado.update(kw)

    modulo = type("m", (), {"Chroma": _ChromaFalso})
    monkeypatch.setitem(__import__("sys").modules, "langchain_chroma", modulo)
    vs = rag.VectorStore(embeddings=type("E", (), {"instance": None})())

    vs._construir_store("pessoal_ana")
    assert capturado["collection_name"] == "pessoal_ana"
    assert capturado["collection_metadata"]["hnsw:space"] == "cosine"
    assert capturado["collection_metadata"]["emb_model"] == _settings_real.embedding_model

    capturado.clear()
    vs._construir_store()
    assert "collection_name" not in capturado          # o default de sempre


# ==========================================================================
# Os dois índices em RAM: malha e figuras
# ==========================================================================
def _com_conceitos(texto: str, fonte: str, *conceitos) -> FakeDoc:
    marca = rag._SEP_CONCEITO
    return _doc(texto, fonte,
                conceitos=marca + marca.join(conceitos) + marca)


async def test_a_malha_e_por_escopo_e_deriva_do_acervo(monkeypatch, tmp_path):
    vs, _lojas = _montar(
        monkeypatch, tmp_path,
        acervo=[(_com_conceitos("Adubação foliar em estufa.",
                                "/v/Acervo/adubo.md", "Adubo"), dist_confiante())],
        pessoais={
            "ana": [(_com_conceitos("Meu canteiro rende mais com adubo.",
                                    "/v/Pessoal/ana/canteiro.md", "Adubo", "Canteiro"),
                     dist_confiante())],
            "bob": [],
        },
    )
    await vs._reconstruir_malha()               # a base, como o open() faz
    for dono in ("ana", "bob"):
        with identidade.usar_dono(dono):
            await vs._escopo()          # abre a coleção -> deriva a malha do dono

    def _conferir():
        with identidade.usar_dono("ana"):
            malha_ana = vs.malha
            assert malha_ana.n_atomos == 2                   # acervo + a nota dela
            assert "canteiro" in malha_ana._por_conceito
        with identidade.usar_dono("bob"):
            malha_bob = vs.malha
            assert malha_bob.n_atomos == 1                   # só o acervo
            assert "canteiro" not in malha_bob._por_conceito
        # A base não foi contaminada: é dela que os dois derivam, e um vazamento AQUI
        # apareceria em todo mundo de uma vez.
        assert vs._malha_base.n_atomos == 1
        assert vs._malha_base._por_conceito["adubo"] == {"/v/Acervo/adubo.md"}

    _conferir()
    # Remontar a base invalida todas as derivadas — o `_reconstruir_malha` refaz as
    # dos donos já abertos. Sem isso, cada sync deixaria a malha do dono presa a um
    # retrato velho do acervo, em silêncio (a malha é fail-soft).
    await vs._reconstruir_malha()
    _conferir()


def test_derivada_e_copy_on_write(tmp_path):
    """A malha do dono compartilha os átomos do acervo em vez de copiá-los: com 4
    donos, é a diferença entre 4 cópias de ~25k átomos em RAM e uma só."""
    base = rag.MalhaIndex()
    base.construir(["Texto do acervo."],
                   [{"source": "/v/Acervo/a.md", "conceitos": "|Adubo|"}])
    derivada = base.derivada()
    derivada.estender(["Nota da ana."],
                      [{"source": "/v/Pessoal/ana/n.md", "conceitos": "|Adubo|"}])
    assert derivada._por_conceito["adubo"] == {"/v/Acervo/a.md", "/v/Pessoal/ana/n.md"}
    assert base._por_conceito["adubo"] == {"/v/Acervo/a.md"}    # o set não foi mutado
    assert base._texto_de["/v/Acervo/a.md"] is derivada._texto_de["/v/Acervo/a.md"]


def test_estender_conta_o_idf_do_atomo_uma_vez_so():
    """O df é por ÁTOMO, não por chunk — o `estender` tem de manter isso, senão o
    aterramento por IDF (G3) passa a achar comum a palavra que é rara."""
    m = rag.MalhaIndex()
    m.construir(["Fotossíntese parte 1.", "Fotossíntese parte 2."],
                [{"source": "/v/a.md"}, {"source": "/v/a.md"}])
    m.estender(["Fotossíntese na horta."], [{"source": "/v/Pessoal/ana/b.md"}])
    assert m.n_atomos == 2
    assert m._df_palavra[textutils.normaliza("fotossintese")] == 2


async def test_a_figura_pessoal_nao_aparece_para_o_outro(monkeypatch, tmp_path):
    """A busca de figura tem espaço PRÓPRIO (matriz exata em RAM, `_IndiceFiguras`) —
    é uma segunda porta de recuperação, e ela precisa do mesmo escopo."""
    def _fig(nome, fonte, emb):
        return _doc(nome, fonte, tipo="figura", _emb=emb)

    vs, lojas = _montar(
        monkeypatch, tmp_path,
        acervo=[(_fig("figura do acervo", "/v/Acervo/f1.md", [1.0, 0.0]), 0.0)],
        pessoais={
            "ana": [(_fig("foto da ana", "/v/Pessoal/ana/f2.md", [1.0, 0.0]), 0.0)],
            "bob": [],
        },
    )
    vs._embeddings = type("E", (), {"instance": type("I", (), {
        "embed_query": staticmethod(lambda t: [1.0, 0.0])})()})()

    with identidade.usar_dono("ana"):
        legendas_ana = [d.page_content for d, _s in await vs._figuras_candidatas("x")]
    with identidade.usar_dono("bob"):
        legendas_bob = [d.page_content for d, _s in await vs._figuras_candidatas("x")]

    assert set(legendas_ana) == {"figura do acervo", "foto da ana"}
    assert legendas_bob == ["figura do acervo"]
    # E o cache é por escopo: a matriz do acervo (`_idx_figuras`) é montada UMA vez e
    # não é remontada por dono. O bob entra como None — ele não tem figura, e cachear
    # a ausência evita tentar montar de novo a cada pergunta dele.
    assert vs._idx_figuras is not None and len(vs._idx_figuras) == 1
    assert len(vs._idx_figuras_dono["ana"]) == 1
    assert vs._idx_figuras_dono["bob"] is None


# ==========================================================================
# sync(): cada pasta na sua coleção
# ==========================================================================
def _escrever(caminho: Path, texto: str) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto, encoding="utf-8")


async def test_sync_manda_cada_pasta_para_a_sua_colecao(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    _escrever(vault / "Acervo" / "livro.md", "# Livro\nCapítulo sobre solo.")
    _escrever(vault / "Pessoal" / "ana" / "diario.md", "# Diário\nHoje plantei alface.")
    _escrever(vault / "Pessoal" / "bob" / "notas.md", "# Notas\nComprar parafusos.")
    vs, lojas = _montar(monkeypatch, vault, pessoais={"ana": [], "bob": []})

    assert await vs.sync() is True

    def _fontes(nome):
        return {os.path.basename(d.metadata["source"])
                for d in lojas[nome].adicionados}

    assert _fontes("acervo") == {"livro.md"}
    assert _fontes("pessoal_ana") == {"diario.md"}
    assert _fontes("pessoal_bob") == {"notas.md"}


async def test_sync_indexa_as_figuras_da_raiz_no_acervo(monkeypatch, tmp_path):
    """`Figuras/` fica na RAIZ do vault, não dentro de `Acervo/` — e mesmo assim é
    escopo do acervo.

    Medido em 2026-08-04, no vault real, ANTES de ligar a flag: com os escopos
    cobrindo só `Acervo/` e `Pessoal/<dono>/`, as **1.817** notas de figura caíam
    todas em `sobras` — avisadas e NÃO indexadas. Como `montar_indice_figuras` lê do
    store, a matriz exata nasceria vazia e o fallback ANN também (não sobraria doc
    `tipo=figura` em coleção nenhuma): ligar o multiusuário APAGARIA a busca de
    figuras inteira.

    A pasta não pode simplesmente mudar de lugar: 3.663 embeds a referenciam por
    CAMINHO (`![[Figuras/...]]`) e o `/api/imagem` resolve com `is_file()` — movê-la
    daria 404 silencioso em todas elas. Logo, é o ESCOPO que vai até ela.

    O teste anterior a este usava figura em `/v/Acervo/f1.md`, que é o layout que o
    vault real não tem: foi por essa costura que o defeito passou pela suíte verde."""
    vault = tmp_path / "vault"
    _escrever(vault / "Acervo" / "livro.md", "# Livro\nCapítulo sobre solo.")
    _escrever(vault / "Figuras" / "obra" / "p0001_f1.md", "# Figura\nTricomas ao micro.")
    _escrever(vault / "Pessoal" / "ana" / "diario.md", "# Diário\nHoje plantei alface.")
    vs, lojas = _montar(monkeypatch, vault, pessoais={"ana": []})
    avisos: list = []
    monkeypatch.setattr(rag.telemetry, "warn", lambda mod, msg: avisos.append(msg))

    assert await vs.sync() is True

    def _fontes(nome):
        return {os.path.basename(d.metadata["source"]) for d in lojas[nome].adicionados}

    assert _fontes("acervo") == {"livro.md", "p0001_f1.md"}
    assert _fontes("pessoal_ana") == {"diario.md"}
    # E nada de figura foi dado como perdido — a régua é "não sobrou", não "entrou".
    assert not [a for a in avisos if "invisíveis" in a], avisos


async def test_acervo_e_figuras_dividem_o_store_sem_se_purgarem(monkeypatch, tmp_path):
    """Duas raízes no MESMO store não podem se apagar uma à outra.

    A purga de órfãos do `_sync_escopo` remove todo chunk cujo `source` não vive sob a
    raiz daquele escopo. Se `Acervo/` e `Figuras/` entrassem como dois ESCOPOS
    separados apontando para a mesma coleção, cada passada declararia a outra órfã e a
    deletaria — o índice terminaria com o que fosse indexado por último. Por isso as
    duas viajam como raízes de um escopo SÓ.

    A segunda passada é o que dá o veredicto: na primeira o store está vazio e a purga
    não tem o que apagar."""
    vault = tmp_path / "vault"
    _escrever(vault / "Acervo" / "livro.md", "# Livro\nTexto.")
    _escrever(vault / "Figuras" / "obra" / "f1.md", "# Figura\nLegenda.")
    vs, lojas = _montar(monkeypatch, vault)

    await vs.sync()
    await vs.sync()      # aqui a purga enxerga o que a 1ª passada gravou

    vivos = {os.path.basename(str(d.metadata["source"])) for d, _s in lojas["acervo"].itens}
    assert vivos == {"livro.md", "f1.md"}, f"purga mútua comeu um dos dois: {vivos}"
    assert lojas["acervo"].deletados == [], lojas["acervo"].deletados


async def test_sync_avisa_da_nota_que_ficou_fora_de_todo_escopo(monkeypatch, tmp_path):
    """Nota fora de `Acervo/` e de `Pessoal/<dono>/` não pertence a coleção nenhuma —
    logo, é invisível para a busca. Encolher a base em silêncio é como este projeto já
    perdeu figura (attach_only) e nota (acervo_suspeito) antes."""
    vault = tmp_path / "vault"
    _escrever(vault / "Acervo" / "livro.md", "# Livro\nTexto.")
    _escrever(vault / "solta.md", "# Solta\nNota da migração que ninguém moveu.")
    vs, lojas = _montar(monkeypatch, vault)
    avisos: list = []
    monkeypatch.setattr(rag.telemetry, "warn", lambda mod, msg: avisos.append(msg))

    await vs.sync()

    assert any("solta.md" in a and "invisíveis" in a for a in avisos), avisos
    assert all("solta" not in d.metadata["source"] for d in lojas["acervo"].adicionados)


async def test_sync_incremental_por_mtime_vale_por_escopo(monkeypatch, tmp_path):
    """O incremental não pode se perder no fan-in: rodar duas vezes não reindexa nada,
    e é isso que impede o boot de re-embedar o vault inteiro a cada partida."""
    vault = tmp_path / "vault"
    _escrever(vault / "Acervo" / "livro.md", "# Livro\nTexto.")
    _escrever(vault / "Pessoal" / "ana" / "n.md", "# N\nTexto da ana.")
    vs, lojas = _montar(monkeypatch, vault, pessoais={"ana": []})

    assert await vs.sync() is True
    quanto = {n: len(c.adicionados) for n, c in lojas.items()}
    assert await vs.sync() is False
    assert {n: len(c.adicionados) for n, c in lojas.items()} == quanto


async def test_sync_ignora_pasta_que_nao_e_nome_de_dono(monkeypatch, tmp_path):
    """`identidade.normalizar` se recusa a adivinhar, e adivinhar aqui seria o servidor
    escolhendo em que coleção a memória de alguém cai."""
    vault = tmp_path / "vault"
    _escrever(vault / "Pessoal" / "Backup Antigo" / "n.md", "# N\nTexto.")
    vs, lojas = _montar(monkeypatch, vault)
    avisos: list = []
    monkeypatch.setattr(rag.telemetry, "warn", lambda mod, msg: avisos.append(msg))

    await vs.sync()

    assert any("Backup Antigo" in a for a in avisos), avisos
    assert list(lojas) == ["acervo"]        # nenhuma coleção foi criada para ela


async def test_a_nota_que_troca_de_dono_sai_da_colecao_antiga(monkeypatch, tmp_path):
    """A purga de órfãos ganha um segundo ofício de graça: `source` que não vive mais
    sob a raiz DAQUELE escopo some de lá. Sem isso, a nota movida de A para B
    responderia para os dois."""
    vault = tmp_path / "vault"
    _escrever(vault / "Pessoal" / "bob" / "n.md", "# N\nTexto migrado.")
    antiga = str(vault / "Pessoal" / "ana" / "n.md")
    vs, lojas = _montar(
        monkeypatch, vault,
        pessoais={"ana": [(_doc("Texto migrado.", antiga, mtime=1.0), 0.1)], "bob": []},
    )
    (vault / "Pessoal" / "ana").mkdir(parents=True, exist_ok=True)

    await vs.sync()

    assert {"source": antiga} in lojas["pessoal_ana"].deletados
    assert [os.path.basename(d.metadata["source"])
            for d in lojas["pessoal_bob"].adicionados] == ["n.md"]


async def test_desligado_o_sync_varre_o_vault_inteiro_numa_colecao_so(monkeypatch, tmp_path):
    """O contrato do interruptor no caminho de escrita: sem multiusuário, a raiz é o
    vault e não existe pasta privilegiada — inclusive as que se CHAMAM Acervo/Pessoal."""
    vault = tmp_path / "vault"
    _escrever(vault / "raiz.md", "# Raiz\nTexto.")
    _escrever(vault / "Acervo" / "livro.md", "# Livro\nTexto.")
    _escrever(vault / "Pessoal" / "ana" / "n.md", "# N\nTexto.")
    vs, lojas = _montar(monkeypatch, vault, ligado=False)

    await vs.sync()

    assert list(lojas) == ["acervo"]
    assert {os.path.basename(d.metadata["source"])
            for d in lojas["acervo"].adicionados} == {"raiz.md", "livro.md", "n.md"}


# ==========================================================================
# O gate segue sendo o gate
# ==========================================================================
async def test_o_limiar_de_confianca_nao_muda_com_o_fan_in(monkeypatch, tmp_path):
    """A escala é de cosseno e o gate é de DISTÂNCIA: um átomo pessoal longe demais
    continua fora, mesmo sendo o único candidato do dono."""
    vs, _ = _montar(
        monkeypatch, tmp_path,
        acervo=[],
        pessoais={"ana": [(_doc("assunto sem relação nenhuma com a pergunta",
                                "/v/Pessoal/ana/x.md"), dist_longe())]},
    )
    with identidade.usar_dono("ana"):
        res = await vs.search("magnésio na floração")
    assert res.relevante is False and res.texto == rag.NENHUM
