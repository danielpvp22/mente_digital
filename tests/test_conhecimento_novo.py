"""
Ciclo de vida do conhecimento auto-colhido:

- Átomos nascem com a tag #conhecimento_novo (curiosidade não consolidada).
- Quando um átomo é de fato USADO numa resposta local, o pipeline REMOVE a tag
  (promoção) — Agent._consolidar_fontes + textutils.remover_tag.
- A busca local reporta as FONTES dos chunks que entraram no contexto (LocalResult.fontes).
- O histórico de conversa é atomizado (EtlProcessor.summarize_dump) em vez de resumo
  estruturado, e nasce como #conhecimento_novo.

Tudo com fakes (sem GPU/rede/Chroma).
"""
import os

import prompts
import textutils
from agent import Agent, EtlProcessor, dividir_atomos
from config import Settings, settings
from rag import NENHUM, VectorStore
from state import AppContext, SessionMemory

from conftest import FakeDoc, FakeLlama, FakeStore, FakeTts


# --- dividir_atomos (puro: 1 arquivo por átomo) ------------------------------
def test_dividir_atomos_separa_por_cabecalho():
    texto = (
        "## Um\ncorpo um\n#zettelkasten_atomico #conhecimento_novo\n\n"
        "## Dois\ncorpo dois\n#zettelkasten_atomico #conhecimento_novo\n"
    )
    blocos = dividir_atomos(texto)
    assert len(blocos) == 2
    assert blocos[0].startswith("## Um") and blocos[1].startswith("## Dois")


def test_dividir_atomos_ignora_preambulo():
    blocos = dividir_atomos("Aqui vão as notas:\n## Único\ncorpo\n")
    assert len(blocos) == 1
    assert blocos[0].startswith("## Único")   # preâmbulo antes do 1º '##' descartado


def test_dividir_atomos_sem_cabecalho_vazio():
    assert dividir_atomos("texto solto sem heading nenhum") == []


# --- textutils.remover_tag (puro) --------------------------------------------
def test_remover_tag_inline_mantem_a_outra():
    txt = "ideia atômica\n#zettelkasten_atomico #conhecimento_novo\n"
    out = textutils.remover_tag(txt, "#conhecimento_novo")
    assert "#conhecimento_novo" not in out
    assert "#zettelkasten_atomico" in out
    assert "  \n" not in out          # não deixou espaço pendurado antes do \n


def test_remover_tag_nao_pega_prefixo_de_outra_tag():
    txt = "x #conhecimento_novo_extra y"
    # \b garante palavra inteira: não deve remover parte de outra tag
    assert textutils.remover_tag(txt, "#conhecimento_novo") == txt


def test_remover_tag_ausente_e_noop():
    txt = "só #zettelkasten_atomico aqui"
    assert textutils.remover_tag(txt, "#conhecimento_novo") == txt


# --- LocalResult.fontes ------------------------------------------------------
def _store_com(resultados):
    vs = VectorStore(embeddings=None)
    vs._store = FakeStore(resultados)
    return vs


async def test_search_reporta_fontes_dos_usados():
    doc = FakeDoc("TensorRT acelera o YOLO", {"confidence": 0.6, "source": "/vault/Conhecimento_Novo/x.md"})
    vs = _store_com([(doc, 0.3)])
    res = await vs.search("tensorrt yolo")
    assert res.relevante is True
    assert res.fontes == ["/vault/Conhecimento_Novo/x.md"]


async def test_search_sem_fonte_nos_metadados_nao_quebra():
    doc = FakeDoc("algo confiante", {"confidence": 1.0})   # sem 'source'
    vs = _store_com([(doc, 0.3)])
    res = await vs.search("algo")
    assert res.fontes == []


async def test_search_irrelevante_sem_fontes():
    doc = FakeDoc("bolo de cenoura", {"source": "/vault/n.md"})
    vs = _store_com([(doc, 1.2)])                            # nem keyword nem confiante
    res = await vs.search("tensorrt")
    assert res.texto == NENHUM
    assert res.fontes == []


# --- Agent._consolidar_fontes (promoção) -------------------------------------
def _agent():
    ctx = AppContext(settings=settings, memory=SessionMemory(settings))
    ctx.llama = FakeLlama([])
    ctx.tts = FakeTts()
    return Agent(ctx)


async def test_consolidar_remove_tag_do_arquivo_usado(tmp_path):
    nota = tmp_path / "Sintese_x.md"
    nota.write_text(
        "## TensorRT acelera YOLO\nAcelera ~36%.\n#zettelkasten_atomico #conhecimento_novo\n",
        encoding="utf-8",
    )
    await _agent()._consolidar_fontes([str(nota)])
    conteudo = nota.read_text(encoding="utf-8")
    assert "#conhecimento_novo" not in conteudo        # promovido
    assert "#zettelkasten_atomico" in conteudo         # continua átomo


async def test_consolidar_sem_tag_nao_reescreve(tmp_path):
    nota = tmp_path / "manual.md"
    original = "## Nota do usuário\nConteúdo.\n#zettelkasten_atomico\n"
    nota.write_text(original, encoding="utf-8")
    mtime0 = nota.stat().st_mtime
    await _agent()._consolidar_fontes([str(nota)])
    assert nota.read_text(encoding="utf-8") == original  # idempotente, sem tag pra tirar
    assert nota.stat().st_mtime == mtime0                # nem tocou no arquivo


async def test_consolidar_arquivo_inexistente_nao_explode():
    # best-effort: fonte apagada entre a resposta e a promoção não pode derrubar nada
    await _agent()._consolidar_fontes(["/caminho/que/nao/existe.md"])


# --- EtlProcessor.summarize_dump (atomização da conversa) --------------------
class FakeVectorStore:
    def __init__(self):
        self.syncs = 0

    async def sync(self):
        self.syncs += 1


def _etl_com(resposta_llm, tmp_path, monkeypatch):
    """EtlProcessor com dump temporário e LLM falso devolvendo `resposta_llm`."""
    st = Settings(_env_file=None)
    st.caminho_obsidian = str(tmp_path)
    dump = tmp_path / "chat_dump.md"
    st.arquivo_chat_dump = str(dump)            # summarize_dump lê ESTE caminho
    os.makedirs(st.dir_conhecimento_novo, exist_ok=True)
    monkeypatch.setattr("agent.settings", st)   # summarize_dump lê o dump por aqui
    monkeypatch.setattr("agent.db.log_etl", lambda *a, **k: None)  # hermético: sem SQLite real
    ctx = AppContext(settings=st, memory=SessionMemory(st))
    ctx.llama = FakeLlama([resposta_llm])
    ctx.vectorstore = FakeVectorStore()
    return EtlProcessor(ctx), dump, st


async def test_conversa_vira_atomos_com_tag_novo(tmp_path, monkeypatch):
    atomo = "## TensorRT\nAcelera inferência YOLO.\n#zettelkasten_atomico #conhecimento_novo\n"
    etl, dump, st = _etl_com(atomo, tmp_path, monkeypatch)
    dump.write_text("## [t]\n**Usuário:** o que é tensorrt\n**Mente Digital:** acelera inferência.\n", encoding="utf-8")

    await etl.summarize_dump()

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Conversa_")]
    assert len(gerados) == 1
    conteudo = (st.dir_conhecimento_novo / gerados[0]).read_text(encoding="utf-8")
    assert prompts.TAG_NOVO in conteudo
    assert dump.read_text(encoding="utf-8") == ""      # dump limpo após salvar


async def test_conversa_small_talk_nao_cria_nota_mas_limpa_dump(tmp_path, monkeypatch):
    etl, dump, st = _etl_com("NADA", tmp_path, monkeypatch)
    dump.write_text("## [t]\n**Usuário:** oi tudo bem?\n**Mente Digital:** tudo!\n" * 3, encoding="utf-8")

    await etl.summarize_dump()

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Conversa_")]
    assert gerados == []                                # nada a reter
    assert dump.read_text(encoding="utf-8") == ""       # mas limpou o dump


async def test_conversa_multi_atomo_gera_um_arquivo_por_atomo(tmp_path, monkeypatch):
    dois = (
        "## TensorRT\nAcelera YOLO.\n#zettelkasten_atomico #conhecimento_novo\n\n"
        "## YOLO\nDetector em tempo real.\n#zettelkasten_atomico #conhecimento_novo\n"
    )
    etl, dump, st = _etl_com(dois, tmp_path, monkeypatch)
    dump.write_text(
        "## [t]\n**Usuário:** o que é tensorrt e yolo?\n"
        "**Mente Digital:** tensorrt acelera o yolo, um detector em tempo real.\n",
        encoding="utf-8",
    )

    await etl.summarize_dump()

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Conversa_")]
    assert len(gerados) == 2                             # UM arquivo por átomo (Zettelkasten puro)
    assert dump.read_text(encoding="utf-8") == ""        # dump limpo após reter


async def test_process_queue_gera_um_arquivo_por_atomo(tmp_path, monkeypatch):
    dois = (
        "## A\nideia a\n#zettelkasten_atomico #conhecimento_novo\n\n"
        "## B\nideia b\n#zettelkasten_atomico #conhecimento_novo\n"
    )
    etl, _dump, st = _etl_com(dois, tmp_path, monkeypatch)

    await etl.process_queue([("tema qualquer", "dados brutos da web")])

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Sintese_")]
    assert len(gerados) == 2
    assert etl.ctx.vectorstore.syncs == 1               # sincroniza uma vez ao fim
    for f in gerados:                                    # cada átomo nasce como curiosidade
        assert prompts.TAG_NOVO in (st.dir_conhecimento_novo / f).read_text(encoding="utf-8")


async def test_atomo_sem_cabecalho_salvo_como_um_fallback(tmp_path, monkeypatch):
    # LLM quebrou o formato (sem '##'): não pode perder o conhecimento em silêncio.
    etl, _dump, st = _etl_com("conhecimento solto sem heading", tmp_path, monkeypatch)

    await etl.process_queue([("tema", "dados")])

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Sintese_")]
    assert len(gerados) == 1                             # fallback: 1 átomo com o texto inteiro
