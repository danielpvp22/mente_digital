"""A escrita do `.env` que dá credencial à janela nativa.

O que estes testes protegem é o arquivo do dono: um `.env` é escrito à mão, tem
comentários que explicam decisões e uma ordem que faz sentido para quem o mantém.
Um script que o reescreve inteiro "consertando" a formatação destrói trabalho
humano — então a regra é substituir UMA linha e não tocar em mais nada.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_CAMINHO = Path(__file__).resolve().parent.parent / "scripts" / "parear_janela.py"
_spec = importlib.util.spec_from_file_location("parear_janela", _CAMINHO)
parear_janela = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parear_janela)

env_com = parear_janela.env_com
CHAVE = parear_janela.CHAVE_ENV


def test_acrescenta_quando_a_chave_nao_existe():
    saida = env_com("MENTE_N_CTX=4096\n", CHAVE, "mdk1.abc.seg")
    assert saida == f"MENTE_N_CTX=4096\n{CHAVE}=mdk1.abc.seg\n"


def test_substitui_no_lugar_preservando_a_ordem():
    entrada = f"MENTE_N_CTX=4096\n{CHAVE}=velha\nMENTE_PORT=8000\n"
    saida = env_com(entrada, CHAVE, "nova")
    assert saida == f"MENTE_N_CTX=4096\n{CHAVE}=nova\nMENTE_PORT=8000\n"


def test_substitui_a_linha_COMENTADA():
    """O dono costuma deixar o exemplo comentado no arquivo. Ignorá-lo faria o
    script parecer que 'não fez nada' — e é a armadilha que o
    `configurar_tailscale.py` já tinha documentado."""
    entrada = f"# {CHAVE}=cole aqui\nMENTE_PORT=8000\n"
    saida = env_com(entrada, CHAVE, "mdk1.abc.seg")
    assert saida == f"{CHAVE}=mdk1.abc.seg\nMENTE_PORT=8000\n"


def test_e_idempotente():
    """Rodar de novo (repareamento) tem de dar o mesmo arquivo, não empilhar linhas."""
    uma = env_com("MENTE_PORT=8000\n", CHAVE, "x")
    duas = env_com(uma, CHAVE, "x")
    assert uma == duas
    assert duas.count(CHAVE) == 1


def test_arquivo_sem_quebra_no_fim_nao_gruda_as_linhas():
    """Sem isto o `.env` terminaria com `MENTE_PORT=8000MENTE_JANELA_...` — uma
    variável a menos e outra com valor errado, sem erro nenhum na tela."""
    saida = env_com("MENTE_PORT=8000", CHAVE, "x")
    assert saida == f"MENTE_PORT=8000\n{CHAVE}=x\n"


def test_nao_toca_no_resto_do_arquivo():
    entrada = (
        "# o contexto do modelo\n"
        "MENTE_N_CTX=4096\n"
        "\n"
        "# acesso\n"
        "MENTE_ACCESS_TOKEN=segredo\n"
    )
    saida = env_com(entrada, CHAVE, "x")
    assert saida.startswith(entrada)
    assert "MENTE_ACCESS_TOKEN=segredo" in saida
