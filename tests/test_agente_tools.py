"""
Testes das ferramentas dos agentes: criar_lembrete (parse + persistência) e o
agente de Listas (adicionar/ler/remover num vault temporário).
"""
from __future__ import annotations

import asyncio

import pytest

import telemetry
import tools
from config import settings


class FakeVS:
    async def sync(self):
        return None


class FakeCtx:
    def __init__(self) -> None:
        self.vectorstore = FakeVS()

    def track_task(self, coro):
        coro.close()  # não deixamos a corrotina pendente no teste
        return None


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """DB temporário + vault temporário (listas escrevem em disco)."""
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "t.db"))
    telemetry.db.init()
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_listas.mkdir(parents=True, exist_ok=True)
    return tmp_path


async def test_criar_lembrete_persiste(ambiente):
    ctx = FakeCtx()
    resp = await tools._t_criar_lembrete({"quando": "daqui a 10 minutos", "mensagem": "café"}, ctx)
    assert "lembrete #" in resp and "café" in resp
    ativos = telemetry.db.listar_agendamentos(("lembrete",))
    assert len(ativos) == 1 and ativos[0]["mensagem"] == "café"


async def test_criar_lembrete_horario_invalido(ambiente):
    ctx = FakeCtx()
    resp = await tools._t_criar_lembrete({"quando": "não sei", "mensagem": "x"}, ctx)
    assert "não entendi" in resp.lower()
    assert telemetry.db.listar_agendamentos(("lembrete",)) == []


async def test_cancelar_lembrete(ambiente):
    ctx = FakeCtx()
    await tools._t_criar_lembrete({"quando": "amanhã às 8h", "mensagem": "y"}, ctx)
    ag_id = telemetry.db.listar_agendamentos(("lembrete",))[0]["id"]
    resp = await tools._t_cancelar_lembrete({"id": f"o lembrete {ag_id}"}, ctx)
    assert "cancelado" in resp
    assert telemetry.db.listar_agendamentos(("lembrete",)) == []


async def test_lista_adiciona_le_remove(ambiente):
    ctx = FakeCtx()
    await tools._t_adicionar_item({"lista": "compras", "item": "pão"}, ctx)
    await tools._t_adicionar_item({"lista": "compras", "item": "leite"}, ctx)

    lida = await tools._t_ler_lista({"lista": "compras"}, ctx)
    assert "pão" in lida and "leite" in lida

    resp = await tools._t_remover_item({"lista": "compras", "item": "pão"}, ctx)
    assert "removi" in resp
    lida2 = await tools._t_ler_lista({"lista": "compras"}, ctx)
    assert "pão" not in lida2 and "leite" in lida2


async def test_lista_inexistente(ambiente):
    ctx = FakeCtx()
    resp = await tools._t_ler_lista({"lista": "fantasma"}, ctx)
    assert "não achei" in resp.lower()


async def test_avisar_quando_cria_watcher(ambiente):
    ctx = FakeCtx()
    resp = await tools._t_avisar_quando(
        {"condicao": "dólar acima de 5,50", "termos": "cotação dólar"}, ctx
    )
    assert "aviso" in resp.lower() or "verificar" in resp.lower()
    watchers = telemetry.db.listar_agendamentos(("watcher",))
    assert len(watchers) == 1


async def test_captura_escreve_na_inbox(ambiente):
    ctx = FakeCtx()
    r1 = await tools._t_capturar({"texto": "ideia genial"}, ctx)
    r2 = await tools._t_capturar({"texto": "outra ideia"}, ctx)
    assert "anotado na inbox" in r1

    def _ler():
        with open(settings.arquivo_inbox, "r", encoding="utf-8") as f:
            return f.read()
    conteudo = await asyncio.to_thread(_ler)
    assert "# Inbox" in conteudo
    assert "ideia genial" in conteudo and "outra ideia" in conteudo


async def test_captura_vazia(ambiente):
    ctx = FakeCtx()
    resp = await tools._t_capturar({"texto": "  "}, ctx)
    assert "faltou" in resp.lower()


def test_comando_desconhecido_registra_e_agrupa(ambiente):
    # Tentativas repetidas do mesmo comando agrupam (UPSERT incrementa n).
    telemetry.db.registrar_comando_desconhecido("toca uma música")
    telemetry.db.registrar_comando_desconhecido("Toca uma música")
    telemetry.db.registrar_comando_desconhecido("acende a luz")
    itens = telemetry.db.get_comandos_desconhecidos()
    # UPSERT agrupa pela chave normalizada e mantém o texto da 1ª ocorrência.
    assert itens[0]["comando"] == "toca uma música" and itens[0]["n"] == 2
    assert {i["comando"] for i in itens} == {"toca uma música", "acende a luz"}
