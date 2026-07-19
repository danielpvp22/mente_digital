"""
_FiltroThink — remove o bloco `<think>…</think>` do INÍCIO do stream (Qwen3).

Sem ele, a tag vaza para o SentenceChunker e o TTS **fala a marcação**. Puro: recebe
tokens, devolve o que pode ser emitido. Testado token-a-token, inclusive com a tag
quebrada entre tokens (é assim que ela chega no streaming real).
"""
from llm import _FiltroThink


def _rodar(tokens):
    """Alimenta o filtro token-a-token e devolve o texto final emitido."""
    f = _FiltroThink()
    return "".join(f.push(t) for t in tokens) + f.flush()


def test_remove_bloco_vazio_do_no_think():
    # O caso REAL do Qwen3 com "/no_think": o bloco vem, porém vazio.
    assert _rodar(["<think>", "\n\n", "</think>", "\n\n", "Ulã ", "Bator"]) == "Ulã Bator"


def test_remove_bloco_com_raciocinio():
    assert _rodar(["<think>", "deixa eu pensar", "</think>", "resposta"]) == "resposta"


def test_sem_bloco_passa_intacto():
    # Qwen2.5, Llama e afins nunca emitem <think> — o filtro não pode atrapalhar.
    assert _rodar(["Olá", " mundo", "!"]) == "Olá mundo!"


def test_tag_quebrada_entre_tokens():
    # No streaming a tag chega picotada; o filtro segura enquanto ainda PODE ser <think>.
    assert _rodar(["<", "th", "ink>", "x", "</think>", "oi"]) == "oi"


def test_texto_que_so_parece_tag_nao_e_engolido():
    assert _rodar(["<p", ">algo"]) == "<p>algo"


def test_flush_nao_engole_prefixo_incompleto():
    # Stream acabou no meio de algo que PODIA virar "<think>": devolve o retido.
    assert _rodar(["<thi"]) == "<thi"


def test_passthrough_depois_do_bloco():
    # Fechado o bloco, o filtro sai da frente (custo zero por token daí em diante).
    f = _FiltroThink()
    f.push("<think>")
    f.push("</think>")
    f.push("\n")            # espaço colado ao bloco: descartado
    assert f.push("a") == "a"
    assert f.push(" b") == " b"


def test_resposta_colada_no_fecha_think():
    # Sem espaço entre </think> e o texto — o resto do mesmo token tem que sair.
    assert _rodar(["<think></think>Oi"]) == "Oi"
