"""
O dump da conversa (matéria-prima do idle) NÃO pode receber comandos de agente —
senão "mestre, adiciona leite" e "mestre, modo sigiloso" viram os átomos-lixo
"## Leite" e "## Modo Sigiloso" (bug visto no teste real).
"""
from config import settings
from state import AppContext
from ws import LiveSession


def _sess():
    ctx = AppContext(settings=settings)
    return LiveSession(ctx, websocket=None)   # __init__ não toca o ws


def _spy(monkeypatch):
    chamadas = []

    async def fake_dump(ator, texto):
        chamadas.append((ator, texto))

    monkeypatch.setattr("ws.append_chat_dump", fake_dump)
    return chamadas


async def test_comando_mestre_fora_do_dump(monkeypatch):
    chamadas = _spy(monkeypatch)
    s = _sess()
    await s._dump_pergunta("mestre, adiciona leite na lista")
    await s._dump_pergunta("mestre, modo sigiloso")
    await s._dump_pergunta("mestre, o que você fez hoje?")
    assert chamadas == []   # nenhum comando-mestre foi para o dump


async def test_pergunta_normal_vai_pro_dump(monkeypatch):
    chamadas = _spy(monkeypatch)
    s = _sess()
    await s._dump_pergunta("como funciona o tensor RT?")
    assert chamadas == [("User", "como funciona o tensor RT?")]


async def test_confidencial_fora_do_dump(monkeypatch):
    chamadas = _spy(monkeypatch)
    s = _sess()
    s.memory.confidencial = True
    await s._dump_pergunta("algo sensível que não pode ser gravado")
    assert chamadas == []


async def test_efemero_fora_do_dump(monkeypatch):
    chamadas = _spy(monkeypatch)
    s = _sess()
    await s._dump_pergunta("qual a cotação do dólar hoje?")
    assert chamadas == []
