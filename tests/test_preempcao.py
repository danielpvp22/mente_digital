"""
Prioridade da inferência interativa sobre o ETL idle.

O pilar declarado é "o ETL cede a GPU para a inferência interativa". Antes ele era
falso: `interactive_idle` só evitava COMEÇAR o próximo documento — o decode em curso
segurava o `_inference_lock` até o fim. Com `max_tokens_sintese=1600` a ~120 tok/s,
uma pergunta que chegasse no meio esperava ~13s pelo primeiro token.

Aqui trava-se o contrato inteiro:
1. o pipeline interativo preempta o ETL em curso;
2. a ordem clear-antes-de-preempt (senão o ETL entra pela janela);
3. o item preemptado VOLTA pra fila (preempção não pode virar perda de conhecimento);
4. o contador de reentrância (um pipeline que sai não pode liberar a GPU se outro
   ainda decodifica — o furo que o barge-in abria).

Sem GPU: o FakeLlama imita o contrato de preempção do LlamaManager real.
"""
import asyncio
import time

import pytest

from mente_digital.agent import Agent, EtlProcessor
from mente_digital.config import Settings
from mente_digital.llm import InferenciaPreemptada, LlamaManager
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


# ==========================================================================
# O MECANISMO REAL do LlamaManager (sem GPU: só o .gguf é falso)
# ==========================================================================
class FakeModel:
    """Imita `llama_cpp.Llama.create_chat_completion(stream=True)`.

    O sleep roda na thread "gpu-infer" (nunca no event loop), como o decode real —
    é o que dá tempo de preemptar no meio.
    """

    def __init__(self, n_tokens: int = 200, delay: float = 0.002) -> None:
        self.n_tokens = n_tokens
        self.delay = delay
        self.emitidos = 0

    def create_chat_completion(self, messages, max_tokens, temperature, stream):
        for i in range(self.n_tokens):
            time.sleep(self.delay)
            self.emitidos += 1
            yield {"choices": [{"delta": {"content": f"t{i} "}}]}


def _manager(modelo: FakeModel) -> LlamaManager:
    lm = LlamaManager()
    lm._model = modelo          # sem load(): não há GPU nem .gguf no teste
    return lm


async def test_preempt_corta_o_decode_real_e_levanta():
    modelo = FakeModel(n_tokens=500)
    lm = _manager(modelo)
    recebidos = []

    async def consumir():
        async for tok in lm.stream("p", preemptible=True):
            recebidos.append(tok)

    task = asyncio.create_task(consumir())
    await asyncio.sleep(0.08)              # deixa decodificar um pouco
    atingidos = lm.preempt()

    with pytest.raises(InferenciaPreemptada):
        await task

    assert atingidos == 1
    assert 0 < len(recebidos) < 500         # cortado no meio, não no fim
    assert modelo.emitidos < 500            # o worker parou de verdade (não só o consumo)
    lm.shutdown()


async def test_stream_interativa_nunca_e_preemptada():
    # A resposta ao usuário é sagrada: preempt() não pode tocá-la.
    modelo = FakeModel(n_tokens=5)
    lm = _manager(modelo)

    async def consumir():
        return [t async for t in lm.stream("p")]      # preemptible=False (default)

    task = asyncio.create_task(consumir())
    await asyncio.sleep(0.002)
    assert lm.preempt() == 0                          # não há nada preemptável

    assert len(await task) == 5                       # terminou inteira
    lm.shutdown()


async def test_preempt_sem_etl_rodando_e_barato_e_noop():
    # O pipeline interativo chama preempt() SEMPRE; no caminho comum (sem ETL) isso
    # tem que custar zero e não quebrar nada.
    lm = _manager(FakeModel(n_tokens=1))
    assert lm.preempt() == 0
    lm.shutdown()


async def test_stop_event_sai_do_registro_ao_terminar():
    # Vazamento aqui faria preempt() setar eventos de streams mortas — inofensivo,
    # mas o set cresceria sem fim na vida do app.
    lm = _manager(FakeModel(n_tokens=3))
    async for _ in lm.stream("p", preemptible=True):
        pass
    assert lm._preemptiveis == set()
    lm.shutdown()


class FakeVectorStore:
    def __init__(self):
        self.syncs = 0

    async def sync(self):
        self.syncs += 1

    async def search(self, termos, texto_busca=None, economico=False):
        from mente_digital.rag import NENHUM, LocalResult
        return LocalResult(NENHUM, None, False)


def _ctx(tokens, tmp_path, monkeypatch):
    st = Settings(_env_file=None)
    st.caminho_obsidian = str(tmp_path)
    st.arquivo_chat_dump = str(tmp_path / "dump.md")
    import os
    os.makedirs(st.dir_conhecimento_novo, exist_ok=True)
    monkeypatch.setattr("mente_digital.agent.settings", st)
    monkeypatch.setattr("mente_digital.agent.db.log_etl", lambda *a, **k: None)
    ctx = AppContext(settings=st)
    ctx.llama = FakeLlama(tokens)
    ctx.tts = FakeTts()
    ctx.vectorstore = FakeVectorStore()
    return ctx


# --- 1/2. o pipeline preempta, e na ordem certa ------------------------------
async def test_pipeline_preempta_o_etl_ao_entrar(tmp_path, monkeypatch):
    ctx = _ctx(["ok."], tmp_path, monkeypatch)
    agent = Agent(ctx)
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    send, _ = make_send()

    await agent.pipeline_resposta("qualquer pergunta durável sobre python", send, SessionMemory(ctx.settings))

    assert ctx.llama.preempcoes == 1        # avisou o ETL para ceder a GPU


async def test_gpu_marcada_como_ocupada_antes_do_preempt(tmp_path, monkeypatch):
    # A ORDEM é o mecanismo: se preempt() viesse antes do clear(), o ETL acordaria
    # na janela entre os dois, veria "GPU livre" e começaria OUTRA síntese.
    ctx = _ctx(["ok."], tmp_path, monkeypatch)
    agent = Agent(ctx)
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)

    visto = {}
    original = ctx.llama.preempt

    def espiao():
        visto["idle_no_preempt"] = ctx.interactive_idle.is_set()
        return original()

    monkeypatch.setattr(ctx.llama, "preempt", espiao)
    send, _ = make_send()

    await agent.pipeline_resposta("pergunta durável", send, SessionMemory(ctx.settings))

    assert visto["idle_no_preempt"] is False   # já estava marcado como ocupado
    assert ctx.interactive_idle.is_set()       # e liberou ao sair


# --- 3. o item preemptado volta pra fila -------------------------------------
async def test_item_preemptado_e_retomado_e_nao_se_perde(tmp_path, monkeypatch):
    atomo = "## Ideia\ncorpo\n#zettelkasten_atomico #conhecimento_novo\n"
    ctx = _ctx([atomo], tmp_path, monkeypatch)
    etl = EtlProcessor(ctx)
    ctx.llama.armar_preempcao()          # a 1ª síntese será cortada no meio

    await etl.process_queue([("tensorrt", "dados brutos")])

    # Cortado na 1ª tentativa, retomado na 2ª: o átomo existe. Se o item fosse
    # descartado no preempt, o conhecimento sumiria em silêncio.
    import os
    gerados = [f for f in os.listdir(ctx.settings.dir_conhecimento_novo)]
    assert len(gerados) == 1


async def test_preempcao_nao_limpa_o_dump_da_conversa(tmp_path, monkeypatch):
    # summarize_dump só limpa o dump DEPOIS de salvar. Ceder a GPU no meio tem que
    # deixar a conversa inteira no arquivo para a próxima passada.
    ctx = _ctx(["## A\ncorpo\n#zettelkasten_atomico #conhecimento_novo\n"], tmp_path, monkeypatch)
    etl = EtlProcessor(ctx)
    dump = tmp_path / "dump.md"
    conversa = "## [t]\n**Usuário:** o que é tensorrt\n**Mente Digital:** acelera inferência.\n" * 3
    dump.write_text(conversa, encoding="utf-8")
    ctx.llama.armar_preempcao()

    await etl.summarize_dump()

    assert dump.read_text(encoding="utf-8") == conversa   # nada perdido


# --- 4. contador de reentrância (o furo do barge-in) -------------------------
async def test_pipeline_que_sai_nao_libera_gpu_com_outro_em_voo(tmp_path, monkeypatch):
    # O bug real: no barge-in, `_cancel_pipeline` não espera a task A morrer e já cria
    # a B. A demora de propósito (o finally do stream espera a thread da GPU parar), e
    # ao desenrolar dava set() — DESFAZENDO o clear de B e soltando o ETL na frente da
    # resposta do usuário. Com contador, só o último a sair libera.
    ctx = _ctx([], tmp_path, monkeypatch)

    async with ctx.interativo():                 # pipeline A
        assert not ctx.interactive_idle.is_set()
        async with ctx.interativo():             # pipeline B (barge-in) entra
            assert not ctx.interactive_idle.is_set()
        # A saiu; B ainda decodifica -> a GPU NÃO pode ser anunciada como livre
        assert not ctx.interactive_idle.is_set()

    assert ctx.interactive_idle.is_set()         # só agora


async def test_contador_nao_vaza_no_cancelamento(tmp_path, monkeypatch):
    # barge-in = CancelledError atravessando o context manager. O finally roda, então
    # o contador tem que voltar a zero — senão o ETL nunca mais roda na vida do app.
    ctx = _ctx([], tmp_path, monkeypatch)

    async def pipeline_cancelado():
        async with ctx.interativo():
            await asyncio.sleep(10)

    task = asyncio.create_task(pipeline_cancelado())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ctx.interactive_idle.is_set()
