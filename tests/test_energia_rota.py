"""
A rota `/api/energia` — o botão de MODO ECONOMIA, agora também no celular.

Fica em arquivo próprio pelo mesmo motivo do test_favicon: o que se prova aqui é
a ROTA. A decisão do watcher é pura e vive em test_standby.py; o que falta cobrir
é o contrato HTTP que três cascas consomem (página, bandeja e app Android).

TestClient SEM o context manager, como os vizinhos: assim o lifespan não roda e a
suíte não carrega modelo nenhum.
"""
from __future__ import annotations

import asyncio

import pytest


class FakeLlama:
    def __init__(self) -> None:
        self.ready = True
        self.carregou = 0

    async def ensure_loaded(self):
        self.carregou += 1
        self.ready = True


class CtxFake:
    """Só o que a rota toca."""

    def __init__(self) -> None:
        self.descansando = False
        self.llama = FakeLlama()
        self.scheduler = None
        self.interactive_idle = asyncio.Event()
        self.interactive_idle.set()
        self.liberou = 0
        self.restaurou = 0
        self.usos = 0

    async def aguardar_ocio(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.interactive_idle.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def liberar_vram(self):
        self.liberou += 1
        return {"llama", "stt", "embeddings"}

    async def restaurar_vram(self):
        self.restaurou += 1

    def marcar_uso(self) -> None:
        self.usos += 1


@pytest.fixture
def cliente(monkeypatch):
    from fastapi.testclient import TestClient

    import main
    from mente_digital import energia

    monkeypatch.setattr(energia, "medir", lambda: {"vram_mb": 1000, "ram_commit_mb": 2000})
    monkeypatch.setattr(energia, "enxugar", lambda: None)
    # ⚠ Com `MENTE_ACCESS_TOKEN` configurado, o gate do painel #7 exige o token
    # "venha de onde vier" — loopback NÃO isenta (acesso.py:32-34). O `.env` do
    # dono define o token, então sem zerá-lo aqui toda chamada leva 401 e o teste
    # falha por um motivo que não é o dele. Sem token, o gate libera o loopback
    # (e "testclient" está na lista), que é o modo padrão do projeto.
    monkeypatch.setattr(main.settings, "access_token", "")
    # Espera curta: o teste não pode gastar os 20 s reais do default.
    monkeypatch.setattr(main.settings, "energia_espera_turno_seconds", 0.05)
    ctx = CtxFake()
    main.app.state.ctx = ctx
    return TestClient(main.app), ctx


def test_desligar_solta_os_modelos_e_marca_o_estado(cliente):
    c, ctx = cliente
    r = c.post("/api/energia", json={"acao": "desligar"})
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["estado"] == "descansando"
    assert ctx.liberou == 1 and ctx.descansando is True
    assert corpo["liberados"] == ["embeddings", "llama", "stt"]


def test_desligar_CEDE_A_VEZ_a_uma_resposta_em_andamento(cliente):
    """O defeito que o teste ao vivo de 2026-08-02 expôs: o botão manual não
    tinha o veto que o watcher automático já tinha. Soltar o embedding no meio de
    um turno não derruba a resposta — deixa-a pior, em silêncio (a busca de
    figuras caiu para 'só com texto' e só o traceback contou por quê).

    Recusar com MOTIVO é melhor que estragar: quem quis economizar aperta de novo."""
    c, ctx = cliente
    ctx.interactive_idle.clear()                    # há um turno em voo

    corpo = c.post("/api/energia", json={"acao": "desligar"}).json()

    assert corpo["adiado"] is True
    assert "em andamento" in corpo["motivo"]
    assert corpo["estado"] == "ligado"
    assert ctx.liberou == 0                         # NADA foi solto
    assert ctx.descansando is False


def test_ligar_restaura_tudo_e_conta_como_uso(cliente):
    """`marcar_uso` no religar: sem ele o watcher veria "20 min sem turno" e
    mandaria dormir logo depois de o celular ter acabado de acordar o PC."""
    c, ctx = cliente
    ctx.descansando = True

    corpo = c.post("/api/energia", json={"acao": "ligar"}).json()

    assert corpo["estado"] == "ligado"
    assert ctx.restaurou == 1 and ctx.llama.carregou == 1
    assert ctx.descansando is False
    assert ctx.usos == 1


def test_estado_nao_mexe_em_nada(cliente):
    """É o que o laço da bandeja chama a cada ~30 s para manter o ícone honesto."""
    c, ctx = cliente
    ctx.descansando = True

    corpo = c.post("/api/energia", json={"acao": "estado"}).json()

    assert corpo["estado"] == "descansando"
    assert ctx.liberou == 0 and ctx.restaurou == 0


def test_health_diz_descansando_com_todas_as_letras(cliente):
    """O app do celular deduzia standby de `llm == false` — igualmente verdadeiro
    durante um boot normal, e por isso mandava um `ligar` por cima de um
    carregamento em curso."""
    c, ctx = cliente
    ctx.descansando = True
    # A rota lê mais serviços do ctx; os que faltam viram atributo simples.
    for nome in ("stt", "tts", "vectorstore"):
        setattr(ctx, nome, type("S", (), {"ready": True})())
    ctx.tarefas_de_fundo = lambda: []

    assert c.get("/api/health").json()["descansando"] is True
