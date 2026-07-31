"""Lotes que cabem no n_ctx — a régua é NADA SE PERDE.

Regressão do idle de 2026-07-31: o dump da conversa acumulou 2 dias (118 turnos,
~57k chars) e foi mandado INTEIRO ao LLM, que morreu com "Requested tokens (21277)
exceed context window of 8192". A resposta vazia virou "nada a reter" e o dump foi
apagado. `lotes_de_texto` é o corte que impede o estouro sem descartar conversa.
"""
import pytest

from mente_digital.textutils import lotes_de_texto


def test_texto_curto_vira_um_lote_so():
    assert lotes_de_texto("uma frase curta", 100) == ["uma frase curta"]


def test_vazio_nao_gera_lote():
    assert lotes_de_texto("", 100) == []
    assert lotes_de_texto("   \n\n  ", 100) == []


def test_budget_invalido_e_erro_explicito():
    # Falhar alto: budget 0 faria laço infinito no corte de bloco grande.
    with pytest.raises(ValueError):
        lotes_de_texto("qualquer coisa", 0)


def test_todo_lote_cabe_no_orcamento():
    turnos = "\n\n".join(f"## [t{i}]\n**Usuário:** pergunta {i}\n**Mente Digital:** " +
                         ("resposta " * 20) for i in range(40))
    lotes = lotes_de_texto(turnos, 600)
    assert len(lotes) > 1, "texto grande tinha de virar vários lotes"
    assert all(len(lote) <= 600 for lote in lotes)


def test_nada_se_perde_o_conteudo_remontado_e_o_original():
    original = "\n\n".join(f"bloco numero {i} com algum texto" for i in range(30))
    assert "\n\n".join(lotes_de_texto(original, 200)) == original


def test_corta_na_fronteira_de_turno_nao_no_meio():
    # Cada turno cabe no orçamento -> nenhum lote pode partir um turno ao meio.
    turnos = "\n\n".join(f"## [t{i}]\n**Usuário:** p{i}\n**Mente Digital:** r{i}"
                         for i in range(12))
    for lote in lotes_de_texto(turnos, 150):
        assert lote.count("**Usuário:**") == lote.count("**Mente Digital:**")


def test_bloco_maior_que_o_orcamento_e_partido_e_nao_descartado():
    # Uma resposta gigante sem linha em branco: parte, mas nenhum char some.
    gigante = "x" * 2500
    lotes = lotes_de_texto(gigante, 1000)
    assert all(len(lote) <= 1000 for lote in lotes)
    assert "".join(lotes) == gigante


def test_bloco_gigante_no_meio_nao_engole_os_vizinhos():
    texto = "antes do gigante\n\n" + ("y" * 900) + "\n\ndepois do gigante"
    lotes = lotes_de_texto(texto, 300)
    assert all(len(lote) <= 300 for lote in lotes)
    assert lotes[0] == "antes do gigante"
    assert lotes[-1] == "depois do gigante"


def test_o_dump_real_de_dois_dias_caberia_no_n_ctx():
    """O caso que estourou: ~57k chars com o orçamento de 6000 do ETL."""
    dump = "\n\n".join(f"## [2026-07-3{i % 2} 14:0{i % 10}:00]\n"
                       f"**Usuário:** pergunta {i}\n**Mente Digital:** " + ("texto " * 60)
                       for i in range(140))
    assert len(dump) > 55_000
    lotes = lotes_de_texto(dump, 6000)
    assert all(len(lote) <= 6000 for lote in lotes)
    assert "\n\n".join(lotes) == dump
