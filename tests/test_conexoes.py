"""
Descobridor de Conexões (G8, Onda 3): "mestre, alguma conexão nova?" acha PONTES no vault
— notas que ligam dois conceitos ESTABELECIDOS (cada um em vários átomos) que quase nunca
co-ocorrem. Algoritmo puro em grafo.py; detector puro em mestre.py; entrega SOB DEMANDA no
_fluxo_mestre. Sem comunidades (não são necessárias): co-ocorrência baixa = "vivem separados".
"""
import grafo
import mestre
from agent import Agent
from config import settings
from rag import MalhaIndex
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


# --- grafo.descobrir_pontes (puro) ------------------------------------------
def test_ponte_liga_dois_temas_estabelecidos_raramente_juntos():
    por_conceito = {
        "python": {"a1", "a2", "a3", "bridge"},   # tema estabelecido (df 4)
        "musica": {"b1", "b2", "b3", "bridge"},   # idem
    }
    conceitos_de = {
        "a1": ["python"], "a2": ["python"], "a3": ["python"],
        "b1": ["musica"], "b2": ["musica"], "b3": ["musica"],
        "bridge": ["python", "musica"],           # co-ocorrem SÓ aqui
    }
    pontes = grafo.descobrir_pontes(por_conceito, conceitos_de, df_min=3, coocorrencia_max=1, limite=3)
    assert len(pontes) == 1
    assert pontes[0].source == "bridge"
    assert {pontes[0].conceito_a, pontes[0].conceito_b} == {"python", "musica"}


def test_tema_solto_abaixo_do_df_min_nao_e_ponte():
    por_conceito = {"python": {"a1", "a2", "a3", "bridge"}, "incidental": {"bridge"}}
    conceitos_de = {
        "a1": ["python"], "a2": ["python"], "a3": ["python"],
        "bridge": ["python", "incidental"],       # 'incidental' só aqui (df 1)
    }
    assert grafo.descobrir_pontes(por_conceito, conceitos_de, df_min=3, coocorrencia_max=1, limite=3) == []


def test_temas_que_coocorrem_muito_nao_sao_ponte():
    # python e web andam SEMPRE juntos -> não é conexão surpreendente
    por_conceito = {"python": {"a1", "a2", "a3"}, "web": {"a1", "a2", "a3"}}
    conceitos_de = {"a1": ["python", "web"], "a2": ["python", "web"], "a3": ["python", "web"]}
    assert grafo.descobrir_pontes(por_conceito, conceitos_de, df_min=3, coocorrencia_max=1, limite=3) == []


def test_ordena_por_forca_e_respeita_limite():
    por_conceito = {
        "python": {f"p{i}" for i in range(5)} | {"b1"},   # df 6
        "musica": {f"m{i}" for i in range(5)} | {"b1"},   # df 6
        "arte":   {f"r{i}" for i in range(3)} | {"b2"},   # df 4
        "fisica": {f"f{i}" for i in range(3)} | {"b2"},   # df 4
    }
    conceitos_de = {
        **{f"p{i}": ["python"] for i in range(5)},
        **{f"m{i}": ["musica"] for i in range(5)},
        **{f"r{i}": ["arte"] for i in range(3)},
        **{f"f{i}": ["fisica"] for i in range(3)},
        "b1": ["python", "musica"],     # força min(6,6)/1 = 6
        "b2": ["arte", "fisica"],       # força min(4,4)/1 = 4
    }
    todas = grafo.descobrir_pontes(por_conceito, conceitos_de, df_min=3, coocorrencia_max=1, limite=3)
    assert [p.source for p in todas] == ["b1", "b2"]     # mais forte primeiro
    top1 = grafo.descobrir_pontes(por_conceito, conceitos_de, df_min=3, coocorrencia_max=1, limite=1)
    assert [p.source for p in top1] == ["b1"]


def test_pontes_vazio_sem_dados():
    assert grafo.descobrir_pontes({}, {}, df_min=3, coocorrencia_max=1, limite=3) == []


# --- mestre.comando_conexoes (puro) -----------------------------------------
def test_comando_conexoes_detecta():
    for c in ("alguma conexão nova?", "quais conexões você achou", "o que liga meus temas", "mostra as pontes"):
        assert mestre.comando_conexoes(c), c


def test_comando_conexoes_ignora_outros():
    for c in ("adiciona pão na lista", "cria um lembrete pra amanhã", "o que é RAG", ""):
        assert not mestre.comando_conexoes(c), c


# --- integração: _fluxo_mestre fala a ponte ---------------------------------
class _VSComMalha:
    def __init__(self, malha):
        self.malha = malha

    async def sync(self):
        pass


def _malha_com_ponte():
    malha = MalhaIndex()
    docs = ["nota"] * 6 + ["conexao"]
    metas = [
        {"source": "p1.md", "conceitos": "|Python|"}, {"source": "p2.md", "conceitos": "|Python|"},
        {"source": "p3.md", "conceitos": "|Python|"},
        {"source": "m1.md", "conceitos": "|Musica|"}, {"source": "m2.md", "conceitos": "|Musica|"},
        {"source": "m3.md", "conceitos": "|Musica|"},
        {"source": "Ponte Curiosa.md", "conceitos": "|Python|Musica|"},
    ]
    malha.construir(docs, metas)
    return malha


def _agent(monkeypatch, malha):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["ok."])
    ctx.tts = FakeTts()
    ctx.vectorstore = _VSComMalha(malha)
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.listar_atalhos", lambda *a, **k: {})
    return Agent(ctx), SessionMemory(settings)


async def test_descobrir_conexoes_formata_a_ponte(monkeypatch):
    agent, mem = _agent(monkeypatch, _malha_com_ponte())
    send, _ = make_send()
    fala, rota = await agent._descobrir_conexoes(send)
    assert rota == "mestre:conexoes"
    assert "Ponte Curiosa" in fala                       # nomeia a nota (sem .md)
    assert "python" in fala and "musica" in fala         # nomeia os dois temas


async def test_descobrir_conexoes_vault_sem_ponte(monkeypatch):
    agent, mem = _agent(monkeypatch, MalhaIndex())       # malha vazia
    send, _ = make_send()
    fala, rota = await agent._descobrir_conexoes(send)
    assert "Não achei" in fala


async def test_pipeline_roteia_comando_para_conexoes(monkeypatch):
    # o comando-mestre chega ao _descobrir_conexoes (não cai em "não reconheci")
    agent, mem = _agent(monkeypatch, _malha_com_ponte())
    send, _ = make_send()
    await agent.pipeline_resposta("mestre, alguma conexão nova?", send, mem)
    falado = " ".join(agent.ctx.tts.chamadas)
    assert "Ponte Curiosa" in falado
    assert "reconheci" not in falado.lower()
