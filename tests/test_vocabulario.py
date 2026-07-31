"""Ponte de vocabulário: jargão inglês do dono -> termo PT que o vault usa.

O caso real que originou o módulo: "o que é topping" respondeu sobre cobertura de
pizza porque o aterramento léxico casa TOKEN, e o vault escreve o assunto como
"Dominância Apical" / "poda" / "meristema" — nenhum token em comum com a palavra
que o dono digitou.
"""
from mente_digital import vocabulario


def test_jargao_ganha_os_termos_pt_do_vault():
    fora = vocabulario.expandir("topping cannabis")
    assert "topping" in fora            # o termo do dono nunca é descartado
    for pt in ("poda", "apical", "meristema", "auxinas"):
        assert pt in fora


def test_pergunta_sem_jargao_sai_INTACTA():
    """O caso comum não paga nada: pergunta em português volta idêntica."""
    termos = "quanto tempo secar as flores"
    assert vocabulario.expandir(termos) == termos


def test_nao_repete_termo_que_ja_esta_na_query():
    """O extrator trabalhou para deixar a query em 5 palavras; duplicar a infla à toa."""
    fora = vocabulario.expandir("topping poda")
    assert fora.split().count("poda") == 1


def test_casa_sem_acento_e_sem_caixa():
    assert "meristema" in vocabulario.expandir("Topping")


def test_palavra_inteira_nao_casa_por_dentro_de_outra():
    """'stopping' contém 'topping' — casar por substring aterraria a pergunta errada."""
    termos = "stopping o cronometro"
    assert vocabulario.expandir(termos) == termos


def test_query_vazia_nao_quebra():
    assert vocabulario.expandir("") == ""


def test_pt_correto_alcanca_o_pt_errado_do_vault():
    """O turno que falhou no teste real: ele digita "oídio", o vault diz "mildio".

    `melhor_dist` deu 0,16137 contra o limiar 0,16 — a pergunta perdeu por um fio e
    foi para a web, enquanto o vault tinha "Controle com Serenade" e "AQ10 como
    Biofungicida".
    """
    fora = vocabulario.expandir("controlar oidio?")
    assert "mildio" in fora and "mildew" in fora


def test_ponte_pt_casa_com_acento_como_o_dono_digita():
    """As chaves são guardadas sem acento; quem digita "oídio" tem de casar."""
    assert "mildio" in vocabulario.expandir("como controlar o oídio")


def test_termo_sem_lacuna_NAO_entra_na_tabela():
    """Ponte onde não há lacuna só suja o aterramento.

    Medido em 2026-07-30: estes já têm cobertura de sobra no vault com o nome que o
    dono usaria (ácaros 113 chunks, muda 250, transplante 152, estaca 78).
    """
    for termo in ("acaros", "muda", "transplante", "estaca", "lagarta"):
        assert termo not in vocabulario.PONTE


def test_tabela_so_tem_sinonimo_medido_no_vault():
    """Guarda da REGRA do módulo: sinônimo plausível que o vault não usa é peso morto.

    Estes quatro foram medidos com 0 chunks de livro (2026-07-30) e por isso NÃO
    podem voltar à tabela sem uma nova medição que os justifique.
    """
    reprovados = {"poda apical", "desponte", "despontar", "beliscar"}
    for sinonimos in vocabulario.PONTE.values():
        assert not (set(sinonimos) & reprovados)
