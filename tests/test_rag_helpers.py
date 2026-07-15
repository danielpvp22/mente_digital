"""
Helpers puros do RAG: resolução de device (embeddings GPU/CPU), remoção de
frontmatter e chunking por cabeçalho Markdown (estrutura Obsidian).
"""
import pytest

from rag import resolve_device, split_markdown, strip_frontmatter


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
