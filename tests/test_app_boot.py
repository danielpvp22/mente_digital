"""Tela de boot do aplicativo nativo (app.py) — a lógica pura.

O caso que estes testes existem para impedir de voltar: em 2026-08-02 a tela
anunciava "tudo pronto" aos 11,6 s, quando o `_boot` ainda tinha 24 s de trabalho
de fundo pela frente (malha, sync do Chroma, XTTS para a RAM). A primeira pergunta
do dono caía nessa janela e decodificava a 44,8 tok/s contra os 93-113 normais da
máquina. Um marco que não é contado é um marco que mente.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import app
from mente_digital.config import settings
from mente_digital.state import AppContext


# --------------------------------------------------------------------------- #
# Progresso                                                                    #
# --------------------------------------------------------------------------- #
def _nenhum() -> dict[str, bool]:
    return dict.fromkeys((c for c, _, _ in app.MARCOS), False)


def test_pesos_somam_cem():
    """Um marco que não soma some da conta e a barra nunca fecha em 100%."""
    assert sum(peso for _, _, peso in app.MARCOS) == 100


def test_chaves_dos_marcos_sao_unicas():
    chaves = [c for c, _, _ in app.MARCOS]
    assert len(chaves) == len(set(chaves))


def test_progresso_vazio_e_zero_e_nao_libera():
    assert app.calcular_progresso(_nenhum()) == (0, False)


def test_progresso_completo_e_cem_e_libera():
    todos = dict.fromkeys((c for c, _, _ in app.MARCOS), True)
    assert app.calcular_progresso(todos) == (100, True)


def test_trabalho_de_fundo_pendente_nao_libera_a_tela():
    """O ARREPENDIMENTO de 2026-08-02, travado em teste: serviços todos prontos
    mas o fundo ainda rodando NÃO é 'pronto'."""
    p = dict.fromkeys((c for c, _, _ in app.MARCOS), True)
    p["fundo"] = False
    pct, libera = app.calcular_progresso(p)
    assert libera is False
    assert pct < 100


@pytest.mark.parametrize("prontos_ate, esperado", [
    ([], "Acordando a mente"),
    (["servidor"], "Afinando a escuta"),
    (["servidor", "stt"], "Abrindo o vault"),
    (["servidor", "stt", "vault"], "Carregando o modelo"),
    (["servidor", "stt", "vault", "llm"], "Preparando a voz"),
    (["servidor", "stt", "vault", "llm", "voz"], "Abrindo a porta"),
    (["servidor", "stt", "vault", "llm", "voz", "porta"], "Terminando o índice"),
])
def test_rotulo_do_estagio_segue_a_ordem_do_boot(prontos_ate, esperado):
    p = _nenhum()
    for chave in prontos_ate:
        p[chave] = True
    assert app.rotulo_estagio(p) == esperado


def test_rotulo_final():
    assert app.rotulo_estagio(dict.fromkeys((c for c, _, _ in app.MARCOS), True)) == "Tudo pronto"


# --------------------------------------------------------------------------- #
# Splash                                                                       #
# --------------------------------------------------------------------------- #
def test_splash_nao_deixa_placeholder():
    html = app._montar_splash(voz_preguicosa=False)
    assert "__LEGENDA__" not in html


def test_splash_rotula_voz_preguicosa():
    """Ponto verde sem explicação é mentira: com o XTTS preguiçoso a voz conta
    como pronta (só sobe no microfone), e a tela precisa DIZER isso."""
    assert "sob demanda" in app._montar_splash(voz_preguicosa=True)
    assert "sob demanda" not in app._montar_splash(voz_preguicosa=False)


# --------------------------------------------------------------------------- #
# Geometria                                                                    #
# --------------------------------------------------------------------------- #
def test_geometria_corrompida_cai_no_padrao(tmp_path, monkeypatch):
    """Arquivo de geometria quebrado JAMAIS pode impedir o app de abrir."""
    ruim = tmp_path / "app_janela.json"
    ruim.write_text("{isto não é json", encoding="utf-8")
    monkeypatch.setattr(app, "ARQ_GEOMETRIA", ruim)
    assert app.ler_geometria((430, 900)) == {"largura": 430, "altura": 900, "x": None, "y": None}


def test_geometria_ignora_campo_de_tipo_errado(tmp_path, monkeypatch):
    arq = tmp_path / "app_janela.json"
    arq.write_text(json.dumps({"largura": 500, "altura": "grande"}), encoding="utf-8")
    monkeypatch.setattr(app, "ARQ_GEOMETRIA", arq)
    geo = app.ler_geometria((430, 900))
    assert geo["largura"] == 500          # o válido entra
    assert geo["altura"] == 900           # o inválido cai no padrão


# --------------------------------------------------------------------------- #
# Trabalho de fundo (AppContext)                                               #
# --------------------------------------------------------------------------- #
async def _dormir_para_sempre() -> None:
    await asyncio.sleep(3600)


async def run_forever() -> None:                      # noqa: D103 - imita o scheduler
    await asyncio.sleep(3600)


async def _malha_e_sync() -> None:                    # noqa: D103 - imita o main.py
    await asyncio.sleep(3600)


async def test_fundo_vazio_quando_nada_roda():
    ctx = AppContext(settings=settings)
    assert ctx.tarefas_de_fundo() == []


async def test_fundo_ignora_lacos_perpetuos():
    """O `scheduler.run_forever` também é tracked e NUNCA retorna. Contá-lo como
    pendente prenderia a tela de boot para sempre."""
    ctx = AppContext(settings=settings)
    tarefa = ctx.track_task(run_forever())
    try:
        assert ctx.tarefas_de_fundo() == []
    finally:
        tarefa.cancel()


async def test_fundo_reporta_rotulo_legivel():
    """O dono pediu para saber em QUE etapa está — então o nome sai traduzido."""
    ctx = AppContext(settings=settings)
    tarefa = ctx.track_task(_malha_e_sync())
    try:
        assert ctx.tarefas_de_fundo() == ["Malha e índice"]
    finally:
        tarefa.cancel()


async def test_fundo_esvazia_quando_a_tarefa_termina():
    ctx = AppContext(settings=settings)
    ctx.track_task(asyncio.sleep(0))
    await asyncio.sleep(0.05)
    assert ctx.tarefas_de_fundo() == []
