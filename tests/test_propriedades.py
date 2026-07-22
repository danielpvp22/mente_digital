"""
Testes de PROPRIEDADE (painel 2026-07, hypothesis) para as máquinas de estado de
streaming — _FiltroThink e SentenceChunker. O bug delas vive nas FRONTEIRAS de
tokenização: um teste de exemplo com split fixo não varre onde o corte cai. Aqui a
propriedade é verificada sobre QUALQUER partição do mesmo texto em tokens.

Oráculo = a SPEC, não identidade: os dois descartam whitespace DE PROPÓSITO (lstrip/
strip), então as propriedades são (a) INVARIÂNCIA sob partição e (b) reconstrução
SEM PERDA de caracteres não-espaço — nunca "reconstrução byte a byte".
"""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from mente_digital.audio import SentenceChunker
from mente_digital.llm import _FiltroThink
from mente_digital import mestre

# Suíte de 600+ testes roda no Windows: teto de exemplos + sem deadline (o shrinking
# e a variação de tempo de máquina não podem deixar a suíte flaky nem lenta).
_PERFIL = settings(max_examples=100, deadline=None)


def _particionar(texto: str, cortes: list[int]) -> list[str]:
    """Fatia `texto` em tokens nas posições `cortes` (arbitrárias) — simula o stream
    do LLM chegando em pedaços de qualquer tamanho, inclusive no meio de uma tag."""
    pts = sorted({c % (len(texto) + 1) for c in cortes} | {0, len(texto)})
    return [texto[a:b] for a, b in zip(pts, pts[1:]) if texto[a:b]]


def _roda_filtro(tokens: list[str]) -> str:
    f = _FiltroThink()
    return "".join(f.push(t) for t in tokens) + f.flush()


def _roda_chunker(tokens: list[str]) -> str:
    c = SentenceChunker()
    out = []
    for t in tokens:
        out.extend(c.push(t))
    resto = c.flush()
    if resto:
        out.append(resto)
    return "".join(out)


def _sem_espaco(s: str) -> str:
    return "".join(s.split())


# ==========================================================================
# _FiltroThink
# ==========================================================================
# Texto realista: letras/pontuação/espaço MAIS os caracteres da tag, para o
# hypothesis conseguir montar "<think>", "</think>" e prefixos parciais sozinho.
_TXT = st.text(alphabet="abc <>/think\n.!", max_size=60)


@_PERFIL
@given(_TXT, st.lists(st.integers(0, 60), max_size=8))
def test_filtro_invariante_sob_particao(texto, cortes):
    # O CERNE: o resultado não pode depender de ONDE o stream foi cortado.
    inteiro = _roda_filtro([texto]) if texto else ""
    partido = _roda_filtro(_particionar(texto, cortes))
    assert inteiro == partido


@_PERFIL
@given(st.text(alphabet="abcdef .!?", max_size=40), st.lists(st.integers(0, 40), max_size=6))
def test_filtro_sem_think_e_passthrough(texto, cortes):
    # Sem a tag, nada é removido — reconstrução EXATA sob qualquer partição.
    assert _roda_filtro(_particionar(texto, cortes)) == texto


@_PERFIL
@given(
    st.text(alphabet="abc \n", max_size=30),   # miolo do <think> (sem "</think>")
    st.text(alphabet="abc .!\n", max_size=40),  # resto (a resposta real)
    st.lists(st.integers(0, 90), max_size=10),
)
def test_filtro_remove_o_bloco_think(miolo, resto, cortes):
    texto = "<think>" + miolo + "</think>" + resto
    # A spec: some o bloco e o whitespace colado após o fecha -> resto.lstrip().
    assert _roda_filtro(_particionar(texto, cortes)) == resto.lstrip()


# ==========================================================================
# SentenceChunker
# ==========================================================================
_FRASE = st.text(alphabet="abcde ,.!?0123\n", max_size=80)


@_PERFIL
@given(_FRASE, st.lists(st.integers(0, 80), max_size=10))
def test_chunker_sem_perda_de_conteudo(texto, cortes):
    # Nenhum caractere não-espaço pode sumir ou trocar de ordem no chunking.
    assert _sem_espaco(_roda_chunker(_particionar(texto, cortes))) == _sem_espaco(texto)


@_PERFIL
@given(_FRASE, st.lists(st.integers(0, 80), max_size=10))
def test_chunker_invariante_sob_particao(texto, cortes):
    inteiro = _roda_chunker([texto]) if texto else ""
    partido = _roda_chunker(_particionar(texto, cortes))
    assert _sem_espaco(inteiro) == _sem_espaco(partido)


# ==========================================================================
# mestre.dividir_comandos — não perde nem reordena ação
# ==========================================================================
@_PERFIL
@given(st.text(alphabet="abc e, ;", max_size=50))
def test_dividir_comandos_partes_sao_trechos_do_original(comando):
    # Cada parte tem que ser uma substring contígua do comando (em minúsculas) —
    # o fatiador nunca inventa nem embaralha texto, só corta em fronteiras de ação.
    baixa = comando.strip().lower()
    for parte in mestre.dividir_comandos(comando):
        assert parte.lower() in baixa
