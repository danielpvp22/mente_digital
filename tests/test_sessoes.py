"""
Isolamento entre conexões (LiveSession.memory).

O bug que isto trava: o `SessionMemory` era ÚNICO no AppContext e toda conexão
WebSocket recebia o mesmo objeto. O cliente gera um `conversaId` por aba e o reenvia
em `set_conversa` a CADA `onopen` — e ele reconecta sozinho, com backoff. Então
bastava o celular estar aberto (ou o WS piscar) para o último a conectar sobrescrever
o `conversa_id` de todos. Consequências, em ordem de gravidade:

1. `db.save_chat(..., memory.conversa_id)` gravava o turno da conversa A DENTRO da
   conversa B — corrupção PERSISTENTE no SQLite, não só de RAM.
2. `chat_history` global vazava o contexto de uma conversa no prompt da outra.
3. `nova_conversa` de uma aba dava `chat_history.clear()` GLOBAL: o follow-up da
   outra aba chegava ao LLM sem antecedente e virava sentinela.

Sem WebSocket real: a memória é o objeto que a LiveSession possui, então testá-la
diretamente cobre o mecanismo.
"""
from config import settings
from state import AppContext, SessionMemory


def test_appcontext_nao_tem_mais_memoria_global():
    # A garantia estrutural: não existe onde guardar estado de conversa compartilhado.
    # Se alguém readicionar `memory` ao AppContext, este teste cai.
    ctx = AppContext(settings=settings)
    assert not hasattr(ctx, "memory")


def test_conversa_id_de_uma_sessao_nao_afeta_a_outra():
    # O caminho exato do bug: duas abas, cada uma com seu id, `set_conversa` em cada.
    a, b = SessionMemory(settings), SessionMemory(settings)
    a.conversa_id = "conversa-A"
    b.conversa_id = "conversa-B"          # a aba B conecta depois (ou A reconecta)

    # Antes: as duas apontavam para o mesmo objeto -> B vencia e o turno de A era
    # gravado no SQLite dentro de B.
    assert a.conversa_id == "conversa-A"
    assert b.conversa_id == "conversa-B"


def test_historico_nao_vaza_entre_sessoes():
    a, b = SessionMemory(settings), SessionMemory(settings)
    a.registrar_turno("o que é TensorRT?", "acelera inferência")
    b.registrar_turno("como faço bolo?", "bata os ovos")

    assert len(a.chat_history) == 1 and len(b.chat_history) == 1
    # o prompt de A jamais pode enxergar o antecedente de B (era o vazamento de contexto)
    assert "bolo" not in str(list(a.chat_history))


def test_nova_conversa_de_uma_aba_nao_apaga_o_contexto_da_outra():
    a, b = SessionMemory(settings), SessionMemory(settings)
    a.registrar_turno("pergunta A", "resposta A")
    b.registrar_turno("pergunta B", "resposta B")

    a.nova_conversa("novo-id")            # "Novo chat" na aba A

    assert len(a.chat_history) == 0       # só A foi zerada...
    assert len(b.chat_history) == 1       # ...e B seguiu com o contexto dela


def test_fila_etl_e_por_sessao():
    # Cada sessão consolida o que ELA pesquisou: o `_finalizar_sessao` de uma aba
    # não pode drenar (nem levar embora) a fila da outra.
    a, b = SessionMemory(settings), SessionMemory(settings)
    a.enfileirar_etl("tensorrt", "dados")
    b.enfileirar_etl("yolo", "dados")

    drenados = a.drenar_etl()

    assert [t for t, _ in drenados] == ["tensorrt"]
    assert len(b.fila_etl) == 1           # a fila de B ficou intacta


def test_sessoes_vivas_ficam_registradas_no_contexto():
    # O /api/metrics agrega as sessões vivas em vez de ler um estado global.
    ctx = AppContext(settings=settings)
    assert ctx.sessoes == set()

    class SessaoFalsa:
        pass

    s = SessaoFalsa()
    ctx.sessoes.add(s)
    assert len(ctx.sessoes) == 1
    ctx.sessoes.discard(s)
    assert ctx.sessoes == set()           # o finally do run() desregistra
