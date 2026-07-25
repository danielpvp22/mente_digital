"""qa-contract-01 (painel 2026-07-24): contrato entre CHAMADORES e classes reais.

O risco estrutural da suíte é o drift fake-vs-real: os fakes aceitam **kwargs, então
um kwarg renomeado/digitado errado num CHAMADOR é engolido e a suíte passa verde —
só quebra em produção, na GPU. Aqui as FORMAS DE CHAMADA que o código de produção
realmente usa são amarradas por `inspect.signature().bind()` às assinaturas REAIS:
se alguém renomear um parâmetro de LlamaManager/Agent/VectorStore sem atualizar os
chamadores (e os fakes), ESTE teste quebra — no CI, não na bancada.
"""
import inspect

from mente_digital.agent import Agent
from mente_digital.audio import TtsService
from mente_digital.llm import LlamaManager
from mente_digital.rag import VectorStore


def _bind(func, *args, **kwargs):
    # self=None: só a FORMA importa (nomes/aridade), nunca executa nada.
    inspect.signature(func).bind(None, *args, **kwargs)


def test_forma_das_chamadas_ao_llm():
    # Como respostas/etl/otimizador chamam stream/collect (kwargs nomeados reais).
    _bind(LlamaManager.stream, "prompt", max_tokens=64, system_prompt="s",
          temperature=0.0, preemptible=True)
    _bind(LlamaManager.collect, "prompt", max_tokens=64, system_prompt="s",
          preemptible=True)


def test_forma_da_chamada_ao_pipeline():
    # Como o ws.py invoca o pipeline (stt_ms/vad_ms do waterfall).
    _bind(Agent.pipeline_resposta, "texto", lambda d: None, object(),
          stt_ms=100, vad_ms=700)


def test_forma_da_busca_local_e_tts():
    # Como o agent chama o VectorStore (incl. o kwarg da fase-b) e o TTS.
    _bind(VectorStore.search, "termos", texto_busca="t", economico=False,
          recuperados=None)
    _bind(TtsService.synth_base64, "uma frase")
