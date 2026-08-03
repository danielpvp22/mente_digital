"""A rota `/favicon.ico` (main.py) — a marca do app na aba do navegador.

Fica em arquivo próprio, e não em test_marca.py, porque o que se testa aqui é a
ROTA: o desenho já é coberto lá, e misturar os dois faria o teste do módulo puro
depender do FastAPI.
"""
from __future__ import annotations

import pytest

from mente_digital import marca


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    """TestClient SEM o context manager — assim o lifespan não roda e a suíte não
    carrega modelo nenhum (mesmo padrão de test_figuras.py). `BASE_DIR` vai para
    tmp para o teste não escrever no dados/ real do dono."""
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setattr(main, "BASE_DIR", tmp_path)
    return TestClient(main.app)


def test_favicon_responde_um_ico(cliente):
    """Sem esta rota o navegador pede /favicon.ico assim mesmo e leva 404 em todo
    carregamento da SPA."""
    r = cliente.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == b"\x00\x00\x01\x00"      # cabeçalho ICO


def test_favicon_e_o_mesmo_arquivo_da_barra_de_tarefas(cliente, tmp_path):
    """A aba do navegador, a bandeja e a barra de tarefas mostram a MESMA marca —
    e mostram porque leem o mesmo arquivo, não porque alguém sincronizou."""
    cliente.get("/favicon.ico")
    assert marca.caminho_ico(tmp_path).exists()


def test_favicon_nao_exige_token(cliente, monkeypatch):
    """Um ícone não revela nada que a rota `/` (que serve a SPA inteira sem gate)
    já não revele — e o navegador pede o favicon sem header nenhum."""
    from mente_digital.config import settings

    monkeypatch.setattr(settings, "access_token", "um-token-qualquer")
    assert cliente.get("/favicon.ico").status_code == 200
