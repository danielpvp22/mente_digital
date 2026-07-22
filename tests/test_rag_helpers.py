"""
Helpers puros do RAG: resolução de device (embeddings GPU/CPU), remoção e LEITURA
de frontmatter, metadados de indexação e chunking por cabeçalho Markdown
(estrutura Obsidian).
"""
import os
from datetime import datetime

import pytest

from mente_digital.config import settings
from mente_digital.rag import (
    _META_VERSAO,
    metadados_da_nota,
    parse_frontmatter,
    resolve_device,
    split_markdown,
    strip_frontmatter,
)

# Átomo como o `agent.normalizar_atomo` grava de verdade (ver Importado_Gemini/*.md).
ATOMO = (
    "---\n"
    "origem: Como-Funciona-a-Pool-de-Mineração-Zcash.json\n"
    "colhido_em: 2026-07-17\n"
    "---\n"
    "## Conexão com a Pool\n"
    "Você configura seu hardware usando o protocolo Stratum.\n"
    "#zettelkasten_atomico #memoria_legada\n"
)


# --- device de embeddings --------------------------------------------------
def test_resolve_device_auto_segue_a_gpu():
    assert resolve_device("auto", cuda_available=True) == "cuda"
    assert resolve_device("auto", cuda_available=False) == "cpu"


def test_resolve_device_cuda_sem_gpu_degrada_para_cpu():
    assert resolve_device("cuda", cuda_available=False) == "cpu"
    assert resolve_device("cuda:0", cuda_available=False) == "cpu"
    assert resolve_device("cuda", cuda_available=True) == "cuda"


def test_resolve_device_cpu_explicito_e_respeitado():
    assert resolve_device("cpu", cuda_available=True) == "cpu"


# --- frontmatter -----------------------------------------------------------
def test_strip_frontmatter_remove_bloco_yaml():
    nota = "---\ntitle: X\ntags: [a, b]\n---\n# Conteúdo\ncorpo"
    assert strip_frontmatter(nota) == "# Conteúdo\ncorpo"


def test_strip_frontmatter_sem_bloco_fica_inalterado():
    nota = "# Título\nsem frontmatter aqui"
    assert strip_frontmatter(nota) == nota


def test_strip_frontmatter_traco_no_meio_nao_e_afetado():
    # '---' no meio do texto (regra horizontal) não é frontmatter -> preservado
    nota = "# Título\ntexto\n---\nmais texto"
    assert strip_frontmatter(nota) == nota


def test_parse_frontmatter_le_os_campos_do_atomo_real():
    fm = parse_frontmatter(ATOMO)
    assert fm["origem"] == "Como-Funciona-a-Pool-de-Mineração-Zcash.json"
    assert fm["colhido_em"] == "2026-07-17"


def test_parse_frontmatter_sem_bloco_devolve_vazio():
    assert parse_frontmatter("## Título\ncorpo sem frontmatter") == {}
    # '---' no meio não é frontmatter (mesma regra do strip)
    assert parse_frontmatter("# T\ntexto\n---\nmais") == {}


def test_parse_frontmatter_valor_com_dois_pontos_nao_e_cortado():
    # 'origem' de web é URL — partition(':') no 1º separador, não split
    fm = parse_frontmatter("---\norigem: https://x.com/a:b\n---\ncorpo")
    assert fm["origem"] == "https://x.com/a:b"


def test_strip_e_parse_sao_contrapartes():
    # o bloco sai do texto indexado (não polui o embedding) mas vira metadado
    assert "origem" not in strip_frontmatter(ATOMO)
    assert "origem" in parse_frontmatter(ATOMO)


# --- metadados de indexação ------------------------------------------------
def test_metadados_atomo_importado_e_conversa_nao_fonte_primaria():
    # REGRESSÃO: pela pasta, todo átomo de Importado_Gemini saía 1.0/"Local" — o
    # mesmo patamar de uma nota escrita à mão. É síntese de LLM sobre conversa.
    meta = metadados_da_nota(os.path.join("vault", "Importado_Gemini", "a.md"), ATOMO, 1.0)
    assert meta["origin"] == "Conversa"
    assert meta["confidence"] == 0.8
    assert meta["origem"] == "Como-Funciona-a-Pool-de-Mineração-Zcash.json"


def test_metadados_nota_manual_continua_fonte_primaria():
    meta = metadados_da_nota(os.path.join("vault", "CPU-GPU.md"), "## Minha nota\ncorpo", 1.0)
    assert meta["origin"] == "Local"
    assert meta["confidence"] == 1.0
    assert "origem" not in meta          # ausente != None (Chroma rejeita None)
    assert "colhido_em" not in meta


def test_metadados_pasta_auto_vence_o_frontmatter():
    # átomo auto-colhido da web tem frontmatter TAMBÉM; a pasta é o sinal mais forte
    caminho = os.path.join("vault", settings.subpasta_conhecimento_novo, "x.md")
    meta = metadados_da_nota(caminho, ATOMO, 1.0)
    assert meta["origin"] == "Web"
    assert meta["confidence"] == 0.6


def test_metadados_colhido_ts_permite_filtro_de_range():
    # o Chroma compara range em NÚMERO, não em string de data: sem o ts, a poda
    # por idade viraria scan em vez de cláusula `where`.
    meta = metadados_da_nota("a.md", ATOMO, 1.0)
    assert meta["colhido_em"] == "2026-07-17"
    assert meta["colhido_ts"] == datetime(2026, 7, 17).timestamp()


def test_metadados_data_corrompida_nao_derruba_indexacao():
    nota = "---\norigem: x.json\ncolhido_em: ontem de manhã\n---\n## T\ncorpo"
    meta = metadados_da_nota("a.md", nota, 1.0)
    assert meta["colhido_em"] == "ontem de manhã"   # preserva o que estava lá
    assert "colhido_ts" not in meta                 # mas não inventa epoch


def test_metadados_carimba_versao_do_esquema():
    # é o que faz o reindex revisitar nota velha quando o esquema muda
    assert metadados_da_nota("a.md", ATOMO, 1.0)["meta_v"] == _META_VERSAO


# --- chunking por cabeçalho ------------------------------------------------
def test_split_markdown_quebra_por_secao_e_carrega_titulo():
    pytest.importorskip("langchain_text_splitters")
    conteudo = "---\ntitle: N\n---\n# Intro\nOlá mundo\n## Detalhes\nMais texto aqui\n"
    base = {"source": "x.md", "mtime": 1.0, "confidence": 1.0, "origin": "Local"}
    docs = split_markdown(conteudo, base, chunk_size=1000, chunk_overlap=0)

    assert len(docs) >= 2
    # metadados do arquivo sobrevivem em todos os chunks
    assert all(d.metadata["source"] == "x.md" for d in docs)
    assert all(d.metadata["origin"] == "Local" for d in docs)
    # o caminho de títulos entra em 'section'
    secoes = " ".join(d.metadata.get("section", "") for d in docs)
    assert "Intro" in secoes
    assert "Detalhes" in secoes


def test_split_markdown_nota_sem_cabecalho_vira_uma_secao():
    pytest.importorskip("langchain_text_splitters")
    conteudo = "só um parágrafo solto, sem nenhum cabeçalho markdown"
    base = {"source": "y.md", "mtime": 2.0, "confidence": 0.6, "origin": "Web"}
    docs = split_markdown(conteudo, base, chunk_size=1000, chunk_overlap=0)

    assert len(docs) == 1
    assert docs[0].metadata["source"] == "y.md"
    assert "parágrafo solto" in docs[0].page_content
