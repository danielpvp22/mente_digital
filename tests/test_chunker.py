"""
SentenceChunker — quebra o stream de tokens em frases prontas para o TTS.
Regras sutis: não quebrar em abreviações/decimais, mas quebrar em fim de frase
real e fazer flush por tamanho para não segurar o áudio.
"""
from audio import SentenceChunker


def _push_all(chunker: SentenceChunker, texto: str, passo: int = 3):
    """Simula o stream: empurra o texto em pedacinhos e junta o que sair."""
    prontas = []
    for i in range(0, len(texto), passo):
        prontas.extend(chunker.push(texto[i : i + passo]))
    return prontas


def test_quebra_em_fim_de_frase():
    c = SentenceChunker(min_len=1, max_len=200)
    prontas = _push_all(c, "Olá mundo. Tudo bem? ")
    assert prontas == ["Olá mundo.", "Tudo bem?"]


def test_nao_quebra_em_abreviacao():
    c = SentenceChunker(min_len=1, max_len=200)
    prontas = _push_all(c, "Falei com o Dr. Silva ontem. ")
    # "Dr." não encerra frase; sai uma frase só
    assert prontas == ["Falei com o Dr. Silva ontem."]


def test_nao_quebra_em_decimal():
    c = SentenceChunker(min_len=1, max_len=200)
    prontas = _push_all(c, "A versão 3.5 chegou. ")
    assert prontas == ["A versão 3.5 chegou."]


def test_frase_curta_nao_fecha_ate_min_len():
    c = SentenceChunker(min_len=20, max_len=200)
    # "Oi." tem < 20 chars -> não fecha; continua acumulando
    prontas = c.push("Oi.")
    assert prontas == []
    resto = c.flush()
    assert resto == "Oi."


def test_flush_por_tamanho_mantem_audio_fluindo():
    c = SentenceChunker(min_len=1, max_len=30)
    # sem pontuação, mas longo -> corta em ~max_len num espaço
    texto = "palavra " * 10  # 80 chars, sem ponto final
    prontas = _push_all(c, texto)
    assert len(prontas) >= 1
    # cada pedaço emitido respeita a janela de max_len
    assert all(len(p) <= 30 for p in prontas)


def test_flush_devolve_o_resto():
    c = SentenceChunker(min_len=1, max_len=200)
    c.push("Sem pontuação final")
    assert c.flush() == "Sem pontuação final"
    # após flush, buffer zerado
    assert c.flush() == ""
