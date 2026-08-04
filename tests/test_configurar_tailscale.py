"""O passo mecânico do acesso remoto — o que vem depois do login no Tailscale.

Instalar e autenticar são atos do dono. O resto é mecânico, e é aqui que o roteiro
manual erra: o handoff marca "usar o IP 100.x em vez do nome MagicDNS" como o erro
MAIS PROVÁVEL de todo o processo, porque o certificado é emitido para o NOME e, com
IP, a falha aparece como "o PC está desligado" — mandando o dono procurar no lugar
errado. Estes testes existem para que esse caminho seja impossível, não improvável.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def _carregar():
    caminho = RAIZ / "scripts" / "configurar_tailscale.py"
    spec = importlib.util.spec_from_file_location("configurar_tailscale", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ct = _carregar()


# --------------------------------------------------------------------------- #
# O nome — e a recusa de cair para o IP                                        #
# --------------------------------------------------------------------------- #
def test_tira_o_ponto_final_do_fqdn():
    """`DNSName` é um FQDN ABSOLUTO: vem com o ponto raiz no fim. Passá-lo ao
    `tailscale cert` geraria arquivo com ponto no nome e um SAN que não casa o host
    que o navegador pede — falha de verificação, com tudo "certo" na tela."""
    st = json.dumps({"Self": {"DNSName": "maquina.tail1234.ts.net."}})
    assert ct.nome_magicdns(st) == "maquina.tail1234.ts.net"


def test_recusa_ip_em_vez_de_usa_lo():
    """Um IP no lugar do nome significa MagicDNS DESLIGADO. Seguir com ele emitiria
    um certificado inútil e o dono veria "o PC está desligado" no celular — o erro
    mais caro do roteiro, porque manda procurar no lugar errado.

    Parar com None é o comportamento certo: quem chama imprime o que ligar."""
    st = json.dumps({"Self": {"DNSName": "100.101.102.103"}})
    assert ct.nome_magicdns(st) is None


def test_sem_o_campo_ou_com_json_quebrado_devolve_None():
    assert ct.nome_magicdns(json.dumps({"Self": {}})) is None
    assert ct.nome_magicdns(json.dumps({})) is None
    assert ct.nome_magicdns("não é json") is None
    assert ct.nome_magicdns("") is None
    assert ct.nome_magicdns(json.dumps({"Self": {"DNSName": "   "}})) is None


# --------------------------------------------------------------------------- #
# O .env — preservar é mais importante que escrever                            #
# --------------------------------------------------------------------------- #
def test_preserva_o_resto_do_arquivo():
    """⚠ Este `.env` tem ~200 linhas de comentário que documentam decisões MEDIDAS
    (por que o gate é 0.16, por que o embedding voltou para a CPU, por que a flag de
    multiusuário existe). Reescrevê-lo "limpo" apagaria memória do projeto que não
    está em nenhum outro lugar — o arquivo é ignorado pelo git."""
    original = (
        "# comentário importante que custou uma medição\n"
        "MENTE_N_CTX=16384\n"
        "# outro comentário\n"
        "MENTE_RAG_SCORE_CONFIDENT=0.16\n"
    )
    novo = ct.escrever_env(original, "C:/c.crt", "C:/c.key")

    for linha in original.splitlines():
        assert linha in novo, f"perdeu: {linha!r}"
    assert "MENTE_SSL_CERT=C:/c.crt" in novo
    assert "MENTE_SSL_KEY=C:/c.key" in novo


def test_substitui_a_linha_existente_em_vez_de_duplicar():
    original = "MENTE_SSL_CERT=/velho.crt\nMENTE_SSL_KEY=/velho.key\n"
    novo = ct.escrever_env(original, "/novo.crt", "/novo.key")

    assert novo.count("MENTE_SSL_CERT=") == 1
    assert "/velho.crt" not in novo
    assert "MENTE_SSL_CERT=/novo.crt" in novo


def test_substitui_tambem_a_linha_COMENTADA():
    """O `.env` real traz as duas chaves comentadas como lembrete. Se o script só
    olhasse linhas ativas, acrescentaria uma segunda ocorrência e o arquivo ficaria
    com a chave duas vezes — o pydantic resolveria pela última, calado, e um dia
    alguém descomentaria a de cima e passaria a hora procurando o motivo."""
    original = "# MENTE_SSL_CERT=/exemplo.crt\n#MENTE_SSL_KEY=/exemplo.key\n"
    novo = ct.escrever_env(original, "/real.crt", "/real.key")

    assert novo.count("MENTE_SSL_CERT=") == 1
    assert novo.count("MENTE_SSL_KEY=") == 1
    assert "/exemplo" not in novo


def test_e_idempotente():
    """Renovar o certificado é rodar isto de novo — a cada 90 dias, para sempre.
    Um script que empilha linhas a cada execução vira um arquivo ilegível em um ano."""
    texto = "MENTE_N_CTX=16384\n"
    uma = ct.escrever_env(texto, "/c.crt", "/c.key")
    duas = ct.escrever_env(uma, "/c.crt", "/c.key")
    tres = ct.escrever_env(duas, "/c.crt", "/c.key")

    assert uma == duas == tres
    assert tres.count("MENTE_SSL_CERT=") == 1


def test_acrescenta_quando_nao_existe_e_avisa_do_prazo():
    novo = ct.escrever_env("MENTE_N_CTX=16384\n", "/c.crt", "/c.key")

    assert "MENTE_SSL_CERT=/c.crt" in novo
    # O prazo é a parte que se esquece: 90 dias depois o acesso remoto para de
    # funcionar sem nada ter mudado, e o motivo tem de estar escrito ali.
    assert "90 DIAS" in novo


def test_arquivo_vazio_nao_quebra():
    novo = ct.escrever_env("", "/c.crt", "/c.key")
    assert "MENTE_SSL_CERT=/c.crt" in novo
    assert novo.endswith("\n")
