"""
Ciclo de vida do modelo na GPU: descarregar no idle (liberar VRAM) e religar sob
demanda. Sem GPU nem .gguf — o FakeModel imita o contrato de fechamento e decode.
"""

import pytest

from mente_digital.llm import LlamaManager


class FakeModel:
    """Imita o mínimo do llama_cpp.Llama que o ciclo toca: decode e close()."""

    def __init__(self) -> None:
        self.fechado = False

    def create_chat_completion(self, messages, max_tokens, temperature, stream):
        yield {"choices": [{"delta": {"content": "ok"}}]}

    def close(self) -> None:
        self.fechado = True


@pytest.mark.asyncio
async def test_unload_fecha_o_modelo_e_libera():
    lm = LlamaManager()
    modelo = FakeModel()
    lm._model = modelo
    lm._ready = True

    await lm.unload()

    assert modelo.fechado is True          # close() foi chamado (libera a VRAM)
    assert lm._model is None
    assert lm.ready is False


@pytest.mark.asyncio
async def test_unload_sem_modelo_e_noop():
    lm = LlamaManager()
    await lm.unload()                       # nada carregado: não explode
    assert lm._model is None


@pytest.mark.asyncio
async def test_ensure_loaded_religa_via_load(monkeypatch):
    lm = LlamaManager()
    chamou = {"n": 0, "warmup": None}

    async def fake_load(warmup: bool = True) -> None:
        chamou["n"] += 1
        chamou["warmup"] = warmup
        lm._model = FakeModel()
        lm._ready = True

    monkeypatch.setattr(lm, "load", fake_load)
    await lm.ensure_loaded()

    assert chamou["n"] == 1
    assert chamou["warmup"] is False        # religar NÃO paga warm-up (a query aquece)
    assert lm._model is not None


@pytest.mark.asyncio
async def test_ensure_loaded_e_noop_com_modelo_presente(monkeypatch):
    lm = LlamaManager()
    lm._model = FakeModel()

    async def nao_deve_chamar(warmup: bool = True) -> None:
        raise AssertionError("load não deveria ser chamado com modelo já presente")

    monkeypatch.setattr(lm, "load", nao_deve_chamar)
    await lm.ensure_loaded()                # já carregado: barato, sem load()


@pytest.mark.asyncio
async def test_stream_interativa_religa_o_modelo_descarregado(monkeypatch):
    lm = LlamaManager()                     # descarregado (_model is None)
    religou = {"n": 0}

    async def fake_ensure() -> None:
        religou["n"] += 1
        lm._model = FakeModel()

    monkeypatch.setattr(lm, "ensure_loaded", fake_ensure)
    toks = [t async for t in lm.stream("oi", max_tokens=1)]

    assert religou["n"] == 1                 # a resposta ao usuário traz o modelo de volta
    assert "".join(toks).strip() == "ok"


@pytest.mark.asyncio
async def test_stream_preemptivel_NAO_religa(monkeypatch):
    # Se o modelo saiu, o idle acabou. Ressuscitá-lo para trabalho de fundo (ETL)
    # anularia o unload — o decode preemptível apenas não roda.
    lm = LlamaManager()
    religou = {"n": 0}

    async def fake_ensure() -> None:
        religou["n"] += 1

    monkeypatch.setattr(lm, "ensure_loaded", fake_ensure)
    toks = [t async for t in lm.stream("bg", max_tokens=1, preemptible=True)]

    assert religou["n"] == 0
    assert toks == []
