"""
Estilo falado (live): turno que chega por VOZ leva instrução de conversa falada ao LLM
(verbosidade.aplicar_fala, compondo com os níveis do #7/#44/#45), e o TTS preserva a
pontuação que o Piper converte em entonação/pausa (antes era descartada no _normalizar).
Fiação testada de ponta a ponta: pipeline_resposta(stt_ms=N) -> instrução no system.
"""
from __future__ import annotations

from collections import deque

from mente_digital.agent import Agent
from mente_digital.audio import TtsService
from mente_digital.config import settings
from mente_digital.rag import NENHUM
from mente_digital.state import AppContext, SessionMemory
from mente_digital.verbosidade import aplicar_fala, aplicar_tutor, classificar

from conftest import FakeTts, make_send


# -- aplicar_fala (puro) -------------------------------------------------------
def test_fala_noop_sem_voz():
    n = classificar("me explica como funciona o RAG")
    assert aplicar_fala(n, False) is n


def test_fala_anexa_instrucao_e_mantem_nivel():
    n = aplicar_fala(classificar("me explica como funciona o RAG"), True)
    assert "FALADA" in n.instrucao
    assert n.nome == "detalhado"   # muda o registro do texto, não o nível


def test_fala_compoe_com_curto():
    # Brevidade (#7) continua valendo; o estilo falado soma, não substitui.
    n = aplicar_fala(classificar("que horas são?"), True)
    assert "UMA frase" in n.instrucao
    assert "FALADA" in n.instrucao
    assert n.nome == "curto"


def test_fala_compoe_com_tutor():
    n = aplicar_fala(aplicar_tutor(classificar("o que é RAG?"), True), True)
    assert "TUTOR" in n.instrucao
    assert "FALADA" in n.instrucao


# -- _normalizar preserva prosódia --------------------------------------------
def test_normalizar_reticencias_viram_pausa():
    tts = TtsService()
    assert tts._normalizar("Olá… tudo bem?") == "Olá... tudo bem?"


def test_normalizar_dois_pontos_e_ponto_e_virgula_viram_virgula():
    tts = TtsService()
    assert tts._normalizar("dois pontos: e ponto e vírgula; viram vírgula") == (
        "dois pontos, e ponto e vírgula, viram vírgula"
    )


def test_normalizar_travessao_vira_pausa():
    tts = TtsService()
    assert tts._normalizar("um aparte — importante — no meio") == "um aparte , importante , no meio"


def test_normalizar_mantem_interrogacao_e_exclamacao():
    tts = TtsService()
    assert tts._normalizar("pergunta? exclamação!") == "pergunta? exclamação!"


# -- syn_config chega ao Piper -------------------------------------------------
class _Cfg:
    sample_rate = 22050


class _Chunk:
    audio_int16_bytes = b"\x00\x01\x02\x03"


class FakeVoiceComConfig:
    """Fake que ACEITA o kwarg syn_config e registra o que recebeu."""

    def __init__(self) -> None:
        self.recebidos = []
        self.config = _Cfg()

    def synthesize(self, texto, syn_config=None):
        self.recebidos.append(syn_config)
        yield _Chunk()


async def test_syn_config_e_repassado_quando_configurado():
    tts = TtsService()
    fake = FakeVoiceComConfig()
    tts._voice = fake
    tts._syn_config = object()   # sentinela: só importa a identidade
    await tts.synth_base64("olá mundo, tudo bem por aí")
    assert fake.recebidos == [tts._syn_config]


async def test_sem_syn_config_nao_passa_kwarg():
    # O caminho None chama synthesize(texto) puro — os fakes antigos (sem o kwarg,
    # como o de test_tts_cache) continuam válidos; aqui só confirmamos o None.
    tts = TtsService()
    fake = FakeVoiceComConfig()
    tts._voice = fake
    await tts.synth_base64("bom dia, como vai você")
    assert fake.recebidos == [None]


# -- dígitos têm leitura própria (não viram vírgula decimal) -------------------
def test_normalizar_horario_vira_leitura_natural():
    # "14,30" seria falado "quatorze vírgula trinta"; "14 e 30" é a leitura de horário.
    tts = TtsService()
    assert tts._normalizar("Agora são 14:30.") == "Agora são 14 e 30."
    assert tts._normalizar("amanhã às 09:05, certo?") == "amanhã às 09 e 05, certo?"


def test_normalizar_intervalo_numerico_vira_a():
    tts = TtsService()
    assert tts._normalizar("leva uns 10–15 minutos") == "leva uns 10 a 15 minutos"


def test_normalizar_dois_pontos_de_prosa_ainda_vira_virgula():
    # O caso especial é só entre dígitos; o dois-pontos de prosa segue como pausa.
    tts = TtsService()
    assert tts._normalizar("resumo: funcionou") == "resumo, funcionou"


# -- fiação de ponta a ponta: stt_ms -> origem_voz -> system prompt ------------
class _SpyLlama:
    """LLM falso que registra os kwargs de cada stream (inspeção da fiação)."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.stream_calls = []

    def preempt(self) -> None:   # pipeline_resposta chama antes de decodificar
        pass

    async def stream(self, prompt, **kw):
        self.stream_calls.append(kw)
        for t in self.tokens:
            yield t


class _VS:
    async def search(self, termos, texto_busca=None, economico=False):
        return NENHUM   # nunca deve ser chamado: a RAM responde e o early-stop segura

    async def sync(self):
        pass


class _FakeOptimizer:
    async def optimize(self, texto, history):
        return "arquitetura software"


def _agent_ram(monkeypatch, tmp_path):
    """Cascata mínima em que a RAM responde (early-stop): só a passada de resposta
    chama o LLM, então stream_calls[0] é o system da resposta ao usuário."""
    monkeypatch.setattr("mente_digital.agent.settings.early_stop_cascata", True)
    ctx = AppContext(settings=settings)
    ctx.llama = _SpyLlama(["Arquitetura ", "é ", "sobre ", "camadas."])
    ctx.tts = FakeTts()
    ctx.vectorstore = _VS()
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump", str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = _FakeOptimizer()
    mem = SessionMemory(settings)
    mem.conhecimento_sessao = deque([("arquitetura software", "SOLID, camadas, baixo acoplamento")])
    return agent, mem


async def test_turno_de_voz_leva_estilo_falado_ao_llm(monkeypatch, tmp_path):
    agent, mem = _agent_ram(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem, stt_ms=120)

    assert agent.ctx.llama.stream_calls, "a passada RAM deveria ter chamado o LLM"
    assert any("FALADA" in kw.get("system_prompt", "") for kw in agent.ctx.llama.stream_calls)


async def test_turno_digitado_nao_leva_estilo_falado(monkeypatch, tmp_path):
    agent, mem = _agent_ram(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("me fale sobre arquitetura de software", send, mem)

    assert agent.ctx.llama.stream_calls
    assert all("FALADA" not in kw.get("system_prompt", "") for kw in agent.ctx.llama.stream_calls)
