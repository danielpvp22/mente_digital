"""
Malha Neural: extração de conceitos e índice invertido conceito -> átomos.

Contexto medido na base real (3.004 átomos): 8.403 links, mediana 3 por átomo, mas só
3,3% resolvem para o TÍTULO de um átomo. A malha não é grafo nota->nota — os links são
CONCEITOS ([[Python]] em 101 átomos, [[VRAM]] em 81) e os títulos são auto-contidos
("Clima no Paraguai: Efeito de Forno no Verão"). Por isso a vizinhança se atravessa
pelo conceito COMPARTILHADO, com peso de IDF para o hub não arrastar a base inteira.
"""
import math

from rag import MalhaIndex, extrair_conceitos, metadados_da_nota

MALHA = "**Malha Neural:** [[Mineração de Zcash]] [[Protocolo Stratum]] [[Servidor da Pool]]"


# --- extração --------------------------------------------------------------
def test_extrair_conceitos_le_a_linha_da_malha():
    assert extrair_conceitos(f"## Conexão com a Pool\ncorpo\n{MALHA}") == [
        "Mineração de Zcash",
        "Protocolo Stratum",
        "Servidor da Pool",
    ]


def test_extrair_conceitos_resolve_alias_do_obsidian():
    # [[Alvo|texto exibido]] -> o que importa é o ALVO
    assert extrair_conceitos("[[TensorRT|o TRT]] acelera") == ["TensorRT"]


def test_extrair_conceitos_dedup_ignora_caixa_e_acento():
    # repetir não torna o conceito mais forte; normaliza pela régua do textutils
    assert extrair_conceitos("[[Mineração]] x [[mineracao]] y [[MINERAÇÃO]]") == ["Mineração"]


def test_extrair_conceitos_sem_malha_devolve_vazio():
    assert extrair_conceitos("## Nota sem links\ncorpo") == []
    assert extrair_conceitos("") == []


def test_conceitos_viram_metadado_delimitado():
    # o Chroma só guarda escalar — a lista vira string com '|' nas duas pontas
    meta = metadados_da_nota("a.md", f"## T\ncorpo\n{MALHA}", 1.0)
    assert meta["conceitos"] == "|Mineração de Zcash|Protocolo Stratum|Servidor da Pool|"


def test_atomo_sem_conceito_omite_a_chave():
    assert "conceitos" not in metadados_da_nota("a.md", "## T\ncorpo", 1.0)


# --- índice ----------------------------------------------------------------
def _indice() -> MalhaIndex:
    """4 átomos: 'ia' é hub (todos), 'stratum' é raro (só a.md e b.md)."""
    docs = ["texto A", "texto B", "texto C", "texto D"]
    metas = [
        {"source": "a.md", "conceitos": "|Stratum|IA|"},
        {"source": "b.md", "conceitos": "|Stratum|IA|"},
        {"source": "c.md", "conceitos": "|IA|"},
        {"source": "d.md", "conceitos": "|IA|"},
    ]
    ix = MalhaIndex()
    ix.construir(docs, metas)
    return ix


def test_indice_conta_atomos_e_conceitos():
    ix = _indice()
    assert ix.n_atomos == 4
    assert ix.n_conceitos == 2


def test_idf_penaliza_o_hub():
    ix = _indice()
    assert ix.idf("IA") == 0.0                      # em 4/4 átomos -> log(1) = 0
    assert ix.idf("Stratum") == math.log(4 / 2)     # em 2/4 -> pontua
    assert ix.idf("Stratum") > ix.idf("IA")


def test_idf_de_conceito_ausente_e_zero_nao_explode():
    assert _indice().idf("Nunca Visto") == 0.0


def test_vizinho_vem_pelo_conceito_raro_e_o_hub_nao_arrasta():
    # semente a.md: 'Stratum' (raro) puxa b.md; 'IA' (hub) NÃO puxa c/d
    viz = _indice().vizinhos(["a.md"], limite=8, idf_min=0.1)
    fontes = [d.metadata["source"] for _, d in viz]
    assert fontes == ["b.md"]


def test_semente_nao_volta_como_vizinho():
    # ela já está no contexto; repeti-la só gastaria orçamento de chars
    viz = _indice().vizinhos(["a.md", "b.md"], limite=8, idf_min=0.1)
    assert [d.metadata["source"] for _, d in viz] == []


def test_idf_min_alto_desliga_a_expansao():
    assert _indice().vizinhos(["a.md"], limite=8, idf_min=99.0) == []


def test_limite_corta_a_vizinhanca():
    assert _indice().vizinhos(["a.md"], limite=0, idf_min=0.1) == []


def test_vizinho_carrega_texto_e_metadado_do_atomo():
    (score, doc), = _indice().vizinhos(["a.md"], limite=8, idf_min=0.1)
    assert doc.page_content == "texto B"
    assert doc.metadata["source"] == "b.md"
    assert score == math.log(4 / 2)


def test_atomo_multi_chunk_junta_o_texto_e_conta_uma_vez():
    # split_markdown pode partir um átomo em 2 chunks: é UM átomo na malha
    ix = MalhaIndex()
    ix.construir(
        ["parte 1", "parte 2", "outro"],
        [
            {"source": "a.md", "conceitos": "|Stratum|"},
            {"source": "a.md", "conceitos": "|Stratum|"},
            {"source": "b.md", "conceitos": "|Stratum|"},
        ],
    )
    assert ix.n_atomos == 2
    (_, doc), = ix.vizinhos(["b.md"], limite=8, idf_min=0.0)
    assert doc.page_content == "parte 1\nparte 2"


def test_construir_e_idempotente():
    ix = _indice()
    ix.construir(["texto A"], [{"source": "a.md", "conceitos": "|Stratum|"}])
    assert ix.n_atomos == 1          # não acumula com a construção anterior
    assert ix.n_conceitos == 1


# --- centralidade (G7: hubs primeiro na síntese) ----------------------------
def test_centralidade_premia_conceito_raro_compartilhado_no_conjunto():
    # a/b compartilham 'Stratum' (raro, idf>0); 'IA' é hub (idf 0, não pontua).
    # No conjunto inteiro, a e b são centrais (um puxa o outro pelo raro); c/d não.
    cent = _indice().centralidade(["a.md", "b.md", "c.md", "d.md"])
    assert cent["a.md"] == math.log(4 / 2)   # Stratum * 1 vizinho no conjunto (b)
    assert cent["b.md"] == math.log(4 / 2)
    assert cent["c.md"] == 0.0               # só 'IA' (hub, idf 0)
    assert cent["d.md"] == 0.0
    assert cent["a.md"] > cent["c.md"]


def test_centralidade_zero_sem_vizinho_no_conjunto():
    # sem o par que compartilha o conceito raro, o átomo não é central AQUI
    cent = _indice().centralidade(["a.md", "c.md"])   # b.md fora do conjunto
    assert cent["a.md"] == 0.0               # 'Stratum' não reaparece no conjunto
    assert cent["c.md"] == 0.0


def test_centralidade_ignora_source_fora_da_malha():
    cent = _indice().centralidade(["a.md", "desconhecido.md"])
    assert "desconhecido.md" not in cent     # só átomos conhecidos entram
