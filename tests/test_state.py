"""
Memória de sessão e cache — todas limitadas por tamanho (sem creep de RAM).
"""
import asyncio

from mente_digital.config import settings
from mente_digital.state import AppContext, LruCache, SessionMemory


def test_lru_evicta_o_mais_antigo():
    c = LruCache(maxsize=2)
    c.put("a", "1")
    c.put("b", "2")
    c.put("c", "3")          # excede -> "a" (mais antigo) sai
    assert c.get("a") is None
    assert c.get("b") == "2"
    assert c.get("c") == "3"


def test_lru_get_promove_para_recente():
    c = LruCache(maxsize=2)
    c.put("a", "1")
    c.put("b", "2")
    c.get("a")               # "a" vira o mais recente
    c.put("c", "3")          # agora "b" é o mais antigo -> sai
    assert c.get("b") is None
    assert c.get("a") == "1"


def test_session_memory_respeita_maxlen():
    mem = SessionMemory(settings)
    limite = settings.max_chat_history
    for i in range(limite + 10):
        mem.registrar_turno(f"p{i}", f"r{i}")
    assert len(mem.chat_history) == limite
    # o mais antigo foi descartado; o mais novo permanece
    assert mem.chat_history[-1] == (f"p{limite + 9}", f"r{limite + 9}")


def test_drenar_etl_esvazia_a_fila():
    mem = SessionMemory(settings)
    mem.enfileirar_etl("tema1", "dados1")
    mem.enfileirar_etl("tema2", "dados2")
    itens = mem.drenar_etl()
    assert itens == [("tema1", "dados1"), ("tema2", "dados2")]
    assert len(mem.fila_etl) == 0
    # drenar de novo devolve vazio
    assert mem.drenar_etl() == []


# --- Trabalho de fundo disparado de FORA do event loop -----------------------
async def test_track_task_threadsafe_agenda_de_dentro_de_uma_thread():
    """`track_task` só serve a quem já está NO loop — e há caminho de produção que não
    está: o gate de acesso roda em `asyncio.to_thread` (main.py), e é de lá que sai o
    alerta de segurança. Numa thread de trabalho, `asyncio.create_task` levanta
    `RuntimeError: no running event loop` e o trabalho some.

    O loop é capturado no lifespan e o agendamento passa por `call_soon_threadsafe` —
    a mesma travessia que o `derrubar()` do WebSocket já usa em main.py."""
    ctx = AppContext(settings=settings)
    ctx.capturar_loop()
    rodou = asyncio.Event()

    async def trabalho() -> None:
        rodou.set()

    assert await asyncio.to_thread(ctx.track_task_threadsafe, trabalho()) is True
    await asyncio.wait_for(rodou.wait(), timeout=2)


async def test_qualquer_trabalho_de_fundo_ja_deixa_o_ctx_alcancavel_de_uma_thread():
    """A rede contra o esquecimento: `capturar_loop()` é UMA linha no lifespan, e nenhum
    teste consegue exercitar aquele lifespan (ele carrega os modelos). Apagá-la deixaria
    o alerta de segurança quebrado outra vez, com a suíte 100% verde — que é exatamente
    a classe de defeito que este caminho já teve. Então todo `track_task` também guarda o
    loop: qualquer AppContext que já criou trabalho de fundo é alcançável de fora."""
    ctx = AppContext(settings=settings)          # sem capturar_loop()
    ctx.track_task(asyncio.sleep(0))             # o boot, o scheduler, o ETL...
    rodou = asyncio.Event()

    async def trabalho() -> None:
        rodou.set()

    assert await asyncio.to_thread(ctx.track_task_threadsafe, trabalho()) is True
    await asyncio.wait_for(rodou.wait(), timeout=2)


async def test_sem_loop_capturado_recusa_em_vez_de_fingir_que_agendou():
    """O processo do VIGIA (stdlib pura, sem loop) e qualquer AppContext montado fora do
    lifespan caem aqui. Devolve False para quem chama poder deixar RASTRO do que não foi
    entregue — e fecha a corrotina, senão sobra um "never awaited" solto no log."""
    ctx = AppContext(settings=settings)          # sem capturar_loop()
    executou = []

    async def trabalho() -> None:
        executou.append(True)

    assert ctx.track_task_threadsafe(trabalho()) is False
    await asyncio.sleep(0)
    assert executou == []
