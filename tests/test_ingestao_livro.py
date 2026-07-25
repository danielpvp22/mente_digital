"""Ingestão de livros — Fase 1 (2026-07-25). Sem GPU/PDF real: o módulo livro é
puro e o processamento roda com FakeLlama + diretórios em tmp_path.

O que se trava: fatiamento sem perda, detecção de livro escaneado (o caso da
Fase 3), segmentação por TOC vs janelas, o capítulo virando átomos COM
PROVENIÊNCIA (livro/cap/página no frontmatter) + nota-síntese, o job saindo de
pendentes/ só após sucesso, o cap por ciclo, e o gate do scheduler (nunca com
sessão viva — restrição do dono: atomização só em idle).
"""
import json

from mente_digital import livro
from mente_digital.config import settings
from mente_digital.etl import EtlProcessor
from mente_digital.scheduler import SchedulerService
from mente_digital.state import AppContext


# ---------- puro: fatiar/escaneado/jobs --------------------------------------
def test_fatiar_lotes_respeita_max_e_nao_perde_conteudo():
    texto = "\n\n".join(f"Parágrafo {i} " + "x" * 80 for i in range(20))
    lotes = livro.fatiar_lotes(texto, max_chars=300)
    assert all(len(lote) <= 300 for lote in lotes)
    assert "".join(lotes).replace("\n", "").replace(" ", "") == \
        texto.replace("\n", "").replace(" ", "")


def test_fatiar_paragrafo_monstro_corta_duro():
    lotes = livro.fatiar_lotes("y" * 1000, max_chars=300)
    assert len(lotes) == 4 and sum(len(x) for x in lotes) == 1000


def test_parece_escaneado_usa_mediana():
    assert livro.parece_escaneado([0, 3, 12, 5])            # livro só-imagem
    assert livro.parece_escaneado([1500, 0, 0, 0, 2000])    # capa digital não mascara
    assert not livro.parece_escaneado([1500, 1800, 900])    # digital de verdade


def test_montar_jobs_com_toc_segmenta_capitulos_reais():
    paginas = [f"texto da página {i + 1}" for i in range(10)]
    toc = [(1, "Introdução", 1), (2, "seção ignorada", 2), (1, "Método", 4), (1, "Conclusão", 8)]
    jobs = livro.montar_jobs("Meu Livro", paginas, toc)
    assert [j["titulo_cap"] for j in jobs] == ["Introdução", "Método", "Conclusão"]
    # "Método" cobre da própria página 4 até a ANTERIOR ao próximo capítulo (7).
    assert (jobs[1]["pagina_inicio"], jobs[1]["pagina_fim"]) == (4, 7)
    assert "página 5" in jobs[1]["texto"] and "página 8" not in jobs[1]["texto"]


def test_montar_jobs_sem_toc_cai_em_janelas():
    jobs = livro.montar_jobs("Sem TOC", [f"p{i} conteúdo" for i in range(25)], [], paginas_por_bloco=10)
    assert len(jobs) == 3
    assert (jobs[2]["pagina_inicio"], jobs[2]["pagina_fim"]) == (21, 25)


# ---------- processamento em idle --------------------------------------------
class FakeLlamaLivro:
    def __init__(self):
        self.chamadas = 0

    async def collect(self, prompt, **kwargs):
        self.chamadas += 1
        if "síntese corrida" in prompt:
            return "O capítulo argumenta que a disciplina supera a motivação."
        return ("## Disciplina vence motivação\nO autor afirma que hábitos batem vontade.\n"
                "**Malha Neural:** [[Hábitos]]\n#zettelkasten_atomico #conhecimento_novo")


class FakeVS:
    async def sync(self):
        pass


_PROSA_DE_CAPITULO = 3 * (
    "A célula eucariótica distingue-se da procariótica pela presença de um núcleo "
    "delimitado por membrana, onde o material genético fica separado do citoplasma "
    "por um envoltório nuclear com poros que regulam o trânsito de moléculas.\n\n"
    "As mitocôndrias, organelas responsáveis pela respiração celular, possuem DNA "
    "próprio e ribossomos semelhantes aos bacterianos, evidência central da teoria "
    "endossimbiótica formulada por Lynn Margulis para explicar a origem dessas "
    "estruturas a partir de bactérias ancestrais englobadas por células maiores.\n\n"
)


def _monta_ambiente(monkeypatch, tmp_path, n_jobs=2):
    vault = tmp_path / "vault"
    (vault / settings.subpasta_conhecimento_novo).mkdir(parents=True)
    monkeypatch.setattr(settings, "caminho_obsidian", str(vault))
    monkeypatch.setattr(settings, "dir_ingestao", str(tmp_path / "ingestao"))
    pend = tmp_path / "ingestao" / "pendentes"
    pend.mkdir(parents=True)
    for i in range(n_jobs):
        job = {"livro": "Meu Livro", "capitulo": i + 1, "titulo_cap": f"Cap {i + 1}",
               "pagina_inicio": 1 + 10 * i, "pagina_fim": 10 * (i + 1),
               # Prosa de tamanho REALISTA: um capítulo de verdade tem dezenas de
               # milhares de chars, e a triagem (que descarta aparato editorial)
               # barra texto curto demais para render ideia — com o fixture de duas
               # linhas de antes, este teste passava por um caminho que não existe.
               "texto": _PROSA_DE_CAPITULO}
        (pend / f"meu-livro__{i + 1:03d}.json").write_text(
            json.dumps(job, ensure_ascii=False), encoding="utf-8")
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlamaLivro()
    ctx.vectorstore = FakeVS()
    etl = EtlProcessor(ctx)

    async def _nao_varre(info):
        return None

    async def _nunca_dup(corpo):
        return False

    monkeypatch.setattr(etl, "_varredura_contradicoes", _nao_varre)
    monkeypatch.setattr(etl, "_ja_no_banco", _nunca_dup)
    return etl, ctx, pend, vault


async def test_capitulo_vira_atomos_com_proveniencia_e_sintese(monkeypatch, tmp_path):
    etl, ctx, pend, vault = _monta_ambiente(monkeypatch, tmp_path, n_jobs=1)
    n = await etl.ingestao_livros()
    assert n == 1
    assert list(pend.glob("*.json")) == []                       # saiu de pendentes
    assert len(list(pend.parent.glob("processados/*.json"))) == 1
    novos = list((vault / settings.subpasta_conhecimento_novo).glob("*.md"))
    corpo_total = "\n".join(a.read_text(encoding="utf-8") for a in novos)
    assert "Livro 'Meu Livro' — Cap 1 (p. 1-10)" in corpo_total  # proveniência
    assert any(a.name.startswith("LivroSintese_") for a in novos)  # nota-síntese (tese)


async def test_cap_por_ciclo_limita_e_o_resto_fica_pendente(monkeypatch, tmp_path):
    etl, ctx, pend, vault = _monta_ambiente(monkeypatch, tmp_path, n_jobs=3)
    monkeypatch.setattr(settings, "ingestao_caps_por_ciclo", 1)
    assert await etl.ingestao_livros() == 1
    assert len(list(pend.glob("*.json"))) == 2                   # fila durável


async def test_scheduler_nao_ingere_com_sessao_viva(monkeypatch, tmp_path):
    etl, ctx, pend, vault = _monta_ambiente(monkeypatch, tmp_path, n_jobs=1)
    sched = SchedulerService(ctx)
    from datetime import datetime
    agora = datetime(2026, 7, 25, 3, 0)
    assert sched._ingestao_devida(agora) is True                 # idle: devida
    ctx.sessoes.add(object())                                    # conversa aberta
    assert sched._ingestao_devida(agora) is False                # restrição do dono
    ctx.sessoes.clear()
    for j in pend.glob("*.json"):
        j.unlink()
    assert sched._ingestao_devida(agora) is False                # sem pendências
