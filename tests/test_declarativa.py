"""
Afirmação vs pergunta/pedido (conserto do caso "Falcão", teste real 2026-07-21):
frase declarativa sem âncora local vira REGISTRO — não escala pra web (a busca
achava um homônimo e o fato alheio contaminava a RAM e virava átomo permanente).
"""
from collections import deque

import tools
from agent import Agent
from config import settings
from rag import NENHUM, LocalResult
from state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send

from otimizador import e_backchannel, e_declarativa


def test_backchannel_reconhece_acuse_recebido():
    for t in ["ok", "Ok.", "aham", "Aham!", "tchau", "tchau tchau", "valeu", "beleza",
              "certo", "entendi", "hmm", "isso", "isso mesmo"]:
        assert e_backchannel(t), t


def test_backchannel_nao_pega_frase_real():
    # Só a fala que É exatamente backchannel — não uma que apenas começa com "ok".
    for t in ["ok, agenda a reunião", "explica RAG", "beleza da vida",
              "que horas são", "isso está correto porque"]:
        assert not e_backchannel(t), t


def test_afirmacao_e_declarativa():
    for t in [
        "O codinome do meu drone é Falcão.",
        "Meu projeto usa uma RTX 3080",
        "A reunião foi remarcada para sexta",
    ]:
        assert e_declarativa(t), t


def test_pergunta_nao_e_declarativa():
    for t in [
        "Qual o codinome do meu drone?",
        "o que é o framework Astro",
        "como funciona o TensorRT",
        "você sabe onde deixei a chave",
        "quanto o tensorrt acelera uma rede yolo",
    ]:
        assert not e_declarativa(t), t


def test_pedido_nao_e_declarativa():
    for t in [
        "salva uma nota: testar o drone amanhã",
        "pesquisa na web sobre o Python 3.13",
        "me explica fotossíntese",
        "anota que preciso revisar o pipeline",
        "me lembra de beber água daqui a 2 minutos",
    ]:
        assert not e_declarativa(t), t


# --- WIRING (#33): declarativa NÃO é sequestrada pelo roteador de ferramenta ---
class _VSVazio:
    async def search(self, termos, texto_busca=None, economico=False):
        return LocalResult(NENHUM, None, False)

    async def sync(self):
        pass


class _WebNoop:
    async def search(self, termo, consulta=None):
        return "- FONTE WEB: genérico."

    async def prefetch(self, tema):
        return "- CONTEXTO."


class _Optimizer:
    async def optimize(self, texto, history):
        return "salvador sexta"


def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Anotado. "])
    ctx.tts = FakeTts()
    ctx.web = _WebNoop()
    ctx.vectorstore = _VSVazio()
    monkeypatch.setattr("agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr("agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = _Optimizer()
    mem = SessionMemory(settings)
    mem.conhecimento_sessao = deque()
    return agent, mem


async def test_declarativa_com_tempo_nao_roteia_ferramenta(monkeypatch, tmp_path):
    # "Vou viajar pra Salvador na sexta": 'salva' é substring de 'Salvador' → talvez_acao
    # dispara, mas a frase é DECLARATIVA → veto. O roteador NEM é chamado; vira memória.
    assert tools.talvez_acao("Vou viajar pra Salvador na sexta") is True   # o gatilho trombou
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    roteou = []
    async def _spy_rotear(texto):
        roteou.append(texto)
        return None
    monkeypatch.setattr(agent, "_rotear", _spy_rotear)

    await agent.pipeline_resposta("Vou viajar pra Salvador na sexta", send, mem)

    assert roteou == []                        # veto: roteador de ferramenta não rodou


async def test_backchannel_ignorado_no_pipeline(monkeypatch, tmp_path):
    # "ok" não roteia, não responde "registrei", não registra na RAM (só é ignorado).
    agent, mem = _agent(monkeypatch, tmp_path)
    send, capturado = make_send()
    roteou = []
    async def _spy_rotear(texto):
        roteou.append(texto)
        return None
    monkeypatch.setattr(agent, "_rotear", _spy_rotear)

    await agent.pipeline_resposta("ok", send, mem)

    assert roteou == []                                              # nem chegou ao roteador
    assert not any("registrei" in str(m).lower() for m in capturado)  # não disse "registrei"


async def test_comando_explicito_ainda_roteia(monkeypatch, tmp_path):
    # Controle: pedido imperativo ('me lembra') NÃO é declarativo → o roteador roda.
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()
    roteou = []
    async def _spy_rotear(texto):
        roteou.append(texto)
        return None                             # devolve None: cai no pipeline normal
    monkeypatch.setattr(agent, "_rotear", _spy_rotear)

    await agent.pipeline_resposta("me lembra de comprar leite amanhã", send, mem)

    assert roteou == ["me lembra de comprar leite amanhã"]   # roteador foi consultado
