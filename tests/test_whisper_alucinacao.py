"""parece_alucinacao (audio.py): descarta o 'Obrigado' fantasma do Whisper em NÃO-fala
(mic abrindo -> ruído -> frase-lixo), mas preserva um 'obrigado' realmente falado.
Puro — sem áudio, sem GPU, sem modelo carregado.
"""
from audio import parece_alucinacao


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


def test_limiar_do_no_speech():
    assert parece_alucinacao("obrigado", 0.6)        # no limiar: descarta
    assert not parece_alucinacao("obrigado", 0.59)   # logo abaixo: passa
