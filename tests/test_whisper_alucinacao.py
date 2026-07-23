"""parece_alucinacao (audio.py): descarta o 'Obrigado' fantasma do Whisper em NÃO-fala
(mic abrindo -> ruído -> frase-lixo), mas preserva um 'obrigado' realmente falado.
Puro — sem áudio, sem GPU, sem modelo carregado.
"""
from mente_digital.audio import parece_alucinacao, transcricao_incerta


def test_filler_com_no_speech_alto_e_descartado():
    assert parece_alucinacao("Obrigado.", 0.9)
    assert parece_alucinacao("tchau", 0.8)
    assert parece_alucinacao("Muito obrigado", 0.7)


def test_filler_realmente_falado_passa():
    # "obrigado" com no_speech_prob BAIXO = fala de verdade -> NÃO descarta.
    assert not parece_alucinacao("Obrigado.", 0.1)


def test_fala_real_nunca_e_alucinacao():
    assert not parece_alucinacao("que horas são?", 0.95)
    assert not parece_alucinacao("me explica o RAG", 0.9)


def test_fantasma_curto_fora_da_allowlist_e_descartado():
    # Generalização anti-ruído: com no_speech ALTO, enunciado de 1-2 palavras é fantasma
    # (eco/ruído), mesmo NÃO estando na allowlist — pega o "Buponte"/"E aí" do teste real.
    assert parece_alucinacao("Buponte.", 0.9)
    assert parece_alucinacao("E aí", 0.8)


def test_fantasma_curto_com_no_speech_baixo_passa():
    # no_speech BAIXO = fala real curta ("sim"/"não" de verdade) -> NÃO descarta.
    assert not parece_alucinacao("Buponte.", 0.2)
    assert not parece_alucinacao("sim", 0.1)


def test_limiar_do_no_speech():
    assert parece_alucinacao("obrigado", 0.6)        # no limiar: descarta
    assert not parece_alucinacao("obrigado", 0.59)   # logo abaixo: passa


# --- Gate de confiança do STT (#37 parte 1) ----------------------------------
# Fala CURTA + avg_logprob abaixo do limiar = provável mistranscrição -> descarta.
_LIMIAR = -1.0
_MAXP = 2


def test_curta_e_incerta_e_descartada():
    # "oração" cravado de um ruído, confiança baixa (-1.5 < -1.0) e 1 palavra.
    assert transcricao_incerta("oração", -1.5, _LIMIAR, _MAXP)
    assert transcricao_incerta("atenção reparação", -2.0, _LIMIAR, _MAXP)


def test_curta_mas_confiante_passa():
    # 1 palavra, mas confiança ALTA (avg_logprob perto de 0) -> fala de verdade, passa.
    assert not transcricao_incerta("sim", -0.2, _LIMIAR, _MAXP)


def test_longa_mesmo_incerta_passa():
    # Enunciado longo (> max_palavras): uma palavra incerta não condena o todo.
    assert not transcricao_incerta("me explica o que é o rag", -1.5, _LIMIAR, _MAXP)


def test_vazia_nao_e_incerta():
    assert not transcricao_incerta("", -3.0, _LIMIAR, _MAXP)
