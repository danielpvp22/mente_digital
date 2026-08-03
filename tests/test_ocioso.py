"""Máquina do idle automático por inatividade do usuário.

O comportamento é temporal, e os bugs naturais dele são todos de sequência:
perguntar duas vezes, nunca mais perguntar, rodar depois de o dono ter dito não,
não parar quando ele volta. Por isso a máquina é pura — aqui o relógio é um
número que os testes escrevem à mão.
"""
from __future__ import annotations

from mente_digital.ocioso import Estado, MaquinaOciosa, segundos_sem_input


def maquina(**kw) -> MaquinaOciosa:
    kw.setdefault("segundos_para_perguntar", 300.0)
    kw.setdefault("segundos_de_resposta", 15.0)
    return MaquinaOciosa(**kw)


# --------------------------------------------------------------------------- #
# O caminho feliz                                                              #
# --------------------------------------------------------------------------- #
def test_usando_a_maquina_nao_acontece_nada():
    m = maquina()
    assert m.tick(sem_input=10, agora=1000) is None
    assert m.estado is Estado.ATIVO


def test_ocioso_pergunta_uma_vez_so():
    """O bug natural aqui é perguntar a cada tique. Perguntou, cala."""
    m = maquina()
    assert m.tick(sem_input=301, agora=1000) == "perguntar"
    assert m.tick(sem_input=305, agora=1004) is None
    assert m.tick(sem_input=310, agora=1009) is None


def test_silencio_de_15s_roda():
    """O pedido do dono: sem sim nem não em 15 s, o idle começa."""
    m = maquina()
    m.tick(sem_input=301, agora=1000)
    assert m.tick(sem_input=310, agora=1014) is None      # 14 s: ainda não
    assert m.tick(sem_input=311, agora=1015) == "rodar"   # 15 s: agora sim
    assert m.estado is Estado.RODANDO


def test_roda_uma_vez_so():
    m = maquina()
    m.tick(sem_input=301, agora=1000)
    assert m.tick(sem_input=316, agora=1015) == "rodar"
    assert m.tick(sem_input=400, agora=1100) is None
    assert m.tick(sem_input=500, agora=1200) is None


# --------------------------------------------------------------------------- #
# A volta do dono                                                              #
# --------------------------------------------------------------------------- #
def test_voltar_para_o_trabalho_em_curso():
    """O requisito central: mexeu no mouse, o idle para e devolve o que pegou."""
    m = maquina()
    m.tick(sem_input=301, agora=1000)
    m.tick(sem_input=316, agora=1015)
    assert m.estado is Estado.RODANDO
    assert m.tick(sem_input=0.5, agora=1100) == "parar"
    assert m.estado is Estado.ATIVO


def test_voltar_sem_nada_rodando_nao_manda_parar():
    """Parar o que não começou mandaria o servidor mexer na VRAM à toa."""
    m = maquina()
    m.tick(sem_input=301, agora=1000)      # perguntando
    assert m.tick(sem_input=0.5, agora=1005) is None
    assert m.estado is Estado.ATIVO


def test_ciclo_completo_recomeca():
    """Voltar rearma: ficar ocioso de novo volta a perguntar."""
    m = maquina()
    assert m.tick(sem_input=301, agora=1000) == "perguntar"
    assert m.tick(sem_input=0.5, agora=1050) is None          # voltou
    assert m.tick(sem_input=301, agora=2000) == "perguntar"   # e saiu de novo


# --------------------------------------------------------------------------- #
# O "não"                                                                      #
# --------------------------------------------------------------------------- #
def test_nao_impede_de_rodar():
    m = maquina()
    m.tick(sem_input=301, agora=1000)
    assert m.responder(sim=False, agora=1003) is None
    assert m.estado is Estado.RECUSADO
    # o timeout de 15 s NÃO pode atropelar a recusa
    assert m.tick(sem_input=400, agora=1100) is None
    assert m.estado is Estado.RECUSADO


def test_nao_nao_vira_pergunta_a_cada_tique():
    """Recusa respeitada é recusa que não insiste — senão vira assédio."""
    m = maquina()
    m.tick(sem_input=301, agora=1000)
    m.responder(sim=False, agora=1003)
    for t in range(1010, 1200, 10):
        assert m.tick(sem_input=float(t - 700), agora=float(t)) is None


def test_sim_roda_na_hora():
    m = maquina()
    m.tick(sem_input=301, agora=1000)
    assert m.responder(sim=True, agora=1002) == "rodar"
    assert m.estado is Estado.RODANDO


def test_clique_atrasado_nao_desfaz_o_que_ja_comecou():
    """Passou os 15 s e o idle começou sozinho: um 'não' que chega depois não
    cancela por esta porta (para isso existe o botão de parar)."""
    m = maquina()
    m.tick(sem_input=301, agora=1000)
    assert m.tick(sem_input=316, agora=1015) == "rodar"
    assert m.responder(sim=False, agora=1016) is None
    assert m.estado is Estado.RODANDO


def test_resposta_sem_pergunta_no_ar_e_ignorada():
    m = maquina()
    assert m.responder(sim=True, agora=1000) is None
    assert m.estado is Estado.ATIVO


# --------------------------------------------------------------------------- #
# Leitura do sistema                                                           #
# --------------------------------------------------------------------------- #
def test_segundos_sem_input_e_plausivel():
    """Não dá para forçar um valor do sistema, então o teste é de sanidade: ou
    None (fora do Windows), ou um número não-negativo e menor que 49,7 dias — o
    limite em que o contador de 32 bits do Windows dá a volta."""
    v = segundos_sem_input()
    assert v is None or (0 <= v < 49.7 * 86400)


def test_ignorancia_nao_e_ociosidade():
    """Contrato do módulo: None significa 'não sei'. Quem chama trata como
    PRESENÇA — senão uma API que falhou ligaria o idle no meio do trabalho."""
    m = maquina()
    valor = segundos_sem_input()
    entrada = valor if valor is not None else 0.0
    assert m.tick(sem_input=entrada, agora=1000) in (None, "perguntar")
