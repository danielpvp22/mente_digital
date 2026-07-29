"""FIGURA INLINE: a imagem entra depois da frase que fala dela, não no fim.

Pedido antigo do dono. Só passou a ser possível em 2026-07-29, quando as legendas
da Encyclopedia deixaram de estar em inglês — o casamento é por PALAVRA, a resposta
é em português, e 825 das 1.818 legendas estavam em EN.

O que nenhuma frase casa continua saindo no bloco do fim: o pior caso desta mudança
é exatamente o comportamento antigo.
"""
import re

import pytest

from mente_digital import figuras
from mente_digital import respostas
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.respostas import _FigurasInline
from mente_digital.state import AppContext
from mente_digital.textutils import palavras_chave

from conftest import FakeLlama, FakeTts, make_send, textos_de_tokens

EMBED = "![[Figuras/livro/livro_p0003_f1.webp]]"
OUTRO = "![[Figuras/livro/livro_p0009_f2.webp]]"
CHAVES = palavras_chave("Solo argiloso pesado retém água e restringe as raízes")


def _agent(tokens):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(tokens)
    ctx.tts = FakeTts()
    return Agent(ctx)


# --- o casamento puro --------------------------------------------------------
def test_duas_palavras_da_legenda_casam_a_frase():
    assert figuras.combina_com_legenda("O solo argiloso retém muita água.", CHAVES)


def test_uma_palavra_so_nao_basta_em_legenda_descritiva():
    """Quase toda frase de uma resposta sobre cultivo tem 'solo'; se uma palavra
    bastasse, a figura grudaria na primeira frase, sempre."""
    assert not figuras.combina_com_legenda("O solo precisa de nutrientes.", CHAVES)


def test_legenda_CURTA_casa_com_uma_palavra():
    """Rótulo de seção tem 2 palavras e nunca alcança 2 casamentos — com o teto
    fixo ele ia SEMPRE para o fim (medido em turno real). O limiar é proporcional:
    metade da legenda. E rótulo de seção erra pouco, porque ele É o tema da página."""
    curta = palavras_chave("Testes de Solo")

    assert figuras.combina_com_legenda("Identifique a textura do solo primeiro.", curta)
    # e a descritiva no MESMO trecho continua exigindo duas
    assert not figuras.combina_com_legenda("Identifique a textura do solo primeiro.", CHAVES)


def test_minimo_zero_desliga_o_inline():
    assert not figuras.combina_com_legenda("solo argiloso retém água", CHAVES, minimo=0)


def test_plural_da_legenda_casa_o_singular_da_frase():
    """A legenda curta é rótulo de seção ("Testes de Solo") e a frase fala no
    singular ("um teste de solo"). Sem empatar o plural, o casamento cai de 2 para
    1 e a figura vai para o fim — foi o que aconteceu no turno real de 2026-07-29."""
    chaves = palavras_chave("Testes de Solo")

    assert figuras.combina_com_legenda("Faça um teste de solo antes do plantio.", chaves)


def test_sem_legenda_nunca_casa():
    assert not figuras.combina_com_legenda("qualquer frase", set())
    assert not figuras.combina_com_legenda("", CHAVES)


# --- a fila de pendentes -----------------------------------------------------
def test_figura_casada_e_consumida_e_nao_repete():
    fila = _FigurasInline([(EMBED, CHAVES)])

    assert fila.casadas("O solo argiloso retém água.") == [EMBED]
    assert fila.casadas("O solo argiloso retém água.") == []   # já saiu
    assert fila.resto() == []


def test_o_que_ninguem_casou_sobra_para_o_fim():
    fila = _FigurasInline([(EMBED, CHAVES), (OUTRO, palavras_chave("Poda apical do broto"))])

    assert fila.casadas("O solo argiloso retém água.") == [EMBED]
    assert fila.resto() == [OUTRO]
    assert not fila                                            # esvaziou


def test_embed_que_o_modelo_ja_copiou_nao_sai_de_novo():
    """O corpo da nota contém o embed, então o modelo às vezes o imita. Emitir
    de novo mostraria a mesma imagem duas vezes."""
    fila = _FigurasInline([(EMBED, CHAVES)])

    assert fila.casadas(f"O solo argiloso retém água. {EMBED}") == []


# --- dentro do stream --------------------------------------------------------
@pytest.mark.asyncio
async def test_imagem_sai_DEPOIS_da_frase_que_fala_dela():
    tokens = ["O solo ", "argiloso retém água. ", "Outra ", "coisa qualquer aqui."]
    agent = _agent(tokens)
    send, enviados = make_send()
    fila = _FigurasInline([(EMBED, CHAVES)])

    await agent._responder_contexto("ctx", "pergunta", send, figuras=fila)

    saida = textos_de_tokens(enviados)
    assert EMBED in saida
    assert saida.index(EMBED) < saida.index("Outra")     # no meio, não no fim
    assert not fila                                      # nada sobrou para o fim


@pytest.mark.asyncio
async def test_figura_sem_frase_correspondente_nao_sai_no_meio():
    tokens = ["A poda ", "apical estimula os ramos laterais. "]
    agent = _agent(tokens)
    send, enviados = make_send()
    fila = _FigurasInline([(EMBED, CHAVES)])

    await agent._responder_contexto("ctx", "pergunta", send, figuras=fila)

    assert EMBED not in textos_de_tokens(enviados)
    assert fila.resto() == [EMBED]        # o bloco do fim ainda a entrega


@pytest.mark.asyncio
async def test_sentinela_nao_emite_figura_nenhuma():
    """O guard anti-alucinação é o motivo de a figura morar no fim. Se a resposta
    vira sentinela, a passada inteira é descartada — e figura sem resposta na tela
    é o pior resultado possível."""
    tokens = ["Não ", "tenho ", "informações ", "suficientes"]
    agent = _agent(tokens)
    send, enviados = make_send()
    fila = _FigurasInline([(EMBED, CHAVES)])

    resultado = await agent._responder_contexto("ctx", "pergunta", send, figuras=fila)

    assert resultado is None
    assert textos_de_tokens(enviados) == ""
    assert fila.resto() == [EMBED]        # intacta: o chamador nem chega a mostrá-la


@pytest.mark.asyncio
async def test_sem_figuras_o_stream_e_identico_ao_de_sempre():
    tokens = ["O solo ", "argiloso retém água. "]
    agent = _agent(tokens)
    send, enviados = make_send()

    await agent._responder_contexto("ctx", "pergunta", send)

    assert textos_de_tokens(enviados) == "O solo argiloso retém água. "


# --- instrumentação: em que ponto do texto a figura caiu ---------------------
def _so_figuras(linhas):
    return [msg for modulo, msg in linhas if modulo == "FIGURAS"]


def _espionar_log(monkeypatch):
    linhas = []
    monkeypatch.setattr(respostas.telemetry, "track",
                        lambda modulo, msg: linhas.append((modulo, msg)))
    return linhas


@pytest.mark.asyncio
async def test_log_registra_em_que_char_a_figura_caiu(monkeypatch):
    """Sem POSIÇÃO no log, conferir o inline exige reencenar o turno na mão —
    foi a maior fatia do custo de 2026-07-29. O log antigo só contava as figuras
    que SOBRAVAM para o fim, ou seja, era cego justamente no caminho novo."""
    linhas = _espionar_log(monkeypatch)
    agent = _agent(["O solo ", "argiloso retém água. ", "Outra coisa qualquer aqui."])
    send, _ = make_send()

    await agent._responder_contexto("ctx", "pergunta", send,
                                    figuras=_FigurasInline([(EMBED, CHAVES)]))

    registros = _so_figuras(linhas)
    assert len(registros) == 1                    # uma linha por TURNO, não por figura
    contagem, nomes = registros[0].split(" -> ")
    quantas, posicao, total = (int(n) for n in re.findall(r"\d+", contagem))
    assert quantas == 1
    assert 0 < posicao < total                    # caiu no MEIO, não no rodapé
    # QUAL figura era: sem isto o log não distingue a imagem certa da sem relação,
    # nem mostra a mesma sequência se repetindo entre perguntas.
    assert nomes == "livro_p0003_f1"


@pytest.mark.asyncio
async def test_turno_sem_casamento_nao_polui_o_log(monkeypatch):
    """Figura que ninguém casou já é contada pelo bloco do fim; registrá-la aqui
    também faria a mesma imagem aparecer duas vezes na leitura do log."""
    linhas = _espionar_log(monkeypatch)
    agent = _agent(["A poda ", "apical estimula os ramos laterais. "])
    send, _ = make_send()

    await agent._responder_contexto("ctx", "pergunta", send,
                                    figuras=_FigurasInline([(EMBED, CHAVES)]))

    assert _so_figuras(linhas) == []


# --- preparo (disco) ---------------------------------------------------------
@pytest.mark.asyncio
async def test_preparar_le_a_legenda_e_descarta_figura_sem_arquivo(tmp_path, monkeypatch):
    """A nota pode seguir indexada depois de o .webp ter sido apagado (aconteceu
    com a p4 do Cervantes): imagem quebrada na tela é pior que figura ausente."""
    vault = tmp_path / "vault"
    (vault / "Figuras" / "livro").mkdir(parents=True)
    nota = vault / "Figuras" / "livro" / "livro_p0003_f1.md"
    nota.write_text(
        "---\norigem: Livro X\n---\n"
        "## Solo argiloso pesado retém água\n"
        "Solo argiloso pesado retém água.\n"
        "![[Figuras/livro/livro_p0003_f1.webp]]\n",
        encoding="utf-8")
    orfa = vault / "Figuras" / "livro" / "livro_p0009_f2.md"
    orfa.write_text("---\no: x\n---\n## Sem imagem em disco\nSem imagem.\n", encoding="utf-8")
    (vault / "Figuras" / "livro" / "livro_p0003_f1.webp").write_bytes(b"fake")

    monkeypatch.setattr(settings, "caminho_obsidian", str(vault))
    monkeypatch.setattr(settings, "subpasta_figuras", "Figuras")
    agent = _agent([])

    fila = await agent._preparar_figuras([str(nota), str(orfa)])

    assert fila.casadas("O solo argiloso pesado retém água.") == [EMBED]
    assert fila.resto() == []             # a órfã nem entrou na fila
