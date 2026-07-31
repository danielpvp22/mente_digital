"""Falha do worker de inferência é ERRO, não silêncio.

Regressão de 2026-07-31: o worker levantou
`ValueError: Requested tokens (21277) exceed context window of 8192`; o `stream`
apenas logava e dava `break`, então `collect()` devolvia "". Para todo chamador isso
era idêntico a "o modelo não tinha nada a dizer" — e o ETL apagou 2 dias de conversa
por causa disso.

O ramo `_WorkerError` do `llm.py` NÃO tinha um único teste antes deste arquivo.

Sem GPU: só o `.gguf` é falso; o `LlamaManager` é o real, com sua thread serializada,
seu lock e seu `finally`.
"""
import asyncio

import pytest

from mente_digital.llm import InferenciaFalhou, LlamaManager

pytestmark = pytest.mark.asyncio


class ModeloQueMorre:
    """Imita `llama_cpp.Llama.create_chat_completion` estourando o contexto."""

    def __init__(self, antes: int = 0, erro: Exception | None = None) -> None:
        self.antes = antes          # tokens emitidos ANTES de morrer
        self.erro = erro or ValueError(
            "Requested tokens (21277) exceed context window of 8192")
        self.chamadas = 0

    def create_chat_completion(self, messages, max_tokens, temperature, stream):
        self.chamadas += 1
        for i in range(self.antes):
            yield {"choices": [{"delta": {"content": f"t{i} "}}]}
        raise self.erro


class ModeloOk:
    def __init__(self) -> None:
        self.chamadas = 0

    def create_chat_completion(self, messages, max_tokens, temperature, stream):
        self.chamadas += 1
        yield {"choices": [{"delta": {"content": "ok"}}]}


def _manager(modelo) -> LlamaManager:
    lm = LlamaManager()
    lm._model = modelo          # sem load(): não há GPU nem .gguf no teste
    return lm


async def test_estouro_de_contexto_levanta_em_vez_de_devolver_vazio():
    """O caso literal do log de 2026-07-31."""
    lm = _manager(ModeloQueMorre())
    with pytest.raises(InferenciaFalhou):
        async for _ in lm.stream("prompt gigante"):
            pass


async def test_collect_propaga_a_falha_nao_devolve_string_vazia():
    """`collect` é o atalho que o ETL inteiro usa — era ele que mentia."""
    lm = _manager(ModeloQueMorre())
    with pytest.raises(InferenciaFalhou):
        await lm.collect("prompt gigante")


async def test_falha_no_meio_da_fala_nao_entrega_o_texto_parcial():
    """Antes, o parcial era devolvido como se fosse a resposta inteira — e ia parar
    no histórico e no dump que o ETL atomiza."""
    lm = _manager(ModeloQueMorre(antes=3))
    recebidos = []
    with pytest.raises(InferenciaFalhou):
        async for tok in lm.stream("p"):
            recebidos.append(tok)
    # O que saiu antes da morte é lixo, e quem chama sabe disso pela exceção:
    # nenhum caminho pode tratar `recebidos` como resposta completa.
    assert len(recebidos) <= 3


async def test_a_causa_original_fica_encadeada():
    """Diagnóstico não pode se perder: o ValueError original vira o __cause__."""
    lm = _manager(ModeloQueMorre())
    with pytest.raises(InferenciaFalhou) as exc:
        await lm.collect("p")
    assert isinstance(exc.value.__cause__, ValueError)
    assert "context window" in str(exc.value.__cause__)


async def test_a_gpu_e_liberada_a_proxima_inferencia_funciona():
    """O risco real do `raise`: lock preso ou thread da GPU pendurada."""
    lm = _manager(ModeloQueMorre())
    with pytest.raises(InferenciaFalhou):
        await lm.collect("p")

    lm._model = ModeloOk()
    assert await lm.collect("agora vai") == "ok"     # lock solto, executor vivo
    assert not lm._inference_lock.locked()


async def test_falha_nao_deixa_stop_event_no_registro_de_preemptiveis():
    """`_preemptiveis` é limpo pelo `finally` — senão um `preempt()` futuro miraria
    um decode morto."""
    lm = _manager(ModeloQueMorre())
    with pytest.raises(InferenciaFalhou):
        await lm.collect("p", preemptible=True)
    assert lm.preempt() == 0
    assert not lm._preemptiveis


async def test_duas_falhas_seguidas_nao_travam_a_serializacao():
    """O ETL faz uma SÉRIE de chamadas (um lote por vez): se a 1ª falha envenenasse
    o executor, o idle inteiro morreria no primeiro erro."""
    lm = _manager(ModeloQueMorre())
    for _ in range(3):
        with pytest.raises(InferenciaFalhou):
            await lm.collect("p")
    lm._model = ModeloOk()
    assert await lm.collect("p") == "ok"


async def test_falha_concorrente_nao_impede_a_stream_seguinte():
    """Duas chamadas disputando a GPU serializada: a que falha não pode segurar o
    lock da outra."""
    lm = _manager(ModeloQueMorre())

    async def falhar():
        with pytest.raises(InferenciaFalhou):
            await lm.collect("p")

    await asyncio.gather(falhar(), falhar())
    lm._model = ModeloOk()
    assert await lm.collect("p") == "ok"
