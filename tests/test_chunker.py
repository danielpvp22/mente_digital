"""
SentenceChunker — quebra o stream de tokens em frases prontas para o TTS.
Regras sutis: não quebrar em abreviações/decimais, mas quebrar em fim de frase
real e fazer flush por tamanho para não segurar o áudio.
"""
from mente_digital.audio import SentenceChunker


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


# --- 1º chunk agressivo (consultoria TTFT #8) --------------------------------

def test_primeiro_chunk_corta_na_virgula():
    c = SentenceChunker(min_len=8, max_len=200, primeiro_max=60)
    prontas = _push_all(c, "A resposta é quarenta e dois, mas depende do contexto todo. ")
    # 1º chunk fecha na vírgula (áudio mais cedo); o resto segue a regra normal
    assert prontas == ["A resposta é quarenta e dois,", "mas depende do contexto todo."]


def test_primeiro_chunk_nao_corta_decimal_pt():
    c = SentenceChunker(min_len=1, max_len=200, primeiro_max=60)
    prontas = _push_all(c, "O lote custa 3,5 milhões hoje. ")
    # vírgula decimal (sem espaço depois) NÃO é fronteira
    assert prontas == ["O lote custa 3,5 milhões hoje."]


def test_virgula_curta_demais_espera_a_proxima_fronteira():
    c = SentenceChunker(min_len=8, max_len=200, primeiro_max=60)
    prontas = _push_all(c, "Sim, é exatamente isso que acontece. ")
    # "Sim," (4 chars) < min_len -> não fecha; a frase sai inteira
    assert prontas == ["Sim, é exatamente isso que acontece."]


def test_primeiro_chunk_flush_na_janela_menor():
    c = SentenceChunker(min_len=1, max_len=200, primeiro_max=30)
    prontas = _push_all(c, "palavra " * 12)          # 96 chars sem pontuação
    assert prontas and len(prontas[0]) <= 30         # 1º chunk sai na janela agressiva


def test_primeiro_max_nunca_alarga_o_max_len():
    # max_len pequeno (30) com primeiro_max maior (60): vale o MENOR — o 1º chunk
    # jamais pode segurar MAIS áudio que o teto histórico.
    c = SentenceChunker(min_len=1, max_len=30, primeiro_max=60)
    prontas = _push_all(c, "palavra " * 10)
    assert all(len(p) <= 30 for p in prontas)


def test_primeiro_max_zero_desliga():
    c = SentenceChunker(min_len=1, max_len=200, primeiro_max=0)
    prontas = _push_all(c, "Bom dia, tudo certo por aí. ")
    assert prontas == ["Bom dia, tudo certo por aí."]   # comportamento histórico


def test_segundo_chunk_ignora_virgula():
    c = SentenceChunker(min_len=1, max_len=200, primeiro_max=60)
    prontas = _push_all(c, "Primeira frase inteira aqui. Depois, a segunda segue sem cortar. ")
    assert prontas == [
        "Primeira frase inteira aqui.",
        "Depois, a segunda segue sem cortar.",
    ]
