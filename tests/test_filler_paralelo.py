"""
Filler ∥ busca web (consultoria TTFT #7): a busca parte ANTES de o filler ser
sintetizado — o Piper falava "vou buscar..." com a rede parada. Os fakes marcam
um ponto de yield (asyncio.sleep(0)) onde o synth real vai a to_thread, que é o
que permite a task da busca começar durante o filler.
"""
from __future__ import annotations

import asyncio
from collections import deque

from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.rag import NENHUM, LocalResult
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, make_send


class _VSIrrelevante:
    """Banco sem match relevante -> o pipeline escala pra web."""

    async def search(self, termos, texto_busca=None, economico=False, recuperados=None):
        return LocalResult(NENHUM, None, False, [])

    async def sync(self):
        pass


class _WebOrdenada:
    def __init__(self, eventos, resposta="- FONTE WEB: conteúdo real."):
        self.ev = eventos
        self._resposta = resposta

    async def search(self, termo, consulta=None, on_colheita=None):
        self.ev.append("web:inicio")
        await asyncio.sleep(0)
        self.ev.append("web:fim")
        return self._resposta

    async def prefetch(self, tema):
        return ""


class _TtsOrdenada:
    def __init__(self, eventos):
        self.ev = eventos
        self.chamadas = []

    async def synth_base64(self, texto):
        # O synth real roda em to_thread — este sleep(0) é o MESMO ponto de yield,
        # onde o event loop tem a chance de rodar a task da busca.
        await asyncio.sleep(0)
        self.ev.append("tts")
        self.chamadas.append(texto)
        return None


class _Optimizer:
    async def optimize(self, texto, hist):
        return "tema qualquer web"


def _agent(monkeypatch, tmp_path, web):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Resposta ", "vinda ", "da ", "web."])
    eventos = web.ev
    ctx.tts = _TtsOrdenada(eventos)
    ctx.web = web
    ctx.vectorstore = _VSIrrelevante()
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = _Optimizer()
    mem = SessionMemory(settings)
    mem.conhecimento_sessao = deque()
    return agent, mem


async def test_busca_comeca_antes_do_filler_terminar(monkeypatch, tmp_path):
    eventos = []
    web = _WebOrdenada(eventos)
    agent, mem = _agent(monkeypatch, tmp_path, web)
    send, enviados = make_send()

    # turno de VOZ (stt_ms): este teste afere a FALA, e turno digitado não sintetiza.
    await agent.pipeline_resposta("me fale sobre um tema qualquer", send, mem, stt_ms=1)

    assert "web:inicio" in eventos and "tts" in eventos
    # o cerne da #7: a rede já estava trabalhando quando o filler foi sintetizado
    assert eventos.index("web:inicio") < eventos.index("tts")
    assert any(m.get("tipo") == "token" for m in enviados)


async def test_caminho_nenhum_segue_gracioso(monkeypatch, tmp_path):
    # A paralelização não pode quebrar o short-circuit: web sem NADA -> fala graciosa.
    eventos = []
    web = _WebOrdenada(eventos, resposta=NENHUM)
    agent, mem = _agent(monkeypatch, tmp_path, web)
    send, enviados = make_send()

    await agent.pipeline_resposta("me fale sobre um tema qualquer", send, mem, stt_ms=1)

    texto = "".join(m["texto"] for m in enviados if m.get("tipo") == "token")
    assert "Não encontrei" in texto
