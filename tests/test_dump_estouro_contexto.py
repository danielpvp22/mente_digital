"""O idle não pode APAGAR a conversa quando o LLM falha.

Regressão real (2026-07-31, servidor de pé às 14:04): o dump acumulara 2 dias sem
ser limpo — 118 turnos, ~57k chars — e o `summarize_dump` o mandou INTEIRO ao LLM:

    ValueError: Requested tokens (21277) exceed context window of 8192

O worker do `llm.py` loga o erro e ENCERRA a stream, então `collect()` devolveu "".
O ETL leu string vazia como "conversa sem conhecimento novo a reter" e apagou o dump.
Dois defeitos num só: o prompt sem teto, e a falha indistinguível do silêncio legítimo.

Aqui trancamos os dois: (1) vazio/exceção NUNCA limpa o dump; (2) dump grande é
FATIADO em lotes que cabem no n_ctx, e nenhum prompt volta a passar do orçamento.
"""
import os

import pytest

from mente_digital.config import Settings
from mente_digital.etl import EtlProcessor
from mente_digital.state import AppContext

pytestmark = pytest.mark.asyncio


class LlamaRoteirizado:
    """LLM falso com resposta POR CHAMADA: string devolve, Exception levanta.

    O FakeLlama do conftest repete os mesmos tokens em toda chamada — não serve para
    testar uma SÉRIE de lotes em que um vai bem e o outro morre.
    """

    def __init__(self, roteiro):
        self.roteiro = list(roteiro)
        self.prompts = []

    async def collect(self, prompt: str, **kwargs) -> str:
        self.prompts.append(prompt)
        passo = self.roteiro.pop(0) if self.roteiro else ""
        if isinstance(passo, Exception):
            raise passo
        return passo


class VectorStoreFake:
    def __init__(self):
        self.syncs = 0

    async def sync(self):
        self.syncs += 1


def _etl(roteiro, tmp_path, monkeypatch):
    st = Settings(_env_file=None)
    st.caminho_obsidian = str(tmp_path)
    dump = tmp_path / "chat_dump.md"
    st.arquivo_chat_dump = str(dump)
    st.diapasao_habilitado = False          # o perfil tem prompt próprio; fora deste teste
    os.makedirs(st.dir_conhecimento_novo, exist_ok=True)
    monkeypatch.setattr("mente_digital.etl.settings", st)
    monkeypatch.setattr("mente_digital.etl.db.log_etl", lambda *a, **k: None)
    ctx = AppContext(settings=st)
    ctx.llama = LlamaRoteirizado(roteiro)
    ctx.vectorstore = VectorStoreFake()
    return EtlProcessor(ctx), dump, st, ctx.llama


def _conversa(turnos: int, tamanho: int = 40) -> str:
    return "\n\n".join(
        f"## [2026-07-31 14:{i % 60:02d}:00]\n**Usuário:** pergunta {i}\n"
        f"**Mente Digital:** " + ("resposta " * tamanho)
        for i in range(turnos)
    )


ATOMO = "## TensorRT\nAcelera inferência YOLO.\n#zettelkasten_atomico #conhecimento_novo\n"


async def test_llm_vazio_preserva_o_dump(tmp_path, monkeypatch):
    """O defeito exato: stream morta -> collect '' -> dump apagado. Nunca mais."""
    etl, dump, st, _ = _etl([""], tmp_path, monkeypatch)
    original = _conversa(2)
    dump.write_text(original, encoding="utf-8")

    await etl.summarize_dump()

    assert dump.read_text(encoding="utf-8") == original, "conversa foi apagada por uma falha do LLM"
    assert [f for f in os.listdir(st.dir_conhecimento_novo)] == []


async def test_excecao_do_llm_preserva_o_dump(tmp_path, monkeypatch):
    """O caso literal do log: ValueError de contexto estourado."""
    estouro = ValueError("Requested tokens (21277) exceed context window of 8192")
    etl, dump, st, _ = _etl([estouro], tmp_path, monkeypatch)
    original = _conversa(2)
    dump.write_text(original, encoding="utf-8")

    await etl.summarize_dump()

    assert dump.read_text(encoding="utf-8") == original


async def test_nada_continua_limpando_o_dump(tmp_path, monkeypatch):
    """A contrapartida: silêncio LEGÍTIMO (o sentinela do prompt) ainda limpa."""
    etl, dump, st, _ = _etl(["NADA"], tmp_path, monkeypatch)
    dump.write_text(_conversa(2), encoding="utf-8")

    await etl.summarize_dump()

    assert dump.read_text(encoding="utf-8") == ""
    assert [f for f in os.listdir(st.dir_conhecimento_novo)] == []


async def test_dump_de_dois_dias_vira_varios_lotes_dentro_do_orcamento(tmp_path, monkeypatch):
    """~57k chars: uma chamada só estourava. Agora são N, cada uma cabendo no n_ctx."""
    etl, dump, st, llama = _etl([ATOMO] * 40, tmp_path, monkeypatch)
    conteudo = _conversa(140, tamanho=60)
    assert len(conteudo) > 55_000, "fixture precisa reproduzir a escala do dump real"
    dump.write_text(conteudo, encoding="utf-8")

    await etl.summarize_dump()

    assert len(llama.prompts) > 1, "dump grande tinha de ser fatiado"
    # O prompt tem moldura fixa (instruções) além do lote — o que importa é que o
    # TEXTO injetado respeite o orçamento; a moldura é de algumas centenas de chars.
    for p in llama.prompts:
        assert len(p) < st.etl_lote_chars + 2000
    assert dump.read_text(encoding="utf-8") == ""     # tudo atomizado -> pode limpar


async def test_lote_que_falha_no_meio_salva_o_resto_e_preserva_o_dump(tmp_path, monkeypatch):
    """Meia atomização é melhor que nenhuma — mas o dump fica para o retry."""
    etl, dump, st, llama = _etl([ATOMO, RuntimeError("morreu no 2º lote"), ATOMO],
                                tmp_path, monkeypatch)
    original = _conversa(60, tamanho=60)
    dump.write_text(original, encoding="utf-8")

    await etl.summarize_dump()

    assert len(llama.prompts) > 1
    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Conversa_")]
    assert gerados, "o lote que deu certo tinha de virar átomo"
    assert dump.read_text(encoding="utf-8") == original, "com falha em jogo o dump não se limpa"


async def test_pagina_web_gigante_e_fatiada_no_process_queue(tmp_path, monkeypatch):
    """A colheita enfileira o corpo inteiro (até web_fetch_max_chars = 20.000)."""
    etl, _dump, st, llama = _etl([ATOMO] * 20, tmp_path, monkeypatch)
    pagina = "\n\n".join(f"paragrafo {i} " + ("conteudo " * 80) for i in range(30))
    assert len(pagina) > 19_000

    await etl.process_queue([("tema quente", pagina)])

    assert len(llama.prompts) > 1, "página grande tinha de ser fatiada"
    for p in llama.prompts:
        assert len(p) < st.etl_lote_chars + 2000
    assert [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Sintese_")]
