"""
#38 — reindex do vault fora do caminho crítico da conversa.

Escrita via ferramenta (nota/lista/captura) DURANTE a conversa não pode disparar
`VectorStore.sync()` na hora: o sync re-embeda os chunks E reconstrói a malha sobre
~13k átomos na GPU serializada, congelando o PRÓXIMO turno (~46s — STT em cuda e decode
disputando a GPU). A escrita só marca o vault "sujo"; o `EtlProcessor.run_idle` faz o
sync no idle pós-conversa, com a GPU livre.

Aqui testamos o lado do IDLE: o flush consolida o que ficou pendente e limpa a flag,
e é no-op quando nada mudou. Sem GPU/rede — vectorstore é fake.
"""
from __future__ import annotations

from agent import EtlProcessor
from config import settings
from state import AppContext


class _CountingVS:
    """Loja fake que só conta quantas vezes foi sincronizada."""

    def __init__(self) -> None:
        self.sync_calls = 0

    async def sync(self) -> None:
        self.sync_calls += 1


def _ctx() -> AppContext:
    ctx = AppContext(settings=settings)
    ctx.interactive_idle.set()          # GPU livre (ninguém interagindo)
    ctx.vectorstore = _CountingVS()
    return ctx


async def test_flush_sincroniza_e_limpa_a_flag():
    ctx = _ctx()
    ctx.vault_pendente_sync = True                 # uma nota/lista/captura ficou pendente
    etl = EtlProcessor(ctx)

    await etl._sincronizar_vault_pendente()

    assert ctx.vectorstore.sync_calls == 1         # reindexou no idle
    assert ctx.vault_pendente_sync is False        # consumido (não re-sincroniza à toa)


async def test_flush_e_noop_quando_nada_pendente():
    ctx = _ctx()
    ctx.vault_pendente_sync = False                # nenhuma escrita na sessão
    etl = EtlProcessor(ctx)

    await etl._sincronizar_vault_pendente()

    assert ctx.vectorstore.sync_calls == 0         # não paga um scan à toa


async def test_run_idle_consolida_escrita_pendente(monkeypatch):
    """O caminho de ponta a ponta do idle: mesmo sem fila web nem dump de conversa
    (sessão que só escreveu nota/lista/captura — que não alimentam o dump), o run_idle
    ainda leva a escrita ao índice. Os passos pesados do idle são isolados aqui; o alvo
    é a garantia de que o flush é chamado e a flag é limpa."""
    ctx = _ctx()
    ctx.vault_pendente_sync = True
    etl = EtlProcessor(ctx)

    async def _noop(*a, **k):
        return None

    # Isola o alvo: os passos pesados do idle (LLM/rede/snapshot) viram no-op.
    monkeypatch.setattr(etl, "process_queue", _noop)
    monkeypatch.setattr(etl, "summarize_dump", _noop)
    monkeypatch.setattr(etl, "pesquisa_proativa", _noop)
    monkeypatch.setattr(etl, "_snapshot_base", _noop)
    # Sem descarregar o modelo: run_idle retorna antes de tocar em ctx.llama.
    monkeypatch.setattr(settings, "idle_descarregar_modelo", False)

    await etl.run_idle([])

    assert ctx.vectorstore.sync_calls == 1         # a escrita pendente foi reindexada
    assert ctx.vault_pendente_sync is False        # e a flag, limpa
