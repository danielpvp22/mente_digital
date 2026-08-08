"""O canal usuário -> MESTRE: a regra de quem lê o quê, e a caixa que sobrevive ao restart.

O que este arquivo trava, em ordem de gravidade:

  (a) O MESTRE NÃO LÊ A CONVERSA ALHEIA. `identidade.py` promete que ele administra sem
      ler a memória dos outros, e um canal de mensagens é exatamente onde essa promessa
      costuma se perder — basta alguém achar que "o admin precisa ver tudo para dar
      suporte". A diferença só aparece no caso A->B, que ninguém exercita à mão, então
      ela precisa de teste. Sem ele, a regressão passa com a suíte 100% verde.

  (b) UM USUÁRIO NÃO LÊ A MENSAGEM DO OUTRO. Metade do que se afirma aqui é uma
      AUSÊNCIA (o que a caixa NÃO trouxe), como em `test_rag_multiusuario.py` — vazamento
      não levanta exceção nem aparece no log.

  (c) A CAIXA SOBREVIVE. Um relato de bug que morre no restart é pior que não ter canal:
      a pessoa acredita ter avisado.

Os testes de identidade rodam com `multiusuario_habilitado=True`, que é como o dono vai
usar isto. O último bloco cobre o contrato do interruptor DESLIGADO — hoje o default —,
onde há um usuário só e tudo é dele.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mente_digital import identidade, mensageiro, telemetry
from mente_digital.config import settings


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    """O singleton `db` apontado para um SQLite temporário, com o schema criado."""
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "mensageiro.db"))
    telemetry.db.init()
    return telemetry.db


@pytest.fixture
def multiusuario(monkeypatch):
    monkeypatch.setattr(settings, "multiusuario_habilitado", True)
    return settings


def _mandar(de: str, para: str, texto: str, tipo: str = mensageiro.TIPO_LIVRE,
            quando: datetime | None = None) -> int:
    """Grava uma mensagem COMO `de` — o `usar_dono` importa porque é ele que decide de
    quem é a caixa nas leituras seguintes."""
    with identidade.usar_dono(de):
        msg_id = telemetry.db.salvar_mensagem(
            de, para, texto, tipo, mensageiro.agora_iso(quando))
    assert msg_id is not None
    return msg_id


def _textos(linhas) -> str:
    return " | ".join(x["texto"] for x in linhas)


# ==========================================================================
# (a) A regra pura — o mestre administra, não lê
# ==========================================================================
def _msg(de: str, para: str, ident: int = 1, lida_em=None) -> mensageiro.Mensagem:
    return mensageiro.Mensagem(id=ident, remetente=de, destinatario=para,
                               texto="oi", criada_em="2026-08-05T10:00:00",
                               lida_em=lida_em)


def test_o_mestre_nao_le_a_conversa_entre_outros_dois():
    """O TESTE que sustenta a promessa de `identidade.py`.

    Um `if leitor == MESTRE: return True` no `pode_ler` passaria em todos os outros
    testes deste arquivo — o mestre é destinatário de quase tudo. Só este caso, uma
    mensagem em que ele não está em nenhuma das duas pontas, separa "recebe tudo porque
    todos escrevem para ele" de "pode ler tudo"."""
    entre_outros = _msg("ana", "bob")
    v = mensageiro.pode_ler(identidade.MESTRE, entre_outros)
    assert v.permitido is False
    assert v.motivo == mensageiro.MOTIVO_ALHEIA


def test_leio_o_que_escrevi_e_o_que_me_mandaram():
    minha = _msg("ana", identidade.MESTRE)
    assert mensageiro.pode_ler("ana", minha).motivo == mensageiro.MOTIVO_REMETENTE
    assert (mensageiro.pode_ler(identidade.MESTRE, minha).motivo
            == mensageiro.MOTIVO_DESTINATARIO)
    assert mensageiro.pode_ler("bob", minha).permitido is False


def test_sem_leitor_definido_nao_e_pode_tudo():
    """Mesmo default de `identidade.exigir_dono`: quem esqueceu de marcar quem está
    falando não herda a caixa de ninguém. Um `if not leitor` que devolvesse True aqui
    abriria toda a caixa para qualquer caminho que perdesse o ContextVar."""
    assert mensageiro.pode_ler(None, _msg("ana", "daniel")).permitido is False
    assert mensageiro.pode_ler("", _msg("ana", "daniel")).permitido is False


def test_so_o_destinatario_marca_como_lida():
    """Se o remetente pudesse marcar, ele apagaria o não-lido da caixa do mestre sem
    que o mestre tivesse aberto nada — e o contador viraria mentira."""
    m = _msg("ana", identidade.MESTRE)
    assert mensageiro.pode_marcar_lida(identidade.MESTRE, m).permitido is True
    assert mensageiro.pode_marcar_lida("ana", m).permitido is False


def test_visiveis_filtra_a_alheia_do_meio_do_lote():
    """`visiveis` é a SEGUNDA guarda que o `main.py` aplica sobre o que veio do banco.
    Aqui ela recebe de propósito um lote em que a SQL falhou (a linha A->B não deveria
    ter vindo) — é esse o cenário que ela existe para cobrir."""
    lote = [_msg("ana", identidade.MESTRE, 1), _msg("bob", "ana", 2),
            _msg(identidade.MESTRE, "ana", 3)]
    vistos = mensageiro.visiveis(identidade.MESTRE, lote)
    assert [m.id for m in vistos] == [1, 3]


def test_nao_lidas_ignora_o_que_eu_mesmo_escrevi():
    """Uma mensagem própria nunca está "não lida" para quem a escreveu — contá-la faria
    o crachá da caixa acusar trabalho que não existe."""
    lote = [_msg("ana", "daniel", 1),                       # chegou, não lida
            _msg("daniel", "ana", 2),                       # eu escrevi
            _msg("ana", "daniel", 3, lida_em="2026-08-05T11:00:00")]
    assert [m.id for m in mensageiro.nao_lidas("daniel", lote)] == [1]


# ==========================================================================
# Validação da entrada
# ==========================================================================
def test_tipo_desconhecido_levanta_em_vez_de_virar_livre():
    """Deixar `tipo="urgente"` cair calado em "livre" criaria uma quarta gaveta que
    ninguém está triando — o cliente acreditaria ter marcado urgência."""
    assert mensageiro.normalizar_tipo(" BUG ") == mensageiro.TIPO_BUG
    assert mensageiro.normalizar_tipo(None) == mensageiro.TIPO_LIVRE
    with pytest.raises(ValueError):
        mensageiro.normalizar_tipo("urgente")


def test_texto_vazio_levanta_mas_texto_longo_e_cortado():
    """Assimetria de propósito: vazio é clique sem conteúdo; longo demais ainda diz o
    essencial, e recusá-lo faria a pessoa perder o que escreveu."""
    with pytest.raises(ValueError):
        mensageiro.limpar_texto("   ")
    longo = mensageiro.limpar_texto("x" * (mensageiro.TEXTO_MAX + 500))
    assert len(longo) == mensageiro.TEXTO_MAX


def test_a_fala_diz_quem_mandou_antes_do_conteudo():
    """O mestre recebe de cinco pessoas: um aviso falado que começa pelo texto obriga a
    esperar o fim da frase para saber de quem é."""
    falado = mensageiro.falar(_bug("ana", "o botão de voz não abre"))
    assert falado.startswith("Relato de problema de ana:")
    assert "o botão de voz não abre" in falado


def test_a_fala_corta_o_relato_longo_e_avisa_que_cortou():
    """4.000 caracteres lidos em voz alta são piores que não avisar — o mestre desliga
    no meio. A bolha escrita leva o texto inteiro; a fala leva o começo e um '...'."""
    falado = mensageiro.falar(_bug("ana", "y" * 1000))
    assert len(falado) < 400 and falado.endswith("...")


def _bug(de: str, texto: str) -> mensageiro.Mensagem:
    return mensageiro.Mensagem(id=1, remetente=de, destinatario=identidade.MESTRE,
                               texto=texto, tipo=mensageiro.TIPO_BUG,
                               criada_em="2026-08-05T10:00:00")


def test_responder_deriva_as_pontas_da_original():
    """As pontas saem da mensagem, nunca de um `para` no corpo: um id trocado passaria a
    responder para a pessoa errada, e um `para` livre reabriria o canal usuário->usuário
    que o desenho fechou."""
    original = _bug("ana", "não consigo entrar")
    remetente, destinatario, texto = mensageiro.responder(original, "  já liberei  ")
    assert (remetente, destinatario, texto) == (identidade.MESTRE, "ana", "já liberei")


# ==========================================================================
# (b) e (c) A caixa no SQLite — isolamento e persistência
# ==========================================================================
def test_a_caixa_sobrevive_a_reabrir_o_banco(db_tmp, multiusuario):
    """(c) Um relato de bug que morre no restart é pior que não ter canal: a pessoa
    acredita ter avisado. O `init()` de novo simula o boot seguinte — e o schema é
    idempotente, então rodá-lo duas vezes não pode apagar nada."""
    _mandar("ana", identidade.MESTRE, "a voz travou", mensageiro.TIPO_BUG)

    db_tmp.init()   # como um restart: mesmo arquivo, migração idempotente por cima

    with identidade.usar_dono(identidade.MESTRE):
        caixa = db_tmp.listar_mensagens()
    assert _textos(caixa) == "a voz travou"
    assert caixa[0]["tipo"] == mensageiro.TIPO_BUG


def test_um_usuario_nao_le_a_mensagem_do_outro(db_tmp, multiusuario):
    """(b) O vazamento silencioso. A Ana e o Bob mandam para o mestre; nenhum dos dois
    pode ver o do outro. Repare que o que se afirma é uma AUSÊNCIA."""
    _mandar("ana", identidade.MESTRE, "segredo da ana")
    _mandar("bob", identidade.MESTRE, "segredo do bob")

    with identidade.usar_dono("ana"):
        caixa_ana = db_tmp.listar_mensagens()
    assert _textos(caixa_ana) == "segredo da ana"
    assert "bob" not in _textos(caixa_ana)


def test_o_mestre_recebe_de_todos_mas_a_sql_tambem_recusa_a_alheia(db_tmp, multiusuario):
    """O espelho do teste puro (a), agora contra o banco: a caixa do mestre traz os dois
    que escreveram PARA ele e NÃO traz a linha entre Ana e Bob.

    A linha A->B não nasce de nenhuma rota — o `destinatario` é sempre o mestre —, mas é
    gravada aqui de propósito: a promessa tem de valer para a linha que existir no
    banco, não só para a que as rotas de hoje sabem criar."""
    _mandar("ana", identidade.MESTRE, "da ana pro mestre")
    _mandar("bob", identidade.MESTRE, "do bob pro mestre")
    _mandar("ana", "bob", "entre ana e bob")

    with identidade.usar_dono(identidade.MESTRE):
        caixa = db_tmp.listar_mensagens()
    textos = _textos(caixa)
    assert "da ana pro mestre" in textos and "do bob pro mestre" in textos
    assert "entre ana e bob" not in textos


def test_id_chutado_nao_abre_a_mensagem_alheia(db_tmp, multiusuario):
    """`msg_id` é um número que trafega pelo cliente. Sem o filtro das duas pontas na
    própria SQL, bastava chutá-lo — o mesmo furo que o `AND dono` do
    `cancelar_agendamento` fechou nos alarmes."""
    alheia = _mandar("ana", identidade.MESTRE, "segredo da ana")

    with identidade.usar_dono("bob"):
        assert db_tmp.get_mensagem(alheia) is None
        assert db_tmp.marcar_mensagem_lida(alheia, mensageiro.agora_iso()) is False


def test_nao_lida_vira_lida_e_a_segunda_vez_nao_muda_nada(db_tmp, multiusuario):
    """O `False` da segunda chamada significa "nada mudou", não "falhou" — é o que
    deixa a rota ser idempotente sem inventar erro para um clique repetido."""
    msg_id = _mandar("ana", identidade.MESTRE, "olha isto")

    with identidade.usar_dono(identidade.MESTRE):
        assert db_tmp.get_mensagem(msg_id)["lida_em"] is None
        assert db_tmp.marcar_mensagem_lida(msg_id, "2026-08-05T12:00:00") is True
        assert db_tmp.marcar_mensagem_lida(msg_id, "2026-08-05T13:00:00") is False
        # E ficou o carimbo da PRIMEIRA leitura, não o da última tentativa.
        assert db_tmp.get_mensagem(msg_id)["lida_em"] == "2026-08-05T12:00:00"


def test_quem_escreveu_nao_marca_a_propria_como_lida(db_tmp, multiusuario):
    """O espelho no banco de `test_so_o_destinatario_marca_como_lida`: a cláusula é
    `AND destinatario`, e não a das duas pontas."""
    msg_id = _mandar("ana", identidade.MESTRE, "olha isto")

    with identidade.usar_dono("ana"):
        assert db_tmp.marcar_mensagem_lida(msg_id, mensageiro.agora_iso()) is False
    with identidade.usar_dono(identidade.MESTRE):
        assert db_tmp.get_mensagem(msg_id)["lida_em"] is None


def test_apenas_nao_lidas_nao_conta_o_que_eu_mandei(db_tmp, multiusuario):
    """O espelho no banco de `test_nao_lidas_ignora_o_que_eu_mesmo_escrevi`: sem o
    `AND destinatario = ?` no filtro, a resposta que o mestre acabou de escrever
    voltaria como "não lida" na caixa dele."""
    _mandar("ana", identidade.MESTRE, "chegou pra mim")
    _mandar(identidade.MESTRE, "ana", "eu respondi")

    with identidade.usar_dono(identidade.MESTRE):
        assert _textos(db_tmp.listar_mensagens(apenas_nao_lidas=True)) == "chegou pra mim"


def test_a_caixa_vem_da_mais_nova_para_a_mais_velha(db_tmp, multiusuario):
    base = datetime(2026, 8, 5, 9, 0)
    _mandar("ana", identidade.MESTRE, "primeira", quando=base)
    _mandar("ana", identidade.MESTRE, "segunda", quando=base + timedelta(minutes=5))

    with identidade.usar_dono(identidade.MESTRE):
        assert _textos(db_tmp.listar_mensagens()) == "segunda | primeira"


def test_o_filtro_por_interlocutor_isola_a_conversa(db_tmp, multiusuario):
    """`de=` monta a conversa com UMA pessoa. Ele ESTREITA o filtro das duas pontas —
    nunca o substitui: pedir `de='ana'` como Bob não pode trazer o que a Ana escreveu
    para o mestre."""
    _mandar("ana", identidade.MESTRE, "da ana")
    _mandar("bob", identidade.MESTRE, "do bob")

    with identidade.usar_dono(identidade.MESTRE):
        assert _textos(db_tmp.listar_mensagens(de="ana")) == "da ana"
    with identidade.usar_dono("bob"):
        assert db_tmp.listar_mensagens(de="ana") == []


# ==========================================================================
# O interruptor DESLIGADO — o default de hoje
# ==========================================================================
def test_desligado_a_maquina_tem_um_usuario_so(db_tmp):
    """Sem `multiusuario_habilitado` não há de quem separar: quem passa no gate é o dono
    padrão, e a caixa dele é a caixa. Este é o comportamento que sobe na máquina do dono
    hoje — e ele não pode depender de ninguém ter marcado identidade em lugar nenhum."""
    assert settings.multiusuario_habilitado is False
    msg_id = telemetry.db.salvar_mensagem(
        identidade.DONO_PADRAO, identidade.MESTRE, "nota pra mim",
        mensageiro.TIPO_LIVRE, mensageiro.agora_iso())

    caixa = db_tmp.listar_mensagens()        # sem `usar_dono` nenhum
    assert _textos(caixa) == "nota pra mim"
    assert db_tmp.get_mensagem(msg_id) is not None
