"""
Identidade POR APARELHO — as regras PURAS (aparelhos.py).

Cobre os caminhos de RECUSA com o mesmo peso dos de aceitação: num gate de acesso, o
teste que importa é o que prova que a porta FICA FECHADA. Sem rede, sem GPU, sem SQLite —
o instante é injetado, como em test_agenda.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from mente_digital import aparelhos
from mente_digital.aparelhos import (
    MOTIVO_BLOQUEADO,
    MOTIVO_CODIGO_EXPIRADO,
    MOTIVO_CODIGO_INVALIDO,
    MOTIVO_CODIGO_USADO,
    MOTIVO_DESCONHECIDA,
    MOTIVO_DESLIGADO_NEGADO,
    MOTIVO_DESLIGADO_OK,
    MOTIVO_EXPIRADO,
    MOTIVO_LOOPBACK,
    MOTIVO_OK,
    MOTIVO_REVOGADO,
    MOTIVO_SEGREDO_ERRADO,
    MOTIVO_SEM_CREDENCIAL,
    MOTIVO_TOKEN_LEGADO,
    Aparelho,
    Situacao,
    avaliar,
)

AGORA = datetime(2026, 8, 3, 12, 0, 0)
# O formato exige >= 16 chars de segredo (o real tem 43). Um literal curto como "x"
# faz `partir_credencial` devolver None e o teste passa a exercitar o caminho LEGADO
# sem perceber — foi o que aconteceu na 1ª escrita destes testes.
SEGREDO = "segredo-de-teste"
OUTRO_SEGREDO = "segredo-diferente-do-certo"


def _aparelho(segredo: str = SEGREDO, **kw) -> Aparelho:
    sal = aparelhos.gerar_sal()
    base = dict(
        id="a" * 16,
        apelido="celular do dono",
        impressao=aparelhos.impressao(segredo, sal),
        sal=sal,
        criado_em=AGORA.isoformat(),
    )
    base.update(kw)
    return Aparelho(**base)


def _situacao(**kw) -> Situacao:
    base = dict(
        habilitado=True, host="203.0.113.9", credencial=None, token_legado="",
        aceita_token_legado=True, agora=AGORA,
    )
    base.update(kw)
    return Situacao(**base)


# -- Compatibilidade: DESLIGADO é o gate de hoje, byte a byte -------------------
def test_desligado_repete_o_gate_de_hoje_loopback():
    # Sem token configurado, loopback entra — exatamente test_acesso.py.
    v = avaliar(_situacao(habilitado=False, host="127.0.0.1"))
    assert v.autorizado is True and v.motivo == MOTIVO_DESLIGADO_OK


def test_desligado_repete_o_gate_de_hoje_lan_fica_fora():
    v = avaliar(_situacao(habilitado=False, host="192.168.0.42"))
    assert v.autorizado is False and v.motivo == MOTIVO_DESLIGADO_NEGADO


def test_desligado_com_token_o_segredo_libera():
    v = avaliar(_situacao(habilitado=False, host="192.168.0.42",
                          credencial="s3gr3do", token_legado="s3gr3do"))
    assert v.autorizado is True


def test_desligado_com_token_errado_nega_ate_loopback():
    v = avaliar(_situacao(habilitado=False, host="127.0.0.1",
                          credencial="errado", token_legado="s3gr3do"))
    assert v.autorizado is False


def test_desligado_ignora_aparelho_valido():
    # A função nova NÃO pode abrir porta nenhuma enquanto estiver desligada.
    ap = _aparelho()
    cred = aparelhos.montar_credencial(ap.id, SEGREDO)
    v = avaliar(_situacao(habilitado=False, host="203.0.113.9", credencial=cred,
                          aparelho=ap, segredo_confere=True))
    assert v.autorizado is False


# -- Credencial de aparelho ----------------------------------------------------
def test_aparelho_valido_entra_de_qualquer_lugar():
    ap = _aparelho()
    cred = aparelhos.montar_credencial(ap.id, SEGREDO)
    v = avaliar(_situacao(credencial=cred, aparelho=ap, segredo_confere=True))
    assert v.autorizado is True
    assert v.motivo == MOTIVO_OK
    assert v.aparelho_id == ap.id


def test_segredo_errado_nega_e_nao_vaza_que_o_id_existe():
    ap = _aparelho()
    cred = aparelhos.montar_credencial(ap.id, OUTRO_SEGREDO)
    v = avaliar(_situacao(credencial=cred, aparelho=ap, segredo_confere=False))
    assert v.autorizado is False
    assert v.motivo == MOTIVO_SEGREDO_ERRADO           # trilha do dono: fino
    assert v.motivo_publico == "credencial_invalida"   # resposta HTTP: grosso


def test_id_desconhecido_responde_igual_a_segredo_errado():
    # O sondador não pode descobrir quais ids existem comparando as respostas.
    cred = aparelhos.montar_credencial("f" * 16, OUTRO_SEGREDO)
    v = avaliar(_situacao(credencial=cred, aparelho=None))
    assert v.autorizado is False
    assert v.motivo == MOTIVO_DESCONHECIDA
    assert v.motivo_publico == "credencial_invalida"


def test_revogado_nega_mesmo_com_o_segredo_certo():
    # O segredo do aparelho roubado continua correto para sempre — quem manda é o registro.
    ap = _aparelho(revogado_em=AGORA.isoformat())
    cred = aparelhos.montar_credencial(ap.id, SEGREDO)
    v = avaliar(_situacao(credencial=cred, aparelho=ap, segredo_confere=True))
    assert v.autorizado is False and v.motivo == MOTIVO_REVOGADO


def test_revogado_e_dito_ao_cliente_para_ele_mostrar_a_tela_certa():
    ap = _aparelho(revogado_em=AGORA.isoformat())
    v = avaliar(_situacao(credencial=aparelhos.montar_credencial(ap.id, SEGREDO),
                          aparelho=ap, segredo_confere=True))
    assert v.motivo_publico == MOTIVO_REVOGADO


def test_expirado_nega_com_o_segredo_certo():
    ap = _aparelho(expira_em=(AGORA - timedelta(seconds=1)).isoformat())
    cred = aparelhos.montar_credencial(ap.id, SEGREDO)
    v = avaliar(_situacao(credencial=cred, aparelho=ap, segredo_confere=True))
    assert v.autorizado is False and v.motivo == MOTIVO_EXPIRADO


def test_sem_data_de_expiracao_nao_expira():
    ap = _aparelho(expira_em=None)
    v = avaliar(_situacao(credencial=aparelhos.montar_credencial(ap.id, SEGREDO),
                          aparelho=ap, segredo_confere=True))
    assert v.autorizado is True


def test_data_de_expiracao_corrompida_e_tratada_como_expirada():
    # Fail-closed: campo ilegível não pode virar credencial eterna.
    ap = _aparelho(expira_em="nao-e-data")
    v = avaliar(_situacao(credencial=aparelhos.montar_credencial(ap.id, SEGREDO),
                          aparelho=ap, segredo_confere=True))
    assert v.autorizado is False and v.motivo == MOTIVO_EXPIRADO


# -- Bloqueio progressivo ------------------------------------------------------
def test_bloqueado_nega_mesmo_com_credencial_valida():
    # Senão a força bruta que ACERTA no meio do castigo entraria.
    ap = _aparelho()
    v = avaliar(_situacao(credencial=aparelhos.montar_credencial(ap.id, SEGREDO),
                          aparelho=ap, segredo_confere=True,
                          bloqueado_ate=AGORA + timedelta(seconds=30)))
    assert v.autorizado is False and v.motivo == MOTIVO_BLOQUEADO


def test_bloqueio_vencido_deixa_passar():
    ap = _aparelho()
    v = avaliar(_situacao(credencial=aparelhos.montar_credencial(ap.id, SEGREDO),
                          aparelho=ap, segredo_confere=True,
                          bloqueado_ate=AGORA - timedelta(seconds=1)))
    assert v.autorizado is True


def test_bloqueio_nao_tranca_o_dono_fora_da_propria_maquina():
    # O painel que REVOGA o invasor roda no loopback; bloquear ali seria trancar a
    # chave do lado de dentro.
    v = avaliar(_situacao(host="127.0.0.1", bloqueado_ate=AGORA + timedelta(seconds=300)))
    assert v.autorizado is True and v.motivo == MOTIVO_LOOPBACK


def test_atraso_dobra_e_respeita_o_teto():
    assert aparelhos.atraso_bloqueio(1, 2.0, 900.0) == 0.0   # dedo errado não castiga
    assert aparelhos.atraso_bloqueio(2, 2.0, 900.0) == 0.0
    assert aparelhos.atraso_bloqueio(3, 2.0, 900.0) == 2.0
    assert aparelhos.atraso_bloqueio(4, 2.0, 900.0) == 4.0
    assert aparelhos.atraso_bloqueio(5, 2.0, 900.0) == 8.0
    assert aparelhos.atraso_bloqueio(99, 2.0, 900.0) == 900.0  # teto, sem overflow


def test_o_castigo_limita_os_chutes_dentro_da_validade_do_codigo():
    """Quantos chutes cabem nos 10 min de vida do código de pareamento?

    MEDIDO: 11. O acumulado do castigo cresce geometricamente, então o atacante gasta
    a janela inteira em uma dezena de tentativas — contra 31**10 ≈ 8,2e14 códigos
    possíveis. É esta conta, e não o tamanho do código sozinho, que sustenta usar um
    segredo curto o bastante para ser digitado à mão.
    """
    janela_segundos = 10 * 60
    gasto, chutes = 0.0, 0
    while gasto <= janela_segundos:
        chutes += 1
        gasto += aparelhos.atraso_bloqueio(chutes, 2.0, 900.0)
    assert chutes == 11
    assert len(_ALFABETO) ** aparelhos.TAMANHO_CODIGO > 1e14


_ALFABETO = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


# -- Legado convivendo com a função ligada -------------------------------------
def test_token_legado_ainda_abre_quando_permitido():
    v = avaliar(_situacao(credencial="s3gr3do", token_legado="s3gr3do"))
    assert v.autorizado is True and v.motivo == MOTIVO_TOKEN_LEGADO


def test_token_legado_pode_ser_desligado_apos_migrar_os_celulares():
    v = avaliar(_situacao(credencial="s3gr3do", token_legado="s3gr3do",
                          aceita_token_legado=False))
    assert v.autorizado is False


def test_loopback_continua_entrando_com_a_funcao_ligada():
    v = avaliar(_situacao(host="127.0.0.1", credencial=None, token_legado=""))
    assert v.autorizado is True and v.motivo == MOTIVO_LOOPBACK


def test_sem_credencial_de_fora_e_recusa_propria():
    # Motivo próprio porque o registro NÃO conta isto como falha (senão a SPA sem
    # token configurado bloquearia o dono sozinha).
    v = avaliar(_situacao(host="203.0.113.9", credencial=None, token_legado="s3gr3do"))
    assert v.autorizado is False and v.motivo == MOTIVO_SEM_CREDENCIAL


# -- Formato da credencial -----------------------------------------------------
def test_credencial_vai_e_volta():
    cred = aparelhos.montar_credencial("abcdef0123456789", "um-segredo-bem-grande-aqui")
    assert aparelhos.partir_credencial(cred) == ("abcdef0123456789", "um-segredo-bem-grande-aqui")


def test_o_que_nao_tem_cara_de_credencial_devolve_none():
    # None não é recusa — é "tente a regra legada". É o que mantém o token de hoje vivo.
    for ruim in [None, "", "s3gr3do", "mdk1.xx.yy", "mdk9." + "a" * 16 + ".segredo-grande-o-suficiente",
                 "mdk1." + "z" * 16 + ".segredo-grande-o-suficiente"]:
        assert aparelhos.partir_credencial(ruim) is None


def test_credencial_com_espaco_em_volta_ainda_casa():
    cred = aparelhos.montar_credencial("abcdef0123456789", "um-segredo-bem-grande-aqui")
    assert aparelhos.partir_credencial(f"  {cred}\n") is not None


def test_segredo_gerado_tem_entropia_e_nao_repete():
    segredos = {aparelhos.gerar_segredo() for _ in range(200)}
    assert len(segredos) == 200
    assert all(len(s) >= 40 for s in segredos)


def test_impressao_muda_com_o_sal():
    # Dois aparelhos com o mesmo segredo não podem ter a mesma impressão.
    assert aparelhos.impressao("igual", "sal-a") != aparelhos.impressao("igual", "sal-b")


def test_confere_segredo_aceita_o_certo_e_recusa_o_resto():
    sal = aparelhos.gerar_sal()
    marca = aparelhos.impressao("certo", sal)
    assert aparelhos.confere_segredo("certo", sal, marca) is True
    assert aparelhos.confere_segredo("errado", sal, marca) is False
    assert aparelhos.confere_segredo("certo", aparelhos.gerar_sal(), marca) is False


def test_o_segredo_nunca_aparece_na_impressao():
    marca = aparelhos.impressao("meu-segredo-literal", "sal")
    assert "meu-segredo-literal" not in marca


# -- Código de pareamento ------------------------------------------------------
def test_codigo_usa_alfabeto_sem_sosias():
    for _ in range(50):
        codigo = aparelhos.gerar_codigo()
        assert len(codigo) == aparelhos.TAMANHO_CODIGO
        assert not (set(codigo) & set("01OIL"))


def test_normalizar_aceita_o_que_o_dono_digita():
    assert aparelhos.normalizar_codigo(" ab23-cd45 ") == "AB23CD45"
    assert aparelhos.normalizar_codigo(None) == ""


def test_codigo_valido_recusa_usado_expirado_e_inexistente():
    assert aparelhos.codigo_valido(AGORA, False, AGORA + timedelta(minutes=1), 10) is None
    assert aparelhos.codigo_valido(AGORA, True, AGORA, 10) == MOTIVO_CODIGO_USADO
    assert aparelhos.codigo_valido(AGORA, False, AGORA + timedelta(minutes=11), 10) == MOTIVO_CODIGO_EXPIRADO
    assert aparelhos.codigo_valido(None, False, AGORA, 10) == MOTIVO_CODIGO_INVALIDO


def test_codigo_expira_exatamente_no_limite():
    # Borda fechada: no instante do vencimento já NÃO vale.
    assert aparelhos.codigo_valido(AGORA, False, AGORA + timedelta(minutes=10), 10) == MOTIVO_CODIGO_EXPIRADO


# -- Teto ----------------------------------------------------------------------
def test_teto_de_quatro_aparelhos():
    assert aparelhos.pode_inscrever(3, 4) is True
    assert aparelhos.pode_inscrever(4, 4) is False
    assert aparelhos.pode_inscrever(9, 0) is True     # 0 = sem teto
