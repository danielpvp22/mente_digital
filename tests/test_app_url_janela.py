"""Que endereço a janela nativa carrega — e por que não é sempre `127.0.0.1`.

Regressão medida em 2026-08-04, no dia em que o TLS entrou. A janela pedia
`https://127.0.0.1:8000`, e NENHUM certificado público pode cobrir um IP de
loopback: o Chrome parava em `ERR_CERT_COMMON_NAME_INVALID` e a única saída era o
dono clicar em "Avançado → permitir" toda vez que abrisse o próprio aplicativo —
guardando uma exceção de certificado no navegador para uso doméstico.

O que estes testes protegem não é a string da URL, é a propriedade: **o host da
janela tem de ser um nome que o certificado cobre**, senão o app só abre por cima
de um aviso de segurança.
"""
from __future__ import annotations

import app


# --------------------------------------------------------------------------- #
# A regra                                                                      #
# --------------------------------------------------------------------------- #
def test_sem_tls_continua_no_ip_de_loopback():
    """Em `http` o IP é o certo: não depende de DNS nenhum, e é o caminho que
    funciona antes de existir Tailscale, cert ou rede."""
    assert app._host_da_janela("http", "") == "127.0.0.1"
    assert app._host_da_janela("http", r"C:\certs\qualquer.crt") == "127.0.0.1"


def test_a_janela_prefere_a_credencial_de_aparelho():
    """A credencial de janela vem PRIMEIRO; o legado é queda, não preferência.

    Sem isto, `MENTE_APARELHOS_TOKEN_LEGADO=false` tranca o dono fora do próprio
    aplicativo: a janela abria sempre com o `access_token`, e o front sobrescreve
    o `localStorage` com o `?token=` da URL a cada abertura — de modo que
    reparear a janela não adiantava, o token morto apagava a credencial boa no
    lance seguinte.
    """
    assert app.credencial_da_janela("mdk1.abc.segredo", "legado") == "mdk1.abc.segredo"


def test_sem_credencial_de_janela_nada_muda_para_quem_nao_configurou():
    """Quem não fez nada continua exatamente como estava — a ponte de migração
    do `aparelhos_token_legado` depende disso."""
    assert app.credencial_da_janela("", "legado") == "legado"
    assert app.credencial_da_janela("   ", "legado") == "legado"


def test_sem_segredo_nenhum_a_url_sai_limpa():
    """Vazio dos dois lados = servidor sem gate (loopback), e a URL não deve
    ganhar um `?token=` vazio."""
    assert app.credencial_da_janela("", "") == ""


def test_espaco_em_volta_nao_vira_credencial():
    """Um `.env` com espaço sobrando não pode virar um token com espaço — ele
    viajaria na query e o gate recusaria sem dizer por quê."""
    assert app.credencial_da_janela(" mdk1.abc.segredo ", "") == "mdk1.abc.segredo"


def test_com_tls_usa_o_nome_do_certificado(tmp_path, monkeypatch):
    cert = tmp_path / "sechex-blrzc2v.tail412b37.ts.net.crt"
    cert.write_text("não é um PEM de verdade", encoding="utf-8")
    # Decodificar falha (não é PEM), então cai no nome do arquivo — que é
    # justamente o caminho de queda que este módulo promete.
    assert app._host_da_janela("https", str(cert)) == "sechex-blrzc2v.tail412b37.ts.net"


def test_com_tls_e_cert_ausente_nao_inventa_host(tmp_path):
    """Sem cert no disco não há nome a usar. Voltar para o IP reproduz o aviso
    antigo — mas é MELHOR que montar uma URL que não resolve, que não abriria
    nada."""
    assert app._host_da_janela("https", str(tmp_path / "sumiu.crt")) == "127.0.0.1"
    assert app._host_da_janela("https", "") == "127.0.0.1"


def test_nome_de_arquivo_generico_e_RECUSADO(tmp_path):
    """`cert.crt` viraria `https://cert:8000`, que não resolve em lugar nenhum —
    trocaria um aviso de certificado por uma janela em branco, que é pior porque
    não diz o que houve."""
    generico = tmp_path / "cert.crt"
    generico.write_text("x", encoding="utf-8")
    assert app._host_da_janela("https", str(generico)) == "127.0.0.1"


def test_o_certificado_de_verdade_manda_mais_que_o_nome_do_arquivo(tmp_path, monkeypatch):
    """O nome do arquivo é a QUEDA, não a fonte. Um cert posto à mão pode se chamar
    qualquer coisa, e aí o arquivo mentiria sobre o que ele cobre."""
    cert = tmp_path / "apelido_qualquer.crt"
    cert.write_text("x", encoding="utf-8")

    import ssl
    monkeypatch.setattr(
        ssl._ssl, "_test_decode_cert",
        lambda _p: {"subjectAltName": (("DNS", "casa.exemplo.ts.net"),)},
        raising=False,
    )
    assert app._host_da_janela("https", str(cert)) == "casa.exemplo.ts.net"


def test_api_privada_do_ssl_sumindo_nao_quebra_a_janela(tmp_path, monkeypatch):
    """`ssl._ssl._test_decode_cert` é API PRIVADA do CPython. Se ela sumir num
    upgrade, a janela tem de voltar ao caminho de antes — não estourar no boot do
    aplicativo, que é a hora em que o dono não tem console para ler o traceback."""
    cert = tmp_path / "nome.valido.ts.net.crt"
    cert.write_text("x", encoding="utf-8")

    import ssl

    def _explode(_p):
        raise AttributeError("sumiu no upgrade")

    monkeypatch.setattr(ssl._ssl, "_test_decode_cert", _explode, raising=False)
    assert app._host_da_janela("https", str(cert)) == "nome.valido.ts.net"
