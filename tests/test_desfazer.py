"""
Desfazer (#8): "mestre, desfaça" reverte a última ação REVERSÍVEL da sessão.

A reversão vive na RAM (SessionMemory.ultima_reversivel), é consumida no undo (não se
desfaz o desfazer) e só cobre mutações com inverso determinístico (add/remove de lista,
lembrete). Aqui o teste é de INTEGRAÇÃO: a ação roda de verdade num vault temporário e
o "desfaça" desfaz o efeito no arquivo.

Sem GPU/rede: LLM/TTS/vectorstore são fakes; o DB é neutralizado por monkeypatch.
"""
import os

from mente_digital import tools
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send


class FakeVS:
    async def sync(self):
        pass


def _agent(monkeypatch, tmp_path):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["ok."])
    ctx.tts = FakeTts()
    ctx.vectorstore = FakeVS()
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.registrar_auditoria", lambda *a, **k: None)
    # Vault temporário: as listas gravam de verdade, isoladas do vault do projeto.
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    settings.dir_listas.mkdir(parents=True, exist_ok=True)
    return Agent(ctx), SessionMemory(settings)


def _conteudo_lista(nome="compras"):
    caminho = tools._caminho_lista(nome)
    if not os.path.exists(caminho):
        return ""
    with open(caminho, "r", encoding="utf-8") as f:
        return f.read()


async def test_desfazer_remove_item_adicionado(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona pão na lista", send, mem)
    assert "pão" in _conteudo_lista()                      # a ação rodou de fato
    assert mem.ultima_reversivel is not None               # lembrou como desfazer
    assert mem.ultima_reversivel[0].tool == "remover_item"

    await agent.pipeline_resposta("mestre, desfaça", send, mem)
    assert "pão" not in _conteudo_lista()                  # o efeito foi revertido
    assert mem.ultima_reversivel is None                   # consumido (não repete)


async def test_desfazer_readiciona_item_removido(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona leite na lista", send, mem)
    await agent.pipeline_resposta("mestre, remove leite da lista", send, mem)
    assert "leite" not in _conteudo_lista()
    assert mem.ultima_reversivel[0].tool == "adicionar_item"  # inverso do remover

    await agent.pipeline_resposta("mestre, desfaça", send, mem)
    assert "leite" in _conteudo_lista()                    # voltou


async def test_desfazer_sem_nada_para_desfazer(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, enviados = make_send()

    await agent.pipeline_resposta("mestre, desfaça", send, mem)
    fala = "".join(m["texto"] for m in enviados if m.get("tipo") == "token")
    assert "nenhuma ação" in fala.lower()


async def test_desfazer_e_consumido_uma_vez_so(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona ovos na lista", send, mem)
    await agent.pipeline_resposta("mestre, desfaça", send, mem)   # remove ovos
    assert "ovos" not in _conteudo_lista()

    # Um segundo "desfaça" não deve re-adicionar (o undo foi consumido).
    send2, enviados2 = make_send()
    await agent.pipeline_resposta("mestre, desfaça", send2, mem)
    assert "ovos" not in _conteudo_lista()
    fala = "".join(m["texto"] for m in enviados2 if m.get("tipo") == "token")
    assert "nenhuma ação" in fala.lower()


async def test_leitura_nao_vira_alvo_de_undo(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona café na lista", send, mem)
    alvo = mem.ultima_reversivel
    # Uma leitura depois NÃO pode apagar o alvo de undo do "adiciona".
    await agent.pipeline_resposta("mestre, o que tem na lista", send, mem)
    assert mem.ultima_reversivel is alvo                   # intacto
    await agent.pipeline_resposta("mestre, desfaça", send, mem)
    assert "café" not in _conteudo_lista()


# ---------------------------------------------------------------------------
# #9 — Corta-e-Corrige (desfaz a última adição e refaz com o valor certo)
# ---------------------------------------------------------------------------
async def test_corrige_troca_o_item(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona pão na lista", send, mem)
    await agent.pipeline_resposta("mestre, corrige: era leite, não pão", send, mem)
    conteudo = _conteudo_lista()
    assert "leite" in conteudo and "pão" not in conteudo   # trocou pão -> leite


async def test_corrige_depois_desfaz_limpo(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona pão na lista", send, mem)
    await agent.pipeline_resposta("mestre, corrige para leite", send, mem)
    # "desfaça" após corrigir remove só o item corrigido — não ressuscita o pão.
    await agent.pipeline_resposta("mestre, desfaça", send, mem)
    conteudo = _conteudo_lista()
    assert "leite" not in conteudo and "pão" not in conteudo


async def test_corrige_encadeado(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, _ = make_send()

    await agent.pipeline_resposta("mestre, adiciona pão na lista", send, mem)
    await agent.pipeline_resposta("mestre, corrige para leite", send, mem)
    await agent.pipeline_resposta("mestre, corrige para água", send, mem)
    conteudo = _conteudo_lista()
    assert "água" in conteudo
    assert "leite" not in conteudo and "pão" not in conteudo   # correções encadeadas limpas


async def test_corrige_sem_acao_recente(monkeypatch, tmp_path):
    agent, mem = _agent(monkeypatch, tmp_path)
    send, enviados = make_send()

    await agent.pipeline_resposta("mestre, corrige para leite", send, mem)
    fala = "".join(m["texto"] for m in enviados if m.get("tipo") == "token")
    assert "corrigir" in fala.lower()
    assert _conteudo_lista() == ""   # não criou nada
