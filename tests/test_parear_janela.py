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


# --------------------------------------------------------------------------- #
# O `--base`: a porta de socorro tem de abrir JUSTAMENTE quando é necessária   #
# --------------------------------------------------------------------------- #
class TestBaseDefault:
    """O defeito que estes testes existem para impedir de voltar.

    Até 2026-08-08 o default era `https://127.0.0.1:PORTA` com verificação
    ESTRITA de TLS — e o certificado desta máquina cobre só o nome MagicDNS.
    Nenhum certificado público pode cobrir um IP de loopback, então o script
    falhava por NOME. E o `except` largo imprimia "não alcancei o servidor", o que
    manda procurar rede caída quando o servidor respondeu perfeitamente.

    Isso importa mais que um bug comum: este é o script de RESGATE da credencial.
    Ele quebrar em silêncio significa quebrar no único dia em que se precisa dele.
    """

    def test_em_https_o_default_nao_e_loopback(self, tmp_path):
        """A regra vem de `app._host_da_janela` — mesma fonte da janela nativa,
        para não haver duas cópias que divirjam no primeiro conserto de uma."""
        import app

        cert = tmp_path / "maquina-exemplo.tail0a1b2c.ts.net.crt"
        cert.write_text("não é um PEM de verdade", encoding="utf-8")
        host = app._host_da_janela("https", str(cert))
        assert host == "maquina-exemplo.tail0a1b2c.ts.net"
        assert host not in ("127.0.0.1", "localhost")

    def test_sem_tls_o_loopback_continua_certo(self):
        """Em `http` não há nome a conferir, e o IP é o caminho que não depende de
        DNS nenhum — inverter isto quebraria quem roda sem certificado."""
        import app

        assert app._host_da_janela("http", "") == "127.0.0.1"

    def test_o_erro_de_certificado_nao_se_disfarca_de_queda_de_rede(self):
        """Certificado e rede são defeitos DIFERENTES, e o `urllib` embrulha os
        dois no mesmo `URLError`. O ramo tem de existir — sem ele o dono lê "não
        alcancei o servidor" e vai depurar a rede enquanto o servidor responde."""
        import inspect
        import ssl as _ssl

        fonte = inspect.getsource(parear_janela._resgatar)
        assert "SSLCertVerificationError" in fonte
        assert hasattr(_ssl, "SSLCertVerificationError")     # existe no Python desta env
