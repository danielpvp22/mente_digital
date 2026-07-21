"""
Preâmbulo comum (consultoria TTFT #10, investigação): com o botão ligado, TODO
system prompt sai com o MESMO começo — condição para o llama.cpp reaproveitar o KV
do prefixo entre chamadas. Off por default (kill criterion: só cravar com A/B real).
"""
from __future__ import annotations

import llm
import prompts
from config import settings


def test_off_composicao_historica(monkeypatch):
    monkeypatch.setattr(settings, "prompt_preambulo_comum", False)
    monkeypatch.setattr(settings, "llm_no_think", True)
    assert llm.montar_system("TAREFA X") == "/no_think\nTAREFA X"


def test_on_prefixo_byte_identico_entre_tarefas(monkeypatch):
    monkeypatch.setattr(settings, "prompt_preambulo_comum", True)
    monkeypatch.setattr(settings, "llm_no_think", True)
    a = llm.montar_system("TAREFA DO EXTRATOR")
    b = llm.montar_system("TAREFA DA RESPOSTA")
    prefixo = f"/no_think\n{prompts.PREAMBULO_COMUM}\n"
    # o reuso de KV exige prefixo IGUAL — e a tarefa específica preservada no fim
    assert a.startswith(prefixo) and b.startswith(prefixo)
    assert a.endswith("TAREFA DO EXTRATOR") and b.endswith("TAREFA DA RESPOSTA")


def test_on_sem_no_think(monkeypatch):
    monkeypatch.setattr(settings, "prompt_preambulo_comum", True)
    monkeypatch.setattr(settings, "llm_no_think", False)
    assert llm.montar_system("T") == f"{prompts.PREAMBULO_COMUM}\nT"


def test_default_do_botao_e_off():
    # contrato da consultoria: investigação com critério de kill — não crava default
    from config import Settings

    assert Settings(_env_file=None).prompt_preambulo_comum is False
