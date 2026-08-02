"""O `sync` reconstruía a malha mesmo quando o índice não mudava.

Diagnóstico de 2026-08-02, com log de boot real: a malha era montada TRÊS vezes num
boot só (aos 18,2 s / 23,6 s / 36,5 s), a 2,4-5,2 s cada.

A cadeia: uma nota que não produz chunk nenhum — arquivo de 0 byte, só frontmatter,
só espaço — nunca é gravada no Chroma, logo nunca ganha entrada no índice, logo volta
a aparecer como `pendente` no boot seguinte. Para sempre. Com `pendentes` nunca vazio,
o atalho "nada novo" jamais dispara e o `sync` sempre chega ao fim, onde reconstrói a
malha. O vault do dono tinha 9 desses.

O conserto não finge que o arquivo foi indexado (isso mentiria para a purga de órfãos
e para a próxima comparação de mtime): reconhece que, sem nada gravado E sem nada
removido, o índice está idêntico — e malha idêntica não precisa ser remontada.
"""
from __future__ import annotations

import pytest

from mente_digital import rag
from mente_digital.config import settings


class _StoreFake:
    """Só o que o `sync` chama. Conta para o teste poder afirmar o que houve."""

    def __init__(self) -> None:
        self.adicionados: list = []
        self.deletados: list = []

    def add_documents(self, docs) -> None:
        self.adicionados.extend(docs)

    def delete(self, where=None) -> None:
        self.deletados.append(where)


@pytest.fixture
def loja(tmp_path, monkeypatch):
    """VectorStore com loja fake, vault temporário e malha instrumentada."""
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path))
    monkeypatch.setattr(rag, "dump_paginado", lambda *a, **k: {"metadatas": []})
    vs = rag.VectorStore(rag.EmbeddingProvider())
    vs._store = _StoreFake()
    malhas = {"n": 0}

    async def _malha_falsa() -> None:
        malhas["n"] += 1

    monkeypatch.setattr(vs, "_reconstruir_malha", _malha_falsa)
    return vs, tmp_path, malhas


async def test_nota_vazia_nao_remonta_a_malha(loja):
    """O caso do dono: 9 arquivos de 0 byte, eternamente pendentes."""
    vs, vault, malhas = loja
    (vault / "vazia.md").write_text("", encoding="utf-8")

    await vs.sync()

    assert malhas["n"] == 0, "malha remontada sobre um índice que não mudou"
    assert vs._store.adicionados == []
    assert vs._store.deletados == []


async def test_o_defeito_e_persistente_e_nao_de_uma_passada_so(loja):
    """O que fazia doer não era o custo de UM sync — era ele voltar todo boot.
    O arquivo continua pendente (correto: ele realmente não está no índice), mas
    deixa de arrastar a malha junto."""
    vs, vault, malhas = loja
    (vault / "vazia.md").write_text("", encoding="utf-8")

    for _ in range(3):
        await vs.sync()

    assert malhas["n"] == 0


async def test_so_espaco_em_branco_tambem_conta_como_vazia(loja):
    """Apagar os 9 arquivos não resolveria: o vault gera mais, e nem todos são
    de 0 byte — basta não sobrar conteúdo depois do split."""
    vs, vault, malhas = loja
    (vault / "brancos.md").write_text("   \n\n\t\n", encoding="utf-8")

    await vs.sync()

    assert malhas["n"] == 0


async def test_nota_com_conteudo_AINDA_remonta_a_malha(loja):
    """A guarda contra o conserto virar regressão: quando há o que indexar, a
    malha TEM de ser remontada — senão o conceito novo nunca entra no índice."""
    vs, vault, malhas = loja
    (vault / "cheia.md").write_text("# Titulo\n\nConteudo de verdade.\n", encoding="utf-8")

    await vs.sync()

    assert malhas["n"] == 1
    assert vs._store.adicionados, "nada foi indexado"


async def test_mistura_de_vazia_e_cheia_remonta(loja):
    """Uma nota real no meio das vazias basta para o índice mudar."""
    vs, vault, malhas = loja
    (vault / "vazia.md").write_text("", encoding="utf-8")
    (vault / "cheia.md").write_text("# Algo\n\nTexto.\n", encoding="utf-8")

    await vs.sync()

    assert malhas["n"] == 1


async def test_vault_sem_nota_nenhuma_nao_remonta(loja):
    """O atalho "nada novo" que já existia continua valendo."""
    vs, _vault, malhas = loja
    await vs.sync()
    assert malhas["n"] == 0
