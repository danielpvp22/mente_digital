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
# Identidade na barra de tarefas                                               #
# --------------------------------------------------------------------------- #
def test_identidade_windows_nunca_levanta(monkeypatch):
    """Rodada no caminho crítico do boot, antes da janela existir. Se o shell32
    recusar (política de grupo, Windows antigo, Wine), o app tem de abrir assim
    mesmo — só com o ícone do interpretador, que é o estado anterior."""
    monkeypatch.setattr(app.os, "name", "posix")
    assert app.identidade_windows() is False


def test_app_id_nao_e_o_titulo():
    """São coisas diferentes e é fácil confundir: o AppUserModelID identifica o
    APLICATIVO para o Windows (agrupamento, fixar na barra, jump list) e nunca é
    exibido; o título é o texto que a barra de tarefas mostra ao passar o mouse.
    Um AppUserModelID com espaço/acento quebra o agrupamento em silêncio."""
    assert app.TITULO == "Mente Digital"
    assert " " not in app.APP_ID
    assert app.APP_ID.isascii()


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


async def test_trabalho_de_runtime_nao_conta_como_boot():
    """A REGRESSÃO de 2026-08-02, travada em teste.

    A primeira versão varria TODAS as tarefas retidas, excluindo uma lista de laços
    perpétuos. Só que o app cria trabalho de fundo o tempo todo em runtime — a
    passada de consolidação do scheduler, o ETL do idle — e nada disso é boot. A
    tela ficou presa em 80% esperando uma fila que nunca esvazia; o dono viu
    "180s · faltando: Rede, Índice e ajustes" numa janela que já estava pronta.

    Lista de PERMISSÃO conserta: "o boot terminou?" é pergunta sobre um conjunto
    FECHADO, e nada criado depois pode entrar nele por acidente."""
    ctx = AppContext(settings=settings)
    perpetua = ctx.track_task(run_forever())        # o scheduler
    runtime = ctx.track_task(_dormir_para_sempre())  # consolidação / ETL do idle
    try:
        assert ctx.tarefas_de_fundo() == []
    finally:
        perpetua.cancel()
        runtime.cancel()


async def test_fundo_reporta_rotulo_legivel():
    """O dono pediu para saber em QUE etapa está — então o rótulo é declarado
    no despacho, não adivinhado pelo nome da corrotina."""
    ctx = AppContext(settings=settings)
    tarefa = ctx.track_boot_task(_malha_e_sync(), "Malha e índice")
    try:
        assert ctx.tarefas_de_fundo() == ["Malha e índice"]
    finally:
        tarefa.cancel()


async def test_boot_e_runtime_convivem():
    """Só a do boot aparece, mesmo com runtime rodando ao lado."""
    ctx = AppContext(settings=settings)
    boot = ctx.track_boot_task(_malha_e_sync(), "Malha e índice")
    runtime = ctx.track_task(_dormir_para_sempre())
    try:
        assert ctx.tarefas_de_fundo() == ["Malha e índice"]
    finally:
        boot.cancel()
        runtime.cancel()


async def test_fundo_esvazia_quando_a_tarefa_termina():
    """E a entrada some do dicionário — importa num processo que fica dias no ar."""
    ctx = AppContext(settings=settings)
    ctx.track_boot_task(asyncio.sleep(0), "Modelo na GPU")
    await asyncio.sleep(0.05)
    assert ctx.tarefas_de_fundo() == []
    assert ctx._rotulo_boot == {}
