"""Consolidação de átomos — Fase 2 (2026-07-25). Sem GPU/Chroma: o agrupamento é
puro (numpy) e o fluxo roda com fakes + tmp_path.

O que se trava — as ressalvas do dono como asserts: originais ARQUIVADOS (nunca
deletados) e removidos do índice, canônico com proveniência da fusão, âncora no
representante (sem corrente A~B~C), nota fora do grupo intocada, cap por ciclo e
o gate do scheduler (nunca com sessão viva; 1x por intervalo).
"""
import math
import os
from datetime import datetime
from pathlib import Path

from mente_digital import consolidacao
from mente_digital.config import settings
from mente_digital.etl import EtlProcessor
from mente_digital.scheduler import SchedulerService
from mente_digital.state import AppContext


# ---------- puro: agrupamento -------------------------------------------------
def test_agrupa_identicos_e_isola_distintos():
    embs = [[1, 0, 0], [1, 0, 0], [0.999, 0.04, 0], [0, 1, 0]]
    assert consolidacao.agrupar_redundantes(embs, dist_max=0.03, min_grupo=3) == [[0, 1, 2]]


def test_min_grupo_barra_par_de_quase_iguais():
    # 2 quase-iguais é trabalho do dedup do ingest, não da consolidação.
    assert consolidacao.agrupar_redundantes([[1, 0], [1, 0]], 0.03, 3) == []


def test_ancora_no_representante_impede_corrente():
    def v(ang):
        return [math.cos(ang), math.sin(ang)]
    # B perto de A; C perto de B mas já longe de A — single-link encadearia os 3.
    a, b, c = v(0.0), v(0.1), v(0.2)
    grupos = consolidacao.agrupar_redundantes([a, b, c], dist_max=0.006, min_grupo=2)
    assert grupos == [[0, 1]]   # C ficou de fora: não derivou junto


# ---------- fluxo com fakes ---------------------------------------------------
class FakeLlamaFusao:
    async def collect(self, prompt, **kwargs):
        return ("## Ideia canônica\nFato A e fato B preservados, sem invenção.\n"
                "**Malha Neural:** [[Tema]]\n#zettelkasten_atomico")


class FakeVSCons:
    def __init__(self, corpus):
        self.corpus = corpus
        self.removidos = []

    async def corpus_com_embeddings(self):
        return self.corpus

    async def remover_fontes(self, fontes):
        self.removidos.extend(fontes)

    async def sync(self):
        pass


def _ambiente(monkeypatch, tmp_path, clusters):
    """clusters: lista de (n_arquivos, vetor). Devolve etl, ctx e os caminhos por cluster."""
    vault = tmp_path / "vault"
    novo = vault / settings.subpasta_conhecimento_novo
    novo.mkdir(parents=True)
    monkeypatch.setattr(settings, "caminho_obsidian", str(vault))
    monkeypatch.setattr(settings, "dir_arquivo_consolidacao", str(tmp_path / "_arq"))
    corpus, grupos = [], []
    for gi, (n, vetor) in enumerate(clusters):
        caminhos = []
        for i in range(n):
            p = novo / f"Sintese_g{gi}_v{i}.md"
            p.write_text(f"---\norigem: web\n---\n## Fato {gi}\nVariação {i}.\n",
                         encoding="utf-8")
            corpus.append((str(p), f"Fato {gi}", vetor))
            caminhos.append(str(p))
        grupos.append(caminhos)
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlamaFusao()
    ctx.vectorstore = FakeVSCons(corpus)
    etl = EtlProcessor(ctx)

    async def _nunca_dup(corpo):
        return False

    async def _nao_varre(info, origem_nova=""):
        return None

    monkeypatch.setattr(etl, "_ja_no_banco", _nunca_dup)
    monkeypatch.setattr(etl, "_varredura_contradicoes", _nao_varre)
    return etl, ctx, novo, grupos


async def test_grupo_vira_canonico_com_originais_arquivados(monkeypatch, tmp_path):
    etl, ctx, novo, grupos = _ambiente(
        monkeypatch, tmp_path, [(3, [1.0, 0.0, 0.0]), (1, [0.0, 1.0, 0.0])])
    redundantes, (distinto,) = grupos
    assert await etl.consolidar_atomos() == 1
    # Originais: fora do vault, ÍNTEGROS no arquivo morto (reversível), fora do índice.
    assert not any(Path(c).exists() for c in redundantes)
    assert len(list(Path(settings.dir_arquivo_consolidacao).rglob("*.md"))) == 3
    assert sorted(ctx.vectorstore.removidos) == sorted(os.path.normpath(c) for c in redundantes)
    # Canônico no vault, com a proveniência da fusão no frontmatter.
    canonicos = list(novo.glob("Consolidado_*.md"))
    assert len(canonicos) == 1
    assert "Consolidação de 3 átomos" in canonicos[0].read_text(encoding="utf-8")
    # A nota de OUTRO assunto não foi tocada.
    assert Path(distinto).exists()


async def test_cap_de_grupos_por_ciclo(monkeypatch, tmp_path):
    etl, ctx, novo, grupos = _ambiente(
        monkeypatch, tmp_path, [(3, [1.0, 0.0, 0.0]), (3, [0.0, 1.0, 0.0])])
    monkeypatch.setattr(settings, "consolidacao_grupos_por_ciclo", 1)
    assert await etl.consolidar_atomos() == 1
    # O 2º grupo ficou INTACTO para o próximo ciclo (cap limita o raio de dano).
    sobraram = [p for g in grupos for p in g if Path(p).exists()]
    assert len(sobraram) == 3


async def test_scheduler_gates_da_consolidacao(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "consolidacao_habilitada", True)  # off na suíte (conftest)
    ctx = AppContext(settings=settings)
    sched = SchedulerService(ctx)
    agora = datetime(2026, 7, 25, 4, 0)
    assert sched._consolidacao_devida(agora) is True
    ctx.sessoes.add(object())                       # conversa aberta: nunca roda
    assert sched._consolidacao_devida(agora) is False
    ctx.sessoes.clear()
    sched._ultima_consolidacao = agora              # já rodou: respeita o intervalo
    assert sched._consolidacao_devida(agora) is False
