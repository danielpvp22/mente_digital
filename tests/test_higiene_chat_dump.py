"""A suíte não pode escrever no chat dump de PRODUÇÃO (2026-07-31).

O dump não é log: é a FILA que `EtlProcessor.summarize_dump` consome no idle e
atomiza como nota PERMANENTE no vault do dono. Dois testes de `test_preempcao.py`
exercitavam o caminho declarativo com o `settings` global e gravavam
"**Mente Digital:** Entendido, registrei." em `dados/chat_dump_bruto.md` a cada
rodada — lixo que a próxima passada de idle transformaria em átomo Zettelkasten.

Mesma família do isolamento do SQLite (consultoria TTFT #5) e do log em arquivo.
Este arquivo é o alarme: se alguém remover o redirect do `conftest`, quebra AQUI e
não em silêncio no vault de alguém.
"""
import os
from pathlib import Path

import pytest

from mente_digital import etl
from mente_digital.config import DIR_DADOS
from mente_digital.config import settings

DUMP_DE_PRODUCAO = Path(DIR_DADOS) / "chat_dump_bruto.md"


def test_o_dump_configurado_nao_e_o_de_producao():
    """O alarme direto: o caminho que a suíte usa não pode ser o do vault."""
    assert Path(settings.arquivo_chat_dump).resolve() != DUMP_DE_PRODUCAO.resolve()


def test_o_dump_configurado_esta_fora_de_dados():
    """Nem o arquivo, nem um vizinho dentro de dados/ — o diretório inteiro é do dono."""
    usado = Path(settings.arquivo_chat_dump).resolve()
    assert DIR_DADOS.resolve() not in usado.parents


async def test_gravar_no_dump_nao_toca_o_arquivo_de_producao():
    """O teste que importa: EXERCITA o gravador de verdade e prova que o arquivo do
    vault não muda — é o que os dois testes de preempção violavam sem ninguém ver."""
    antes = DUMP_DE_PRODUCAO.stat().st_size if DUMP_DE_PRODUCAO.exists() else None

    await etl.append_chat_dump("User", "uma pergunta de teste")
    await etl.append_chat_dump("IA", "Entendido, registrei.")

    depois = DUMP_DE_PRODUCAO.stat().st_size if DUMP_DE_PRODUCAO.exists() else None
    assert depois == antes, "a suíte escreveu no chat dump de produção"
    # E o conteúdo foi PARAR em algum lugar — senão o teste passaria por não gravar nada.
    assert os.path.getsize(settings.arquivo_chat_dump) > 0


@pytest.mark.parametrize("ator", ["User", "IA"])
async def test_os_dois_atores_escrevem_no_dump_isolado(ator):
    """Os dois ramos do `append_chat_dump` gravam — e nenhum toca o arquivo do vault.

    A assercao é sobre o TAMANHO do arquivo de produção, não sobre o conteúdo dele:
    a 1ª versão deste teste procurava a string "Entendido" lá dentro, o que faz o
    resultado depender do que o dono conversou. Teste não afere dado de usuário."""
    antes = DUMP_DE_PRODUCAO.stat().st_size if DUMP_DE_PRODUCAO.exists() else None

    await etl.append_chat_dump(ator, "conteúdo de teste")

    depois = DUMP_DE_PRODUCAO.stat().st_size if DUMP_DE_PRODUCAO.exists() else None
    assert depois == antes
    assert Path(settings.arquivo_chat_dump).exists()
