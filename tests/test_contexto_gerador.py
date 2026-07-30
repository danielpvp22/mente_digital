"""
Histórico no prompt do GERADOR — só em pergunta que PRECISA do antecedente.

Por que existe (teste real do dono, 2026-07-29): o `_pergunta_com_contexto` prefixava
os 2 turnos recentes em TODA pergunta. "O que o livro diz sobre cultivo de tomate?" é
auto-contida, levou o turno de floração no prompt e a resposta gravada saiu com "o
tempo de colheita após a floração é de 6 a 9 semanas" DENTRO de uma resposta sobre
tomate (turno #356 do `chat_history`). Naquelas 40 perguntas, 1 precisava do
antecedente e 40 recebiam.

O portão NÃO pode ser só `referencia_contexto`: "explique melhor" não tem pronome
nenhum e é exatamente o caso para o qual o histórico foi introduzido. Daí o núcleo
próprio (`precisa_antecedente`), medido em lote: 15/15 continuações preservadas.
"""
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.otimizador import precisa_antecedente
from mente_digital.state import AppContext, SessionMemory

PERGUNTA_ANTERIOR = "Quantas semanas de floração até a colheita?"
RESPOSTA_ANTERIOR = (
    "Para variedades puras de indica cultivadas em ambientes fechados, o tempo de "
    "colheita após a indução da floração é de 6 a 9 semanas."
)


def _agent_com_historico():
    agent = Agent(AppContext(settings=settings))
    mem = SessionMemory(settings)
    mem.registrar_turno(PERGUNTA_ANTERIOR, RESPOSTA_ANTERIOR)
    return agent, mem


# --- o wiring no gerador ------------------------------------------------------
def test_pergunta_autocontida_nao_leva_historico():
    # O caso REAL da alucinação: a pergunta tem núcleo próprio (livro, cultivo, tomate).
    agent, mem = _agent_com_historico()
    saida = agent._pergunta_com_contexto("O que o livro diz sobre cultivo de tomate?", mem)
    assert saida == "O que o livro diz sobre cultivo de tomate?"
    assert "6 a 9 semanas" not in saida


def test_follow_up_com_pronome_leva_historico():
    agent, mem = _agent_com_historico()
    saida = agent._pergunta_com_contexto("e sobre isso?", mem)
    assert "[CONVERSA RECENTE]" in saida
    assert PERGUNTA_ANTERIOR in saida
    assert saida.endswith("[PERGUNTA ATUAL] e sobre isso?")


def test_follow_up_sem_nucleo_proprio_leva_historico():
    # "explique melhor" NÃO tem pronome — é o caso que referencia_contexto sozinho
    # perderia, e o motivo pelo qual o portão mede núcleo próprio.
    agent, mem = _agent_com_historico()
    for q in ("explique melhor", "detalha mais", "e a segunda opção?"):
        assert "[CONVERSA RECENTE]" in agent._pergunta_com_contexto(q, mem), q


def test_primeira_pergunta_da_sessao_segue_intacta():
    agent = Agent(AppContext(settings=settings))
    mem = SessionMemory(settings)   # sem histórico
    assert agent._pergunta_com_contexto("explique melhor", mem) == "explique melhor"


def test_kill_switch_volta_a_injetar_em_todas(monkeypatch):
    monkeypatch.setattr("mente_digital.respostas.settings.contexto_gerador_followup", False)
    agent, mem = _agent_com_historico()
    saida = agent._pergunta_com_contexto("O que o livro diz sobre cultivo de tomate?", mem)
    assert "[CONVERSA RECENTE]" in saida   # comportamento antigo, integralmente


# --- o portão puro ------------------------------------------------------------
def test_precisa_antecedente_falso_nas_perguntas_reais():
    # Amostra literal do teste real do dono (as que hoje pagam contaminação de graça).
    for q in (
        "O que o livro diz sobre cultivo de tomate?",
        "O que é topping e quando devo fazer?",
        "Quanto tempo leva a secagem dos buds?",
        "Como faço um teste de pH do solo?",
        "Me explica fotossíntese como se eu fosse uma criança",
        "Quais os passos para instalar as lâmpadas na sala de cultivo?",
    ):
        assert precisa_antecedente(q) is False, q


def test_precisa_antecedente_verdadeiro_nas_continuacoes():
    for q in (
        "explique melhor", "poderia explicar melhor?", "e sobre isso?", "continue",
        "por quê?", "e depois?", "me da mais detalhes", "resume isso",
        "e o segundo?", "e a segunda opção?", "como assim?",
    ):
        assert precisa_antecedente(q) is True, q
