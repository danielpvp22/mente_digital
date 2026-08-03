"""
O registro dos aparelhos (registro_aparelhos.py) — persistência, castigo e revogação.

SQLite de verdade, mas em `tmp_path`: o que se quer provar aqui é justamente que o
estado SOBREVIVE (código de uso único, revogação, teto), e um fake de banco provaria
só que o fake funciona. Sem rede, sem GPU. O relógio é injetado.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mente_digital import aparelhos as regras
from mente_digital.registro_aparelhos import RegistroAparelhos

AGORA = datetime(2026, 8, 3, 12, 0, 0)
TETO = 4
VALIDADE = 10
EXPIRA_DIAS = 90


class RelogioFalso:
    def __init__(self, inicio: datetime = AGORA) -> None:
        self.agora = inicio

    def __call__(self) -> datetime:
        return self.agora

    def avancar(self, **kw) -> None:
        self.agora += timedelta(**kw)


class DbEspiao:
    """A trilha de auditoria, sem tocar no SQLite real. O conftest já redireciona o
    banco global, mas depender disso acoplaria este teste à ordem de import."""

    def __init__(self) -> None:
        self.linhas: list[tuple[str, str]] = []

    def registrar_auditoria(self, acao: str, detalhe: str) -> None:
        self.linhas.append((acao, detalhe))

    def acoes(self) -> list[str]:
        return [a for a, _ in self.linhas]


@pytest.fixture
def registro(tmp_path):
    relogio = RelogioFalso()
    espiao = DbEspiao()
    r = RegistroAparelhos(str(tmp_path / "teste.db"), relogio=relogio, db=espiao)
    r.init()
    r.relogio = relogio      # atalho para o teste mexer no tempo
    r.espiao = espiao
    return r


def _parear(registro, apelido="celular do dono"):
    codigo = registro.emitir_codigo(apelido, TETO)
    assert codigo is not None
    return registro.parear(codigo, "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS)


def _autorizar(registro, credencial, host="203.0.113.9", rota="/api/nota"):
    return registro.autorizar(credencial, host, rota, habilitado=True,
                              token_legado="", aceita_token_legado=True)


# -- Inscrição -----------------------------------------------------------------
def test_init_e_idempotente(registro):
    registro.init()          # 2ª vez não pode explodir nem apagar nada
    assert registro.listar() == []


def test_o_ciclo_completo_parear_e_entrar(registro):
    r = _parear(registro)
    assert r.ok is True and r.credencial is not None
    assert _autorizar(registro, r.credencial).autorizado is True


def test_o_segredo_nunca_e_gravado_no_banco(registro):
    r = _parear(registro)
    _id, segredo = regras.partir_credencial(r.credencial)
    guardado = registro.buscar(r.aparelho_id)
    assert segredo not in guardado.impressao
    assert segredo not in (guardado.sal or "")


def test_codigo_e_de_uso_unico(registro):
    codigo = registro.emitir_codigo("primeiro", TETO)
    assert registro.parear(codigo, "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS).ok is True
    segunda = registro.parear(codigo, "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS)
    assert segunda.ok is False and segunda.motivo == regras.MOTIVO_CODIGO_USADO


def test_codigo_expira(registro):
    codigo = registro.emitir_codigo("atrasado", TETO)
    registro.relogio.avancar(minutes=VALIDADE + 1)
    r = registro.parear(codigo, "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS)
    assert r.ok is False and r.motivo == regras.MOTIVO_CODIGO_EXPIRADO


def test_codigo_inventado_nao_pareia(registro):
    r = registro.parear("ZZZZZZZZZZ", "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS)
    assert r.ok is False and r.motivo == regras.MOTIVO_CODIGO_INVALIDO


def test_aparelho_novo_nunca_entra_sem_ato_do_dono(registro):
    # Não existe caminho que crie aparelho sem um código emitido na máquina.
    assert registro.listar() == []
    assert _autorizar(registro, regras.montar_credencial("a" * 16, "x" * 40)).autorizado is False
    assert registro.listar() == []


def test_teto_de_quatro_aparelhos(registro):
    for i in range(TETO):
        assert _parear(registro, f"celular {i}").ok is True
    assert registro.emitir_codigo("o quinto", TETO) is None


def test_revogar_abre_vaga_para_o_quinto(registro):
    primeiros = [_parear(registro, f"celular {i}") for i in range(TETO)]
    assert registro.emitir_codigo("o quinto", TETO) is None
    registro.revogar(primeiros[0].aparelho_id)
    assert registro.emitir_codigo("o quinto", TETO) is not None


def test_teto_e_reconferido_no_pareamento(registro):
    # Dois códigos emitidos com 3 aparelhos; o 2º tem de bater no teto na hora de USAR.
    for i in range(TETO - 1):
        _parear(registro, f"celular {i}")
    codigo_a = registro.emitir_codigo("quarto", TETO)
    codigo_b = registro.emitir_codigo("quinto", TETO)
    assert registro.parear(codigo_a, "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS).ok is True
    segundo = registro.parear(codigo_b, "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS)
    assert segundo.ok is False and segundo.motivo == regras.MOTIVO_TETO


# -- Revogação -----------------------------------------------------------------
def test_revogar_fecha_a_porta_na_hora(registro):
    r = _parear(registro)
    assert _autorizar(registro, r.credencial).autorizado is True
    assert registro.revogar(r.aparelho_id) is True
    veredito = _autorizar(registro, r.credencial)
    assert veredito.autorizado is False and veredito.motivo == regras.MOTIVO_REVOGADO


def test_revogar_derruba_o_websocket_ja_aberto(registro):
    # O ponto do pedido: sem isto, "revogar" só valeria na próxima conexão e a sessão
    # aberta do celular roubado seguiria conversando.
    r = _parear(registro)
    derrubado = []
    sid = registro.registrar_sessao(r.aparelho_id, lambda: derrubado.append(True))
    assert sid is not None and registro.sessoes_vivas(r.aparelho_id) == 1
    registro.revogar(r.aparelho_id)
    assert derrubado == [True]
    assert registro.sessoes_vivas(r.aparelho_id) == 0


def test_revogar_derruba_todas_as_sessoes_do_aparelho(registro):
    r = _parear(registro)
    caidas = []
    for i in range(3):
        registro.registrar_sessao(r.aparelho_id, lambda i=i: caidas.append(i))
    registro.revogar(r.aparelho_id)
    assert sorted(caidas) == [0, 1, 2]


def test_um_derrubador_que_explode_nao_salva_os_outros(registro):
    # Revogação parcial é pior que nenhuma: o dono acreditaria ter fechado a porta.
    r = _parear(registro)
    caidas = []

    def explode():
        raise RuntimeError("socket já morto")

    registro.registrar_sessao(r.aparelho_id, explode)
    registro.registrar_sessao(r.aparelho_id, lambda: caidas.append("ok"))
    registro.revogar(r.aparelho_id)
    assert caidas == ["ok"]


def test_revogar_nao_derruba_a_sessao_dos_outros_aparelhos(registro):
    a = _parear(registro, "celular A")
    b = _parear(registro, "celular B")
    caidas = []
    registro.registrar_sessao(a.aparelho_id, lambda: caidas.append("A"))
    registro.registrar_sessao(b.aparelho_id, lambda: caidas.append("B"))
    registro.revogar(a.aparelho_id)
    assert caidas == ["A"]
    assert _autorizar(registro, b.credencial).autorizado is True


def test_encerrar_sessao_tira_o_derrubador(registro):
    r = _parear(registro)
    caidas = []
    sid = registro.registrar_sessao(r.aparelho_id, lambda: caidas.append(1))
    registro.encerrar_sessao(r.aparelho_id, sid)
    registro.revogar(r.aparelho_id)
    assert caidas == []      # sessão já fechada não é "derrubada" de novo


def test_sessao_sem_aparelho_nao_e_registrada(registro):
    # Loopback/token legado não têm identidade — guardar seria vazamento de memória.
    assert registro.registrar_sessao(None, lambda: None) is None


def test_revogar_duas_vezes_nao_mente(registro):
    r = _parear(registro)
    assert registro.revogar(r.aparelho_id) is True
    assert registro.revogar(r.aparelho_id) is False


def test_revogado_some_da_lista_mas_nao_do_historico(registro):
    r = _parear(registro)
    registro.revogar(r.aparelho_id)
    assert registro.listar() == []
    assert len(registro.listar(incluir_revogados=True)) == 1


# -- Bloqueio progressivo ------------------------------------------------------
def test_falhas_seguidas_bloqueiam_o_ip(registro):
    r = _parear(registro)
    falsa = regras.montar_credencial(r.aparelho_id, "segredo-que-nao-e-o-certo")
    for _ in range(3):
        _autorizar(registro, falsa)
    # Com o castigo em vigor, nem a credencial CERTA passa.
    veredito = _autorizar(registro, r.credencial)
    assert veredito.autorizado is False and veredito.motivo == regras.MOTIVO_BLOQUEADO


def test_o_castigo_passa_com_o_tempo(registro):
    r = _parear(registro)
    falsa = regras.montar_credencial(r.aparelho_id, "segredo-que-nao-e-o-certo")
    for _ in range(3):
        _autorizar(registro, falsa)
    registro.relogio.avancar(seconds=5)
    assert _autorizar(registro, r.credencial).autorizado is True


def test_acerto_zera_o_contador(registro):
    r = _parear(registro)
    falsa = regras.montar_credencial(r.aparelho_id, "segredo-que-nao-e-o-certo")
    _autorizar(registro, falsa)
    assert _autorizar(registro, r.credencial).autorizado is True
    # Depois do acerto, as 2 falhas livres valem de novo.
    _autorizar(registro, falsa)
    assert _autorizar(registro, r.credencial).autorizado is True


def test_o_castigo_e_por_ip_nao_global(registro):
    # Senão um atacante trancaria os outros três celulares do dono de graça (DoS).
    r = _parear(registro)
    falsa = regras.montar_credencial(r.aparelho_id, "segredo-que-nao-e-o-certo")
    for _ in range(5):
        _autorizar(registro, falsa, host="198.51.100.7")
    assert _autorizar(registro, r.credencial, host="203.0.113.9").autorizado is True


def test_requisicao_sem_credencial_nao_conta_falha(registro):
    # A SPA antes de configurar o token bate sem nada; isso não pode bloquear o dono.
    r = _parear(registro)
    for _ in range(10):
        _autorizar(registro, None)
    assert _autorizar(registro, r.credencial).autorizado is True


def test_o_castigo_tambem_cobre_o_pareamento(registro):
    for _ in range(3):
        registro.parear("ZZZZZZZZZZ", "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS)
    codigo = registro.emitir_codigo("legítimo", TETO)
    r = registro.parear(codigo, "203.0.113.9", TETO, VALIDADE, EXPIRA_DIAS)
    assert r.ok is False and r.motivo == regras.MOTIVO_BLOQUEADO


# -- Trilha e carimbo ----------------------------------------------------------
def test_pareamento_e_revogacao_entram_na_auditoria(registro):
    r = _parear(registro)
    registro.revogar(r.aparelho_id)
    assert "aparelho_pareado" in registro.espiao.acoes()
    assert "aparelho_revogado" in registro.espiao.acoes()


def test_recusa_entra_na_auditoria_com_ip_e_rota(registro):
    _autorizar(registro, regras.montar_credencial("f" * 16, "x" * 40), rota="/api/nota")
    linha = [d for a, d in registro.espiao.linhas if a == "acesso_recusado"]
    assert len(linha) == 1
    assert "198" not in linha[0] and "203.0.113.9" in linha[0] and "/api/nota" in linha[0]


def test_flood_de_recusa_nao_vira_flood_de_insert(registro):
    # O atacante escolhe quantos 401 pede; ele não pode escolher quantas linhas eu escrevo.
    falsa = regras.montar_credencial("f" * 16, "x" * 40)
    for _ in range(50):
        _autorizar(registro, falsa)
    assert len(registro.espiao.acoes()) < 5


def test_acesso_bem_sucedido_e_registrado_uma_vez_por_janela(registro):
    r = _parear(registro)
    for _ in range(20):
        _autorizar(registro, r.credencial, rota="/api/historico")
    assert registro.espiao.acoes().count("acesso_aparelho") == 1


def test_janela_vencida_registra_de_novo(registro):
    r = _parear(registro)
    _autorizar(registro, r.credencial, rota="/api/historico")
    registro.relogio.avancar(seconds=400)
    _autorizar(registro, r.credencial, rota="/api/historico")
    assert registro.espiao.acoes().count("acesso_aparelho") == 2


def test_ultimo_uso_e_ip_ficam_na_linha_do_aparelho(registro):
    r = _parear(registro)
    _autorizar(registro, r.credencial, host="198.51.100.7")
    guardado = registro.buscar(r.aparelho_id)
    assert guardado.ultimo_ip == "198.51.100.7"
    assert guardado.ultimo_uso is not None


def test_o_painel_ve_apelido_e_data_de_cada_aparelho(registro):
    _parear(registro, "celular do dono")
    _parear(registro, "tablet da cozinha")
    apelidos = [a.apelido for a in registro.listar()]
    assert apelidos == ["celular do dono", "tablet da cozinha"]
    assert all(a.criado_em for a in registro.listar())


# -- Compatibilidade -----------------------------------------------------------
def test_desligado_o_registro_nao_muda_nada(registro):
    # Loopback entra, LAN não — o gate de hoje, com a função nova instalada e off.
    dentro = registro.autorizar(None, "127.0.0.1", "/api/nota", habilitado=False,
                                token_legado="", aceita_token_legado=True)
    fora = registro.autorizar(None, "192.168.0.42", "/api/nota", habilitado=False,
                              token_legado="", aceita_token_legado=True)
    assert dentro.autorizado is True and fora.autorizado is False


def test_token_legado_continua_valendo_com_a_funcao_ligada(registro):
    v = registro.autorizar("s3gr3do", "192.168.0.42", "/api/nota", habilitado=True,
                           token_legado="s3gr3do", aceita_token_legado=True)
    assert v.autorizado is True


def test_token_legado_desligado_fecha_a_porta_do_segredo_unico(registro):
    v = registro.autorizar("s3gr3do", "192.168.0.42", "/api/nota", habilitado=True,
                           token_legado="s3gr3do", aceita_token_legado=False)
    assert v.autorizado is False


def test_expiracao_de_credencial_e_persistida(registro):
    r = registro.parear(registro.emitir_codigo("efêmero", TETO), "203.0.113.9",
                        TETO, VALIDADE, expira_dias=1)
    assert _autorizar(registro, r.credencial).autorizado is True
    registro.relogio.avancar(days=2)
    veredito = _autorizar(registro, r.credencial)
    assert veredito.autorizado is False and veredito.motivo == regras.MOTIVO_EXPIRADO


def test_expira_dias_zero_nao_expira_nunca(registro):
    r = registro.parear(registro.emitir_codigo("eterno", TETO), "203.0.113.9",
                        TETO, VALIDADE, expira_dias=0)
    registro.relogio.avancar(days=3650)
    assert _autorizar(registro, r.credencial).autorizado is True


def test_o_estado_sobrevive_a_reabertura_do_banco(tmp_path):
    # Revogação e teto não podem morar em RAM: reiniciar o servidor devolveria acesso
    # ao aparelho revogado.
    caminho = str(tmp_path / "persistente.db")
    primeiro = RegistroAparelhos(caminho, relogio=RelogioFalso(), db=DbEspiao())
    primeiro.init()
    r = primeiro.parear(primeiro.emitir_codigo("celular", TETO), "203.0.113.9",
                        TETO, VALIDADE, EXPIRA_DIAS)
    primeiro.revogar(r.aparelho_id)

    segundo = RegistroAparelhos(caminho, relogio=RelogioFalso(), db=DbEspiao())
    segundo.init()
    veredito = segundo.autorizar(r.credencial, "203.0.113.9", "/api/nota",
                                 habilitado=True, token_legado="", aceita_token_legado=True)
    assert veredito.autorizado is False and veredito.motivo == regras.MOTIVO_REVOGADO
