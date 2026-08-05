"""A ENTREGA do mensageiro: chega ao vivo se houver sessão, espera na fila se não houver.

Estes testes existem para travar a decisão de REUSO. "Avisar alguém que pode não estar
conectado" já foi resolvido uma vez neste projeto — fila durável em `agendamentos`,
reentrega no accept do WebSocket, ack de aplicação — e a tentação de escrever um segundo
mecanismo para mensagens é grande porque ele parece mais simples. Ele é, até o dia em
que alguém fecha o app antes do push: aí falta a fila, e a mensagem some sem erro.

O que se afirma aqui, então, não é "o push funciona" — é que ele funciona PELO MESMO
CAMINHO dos alarmes:
  - sem ouvinte, a linha vira 'pendente_entrega' na tabela `agendamentos`;
  - o `entregar_pendentes` do accept a reentrega, FILTRADA pelo dono da conexão;
  - o ack conclui, e a trilha não passa a mostrar alarmes que ninguém criou.

Os fakes são os mesmos de `test_scheduler_multiusuario.py` (importados de lá): duplicá-los
faria duas noções de "sessão viva" divergirem no primeiro conserto de uma.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from mente_digital import identidade, mensageiro, telemetry
from mente_digital.config import settings
from mente_digital.scheduler import SchedulerService

from test_scheduler_multiusuario import FakeCtx, FakeSession, sem_trabalho_de_fundo  # noqa: F401


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "entrega.db"))
    telemetry.db.init()
    return telemetry.db


@pytest.fixture
def multiusuario(monkeypatch):
    monkeypatch.setattr(settings, "multiusuario_habilitado", True)
    return settings


def _msg(de: str, para: str, texto: str, ident: int = 1) -> mensageiro.Mensagem:
    return mensageiro.Mensagem(id=ident, remetente=de, destinatario=para, texto=texto,
                               tipo=mensageiro.TIPO_BUG,
                               criada_em="2026-08-05T10:00:00")


def _de_tipo(sessao, tipo: str) -> list[dict]:
    return [m for m in sessao.recebidos if m.get("tipo") == tipo]


def _falado(sessao) -> str:
    return " | ".join(m.get("texto", "") for m in _de_tipo(sessao, "proativo"))


# ==========================================================================
# Ao vivo
# ==========================================================================
async def test_a_mensagem_chega_a_sessao_do_destinatario(db_tmp, multiusuario):
    """Dois pushes, não um: o card (`tipo: mensagem`) carrega id/tipo/remetente, que é o
    que a tela desenha e o que dá o botão de responder; a bolha falada é o que o front de
    HOJE já sabe exibir. Mesma dupla do alerta de segurança, e pelo mesmo motivo."""
    celular = FakeSession(identidade.MESTRE)
    sched = SchedulerService(FakeCtx([celular]))

    entregue = await sched.entregar_mensagem(_msg("ana", identidade.MESTRE, "a voz travou"))

    assert entregue is True
    assert "a voz travou" in _falado(celular)
    card = _de_tipo(celular, "mensagem")[0]
    assert card["remetente"] == "ana" and card["id"] == 1
    # E nada ficou pendente: quem ouviu não precisa de fila.
    assert db_tmp.get_agendamentos_pendentes(todos_os_donos=True) == []


async def test_o_card_diz_que_tipo_de_mensagem_e(db_tmp, multiusuario):
    """O rótulo (bug/pedido/livre) tem de SOBREVIVER ao envelope, e este teste existe
    porque ele não sobrevivia.

    A chave `tipo` do card é do ENVELOPE — é por ela que o front roteia o push, como em
    "proativo" e "energia". O tipo da MENSAGEM tinha o mesmo nome, então o `**para_json()`
    sobrescrevia o envelope e a linha seguinte o restaurava: o card chegava íntegro,
    entregue, ACKado — e sem o rótulo que o mestre usa para triar. Um relato de problema
    ficava indistinguível de um "oi", e nada falhava. Daí `classe`.
    """
    celular = FakeSession(identidade.MESTRE)
    sched = SchedulerService(FakeCtx([celular]))

    await sched.entregar_mensagem(_msg("ana", identidade.MESTRE, "a voz travou"))

    card = _de_tipo(celular, "mensagem")[0]
    assert card["tipo"] == "mensagem"            # o envelope, que o front roteia
    assert card["classe"] == mensageiro.TIPO_BUG  # o rótulo, que o mestre tria


async def test_a_mensagem_nao_chega_na_sessao_de_quem_nao_e_o_destinatario(db_tmp, multiusuario):
    """O defeito espelho do broadcast dos alarmes: conteúdo pessoal na tela de estranho.
    O que se afirma é a AUSÊNCIA — a sessão da Ana não recebeu nada."""
    mestre, ana = FakeSession(identidade.MESTRE), FakeSession("ana")
    sched = SchedulerService(FakeCtx([mestre, ana]))

    await sched.entregar_mensagem(_msg("bob", identidade.MESTRE, "segredo do bob"))

    assert "segredo do bob" in _falado(mestre)
    assert ana.recebidos == []


async def test_a_resposta_do_mestre_vai_para_o_celular_de_quem_perguntou(db_tmp, multiusuario):
    """A volta do canal. O `usar_dono(msg.destinatario)` dentro de `entregar_mensagem` é
    o que faz isto funcionar: sem ele, a resposta do mestre falaria na sessão DELE."""
    mestre, ana = FakeSession(identidade.MESTRE), FakeSession("ana")
    sched = SchedulerService(FakeCtx([mestre, ana]))

    await sched.entregar_mensagem(_msg(identidade.MESTRE, "ana", "já corrigi"))

    assert "já corrigi" in _falado(ana)
    assert mestre.recebidos == []


# ==========================================================================
# Sem ouvinte: a fila durável
# ==========================================================================
async def test_sem_sessao_a_mensagem_fica_pendente_na_fila_de_quem_vai_receber(
        db_tmp, multiusuario):
    """Ninguém conectado. A mensagem em si já está na tabela `mensagens` (quem grava é a
    rota); o que entra na fila é o AVISO — e ele tem de entrar na fila do DESTINATÁRIO,
    não na de quem enviou."""
    sched = SchedulerService(FakeCtx([]))

    entregue = await sched.entregar_mensagem(_msg("ana", identidade.MESTRE, "socorro"))

    assert entregue is False
    pendentes = db_tmp.get_agendamentos_pendentes(todos_os_donos=True)
    assert len(pendentes) == 1
    assert pendentes[0]["tipo"] == "mensagem"
    assert pendentes[0]["dono"] == identidade.MESTRE
    assert "socorro" in pendentes[0]["mensagem"]


async def test_o_pendente_e_reentregue_quando_o_mestre_conecta(db_tmp, multiusuario):
    """O caminho completo: mensagem sem ouvinte -> fila -> `entregar_pendentes` do accept.

    É este teste que prova o REUSO. Um mecanismo próprio para mensagens teria de
    reimplementar esta reentrega, e a que se esquece é sempre ela."""
    ctx = FakeCtx([])
    sched = SchedulerService(ctx)
    await sched.entregar_mensagem(_msg("ana", identidade.MESTRE, "o app não abre"))

    celular = FakeSession(identidade.MESTRE)      # o mestre abre o app agora
    ctx.sessoes.add(celular)
    await sched.entregar_pendentes(identidade.MESTRE)

    assert "o app não abre" in _falado(celular)
    # O CARD volta junto, remontado do payload gravado: reentregar só a fala deixaria a
    # mensagem sem o botão de responder — ela chegou, mas não dá para agir sobre ela.
    card = _de_tipo(celular, "mensagem")[0]
    assert card["remetente"] == "ana"
    # E com o rótulo intacto: o payload do pendente é gravado pelo MESMO `_card_mensagem`
    # do ao vivo, então perder `classe` aqui significaria que o gravado e o ao vivo
    # divergiram — a reentrega desenharia um cartão diferente do original.
    assert card["classe"] == mensageiro.TIPO_BUG


async def test_a_reentrega_do_accept_nao_entrega_o_pendente_alheio(db_tmp, multiusuario):
    """`entregar_pendentes` sai FILTRADA pelo dono da conexão. Sem isso, a Ana abrindo o
    app receberia o que estava esperando o mestre — e o vazamento não levantaria nada."""
    sched = SchedulerService(FakeCtx([]))
    await sched.entregar_mensagem(_msg("bob", identidade.MESTRE, "só pro mestre"))

    ana = FakeSession("ana")
    sched.ctx.sessoes.add(ana)
    await sched.entregar_pendentes("ana")

    assert ana.recebidos == []
    assert len(db_tmp.get_agendamentos_pendentes(todos_os_donos=True)) == 1


async def test_o_tick_tambem_reentrega_a_mensagem_pendente(db_tmp, multiusuario):
    """O outro chamador da reentrega: o laço de fundo, que roda SEM dono no contexto e
    roteia linha a linha. Se o tipo 'mensagem' não fosse conhecido do despacho, um aviso
    que ficasse 'ativo' seria CANCELADO como tipo desconhecido."""
    celular = FakeSession(identidade.MESTRE)
    sched = SchedulerService(FakeCtx([celular]))
    with identidade.usar_dono(identidade.MESTRE):
        ag_id = telemetry.db.criar_agendamento(
            "mensagem", "Relato de problema de ana: trava", datetime.now().isoformat(),
            payload='{"tipo": "mensagem", "id": 7, "remetente": "ana"}')
    assert ag_id is not None

    await sched.tick()

    assert "Relato de problema de ana: trava" in _falado(celular)


async def test_o_ack_conclui_sem_sujar_a_trilha_de_alarmes(db_tmp, multiusuario, monkeypatch):
    """Sem o ramo próprio no `confirmar_entrega`, a mensagem cairia no `else` e a
    auditoria do dono passaria a mostrar "lembrete_disparado" — alarmes que ele nunca
    criou, no meio dos que ele criou."""
    auditado: list[tuple[str, str]] = []
    monkeypatch.setattr(telemetry.db, "registrar_auditoria",
                        lambda acao, detalhe: auditado.append((acao, detalhe)))
    celular = FakeSession(identidade.MESTRE)
    sched = SchedulerService(FakeCtx([celular]))
    with identidade.usar_dono(identidade.MESTRE):
        ag_id = telemetry.db.criar_agendamento(
            "mensagem", "Mensagem de ana: oi", datetime.now().isoformat())
    await sched.tick()
    with identidade.usar_dono(identidade.MESTRE):
        await sched.confirmar_entrega(str(ag_id))

    assert [a for a, _ in auditado] == []
    # E a linha saiu da fila: um aviso concluído não volta a cada tick para sempre.
    assert db_tmp.get_agendamentos_pendentes(todos_os_donos=True) == []


# ==========================================================================
# Degradação
# ==========================================================================
async def test_sem_agendador_a_mensagem_nao_se_perde(db_tmp, multiusuario, monkeypatch):
    """Um erro na entrega não pode derrubar o envio: a mensagem já está gravada e vai
    aparecer na caixa do destinatário. O que se perde é o aviso imediato, e isso é dito
    no log — nunca engolido."""
    celular = FakeSession(identidade.MESTRE)
    sched = SchedulerService(FakeCtx([celular]))

    async def _explode(*_a, **_k):
        raise RuntimeError("socket morreu")

    monkeypatch.setattr(sched, "_empurrar_card_mensagem", _explode)

    assert await sched.entregar_mensagem(_msg("ana", identidade.MESTRE, "oi")) is False
