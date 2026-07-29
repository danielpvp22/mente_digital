"""Duas edições da mesma obra: quem sai é a VELHA (2026-07-27).

Ordem do dono: a edição web de *The Cannabis Encyclopedia* é a atual e vence o
Cervantes antigo já no vault; e, havendo átomo duplicado, "apague o átomo antigo,
não o novo".

O defeito que isto conserta é silencioso e caro: o dedup do ETL descarta o átomo
que CHEGA quando já existe um quase idêntico — e quem chega é sempre a edição
nova. Importar o livro novo por cima do velho jogaria fora, fato a fato,
exatamente o que se queria trazer, sem uma linha de erro.
"""
import asyncio
from pathlib import Path

from mente_digital.config import settings
from mente_digital.etl import EtlProcessor
from mente_digital.state import AppContext

from conftest import FakeDoc

NOVA = "Livro 'The Cannabis Encyclopedia (Jorge Cervantes) — edição web' — Soil (p. 3-3)"
VELHA = "Livro 'Cervantes — Marijuana Horticulture' — Solo (p. 88-90)"
ATOMO = "## O pH do solo\nA faixa ideal fica entre 6,0 e 7,0 para a absorção de nutrientes."


class _StoreComVizinho:
    """Loja que sempre acha o MESMO vizinho, à distância pedida."""

    def __init__(self, doc, dist):
        self._doc, self._dist = doc, dist
        self._store = self
        self.removidas = []

    def similarity_search_with_score(self, corpo, k):
        return [(self._doc, self._dist)]

    async def remover_fontes(self, fontes):
        self.removidas.extend(fontes)


def _cenario(tmp_path, monkeypatch, origem_indexada, dist=0.005):
    """ETL apontando para um vault temporário, com uma nota antiga em disco."""
    vault = tmp_path / "vault"
    (vault / settings.subpasta_conhecimento_novo).mkdir(parents=True)
    antiga = vault / settings.subpasta_conhecimento_novo / "antiga.md"
    antiga.write_text("## O pH do solo\nversão velha\n", encoding="utf-8")
    monkeypatch.setattr(settings, "caminho_obsidian", str(vault))
    monkeypatch.setattr(settings, "dir_aposentados", str(tmp_path / "aposentados"))
    monkeypatch.setattr(settings, "contradicao_detectar", False)
    doc = FakeDoc("versão velha", {"source": str(antiga), "origem": origem_indexada})
    ctx = AppContext(settings=settings)
    ctx.vectorstore = _StoreComVizinho(doc, dist)
    return EtlProcessor(ctx), antiga, ctx


def _novos_atomos(vault: Path) -> list:
    pasta = Path(vault) / settings.subpasta_conhecimento_novo
    return [p for p in pasta.glob("*.md") if p.name != "antiga.md"]


def test_edicao_preferida_aposenta_o_atomo_antigo(tmp_path, monkeypatch):
    etl, antiga, ctx = _cenario(tmp_path, monkeypatch, VELHA)

    salvos = asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA))

    assert salvos == 1                                   # o novo ENTROU
    assert not antiga.exists()                           # e o velho saiu do vault
    assert (Path(settings.dir_aposentados) / "antiga.md").is_file()   # não foi destruído
    assert ctx.vectorstore.removidas == [str(antiga)]    # e saiu do índice na hora
    assert len(_novos_atomos(settings.caminho_obsidian)) == 1


def test_duplicata_da_MESMA_obra_segue_sendo_descartada(tmp_path, monkeypatch):
    """Sem troca de edição, o dedup continua como sempre foi: o novo é o que cai."""
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, NOVA)

    salvos = asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA))

    assert salvos == 0
    assert antiga.exists()
    assert _novos_atomos(settings.caminho_obsidian) == []


def test_obra_antiga_nao_derruba_a_nova(tmp_path, monkeypatch):
    """O sentido importa: reimportar o livro VELHO não pode apagar o novo."""
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, NOVA)

    salvos = asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=VELHA))

    assert salvos == 0 and antiga.exists()


def test_atomo_de_OUTRO_LIVRO_nunca_e_aposentado(tmp_path, monkeypatch):
    """O defeito que rodou de verdade: um átomo do Raven a 0,06 do átomo novo
    era aposentado porque 'a obra que chega é preferida'. Agora a que sai tem de
    estar declarada como superada — e o conhecimento do outro livro fica."""
    outro = "Livro 'Biologia Vegetal - Raven (8ª Edição)' — Estômatos (p. 12-14)"
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, outro, dist=BANDA)

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 1
    assert antiga.exists()                                   # o Raven continua lá
    assert not Path(settings.dir_aposentados).exists()


def test_nota_SEM_origem_do_dono_nunca_e_aposentada(tmp_path, monkeypatch):
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, "", dist=BANDA)

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 1
    assert antiga.exists()


def test_sem_lista_de_superadas_nada_e_aposentado(tmp_path, monkeypatch):
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, VELHA, dist=BANDA)
    monkeypatch.setattr(settings, "obras_substituidas", "")

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 1
    assert antiga.exists()


# --- contradição: a informação NOVA prevalece sobre a edição superada ---------
class _Llama:
    """LLM de fundo que sempre acusa contradição (o veredito é do `contradicao`)."""

    def __init__(self, veredito):
        self.veredito = veredito

    async def collect(self, *a, **kw):
        return self.veredito


def _com_contradicao(tmp_path, monkeypatch, origem_indexada, veredito):
    etl, antiga, ctx = _cenario(tmp_path, monkeypatch, origem_indexada, dist=0.20)
    monkeypatch.setattr(settings, "contradicao_detectar", True)
    # a banda da contradição é [dedup_dist_max, contradicao_dist_max): 0,20 cai nela
    monkeypatch.setattr(settings, "contradicao_dist_max", 0.5)
    ctx.llama = _Llama(veredito)
    ctx.interactive_idle.set()
    return etl, antiga


def test_contradicao_com_a_edicao_superada_aposenta_a_antiga(tmp_path, monkeypatch):
    etl, antiga = _com_contradicao(tmp_path, monkeypatch, VELHA,
                                   "SIM: a faixa de pH indicada é outra")

    asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA))

    assert not antiga.exists()
    assert (Path(settings.dir_aposentados) / "antiga.md").is_file()


def test_contradicao_com_OUTRO_LIVRO_so_e_relatada(tmp_path, monkeypatch):
    """Duas fontes independentes que discordam é assunto do dono: as duas podem
    estar certas em contextos diferentes, e não há edição velha para aposentar."""
    outro = "Livro 'Biologia Vegetal - Raven (8ª Edição)' — Estômatos (p. 12-14)"
    etl, antiga = _com_contradicao(tmp_path, monkeypatch, outro,
                                   "SIM: afirmam o contrário sobre estômatos")

    asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA))

    assert antiga.exists()


def test_sem_contradicao_a_edicao_velha_fica(tmp_path, monkeypatch):
    etl, antiga = _com_contradicao(tmp_path, monkeypatch, VELHA, "NAO")

    asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA))

    assert antiga.exists()


def test_botao_desliga_a_resolucao_automatica(tmp_path, monkeypatch):
    etl, antiga = _com_contradicao(tmp_path, monkeypatch, VELHA, "SIM: contradizem")
    monkeypatch.setattr(settings, "contradicao_resolver_por_obra", False)

    asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA))

    assert antiga.exists()


def test_sem_preferencia_configurada_nada_e_aposentado(tmp_path, monkeypatch):
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, VELHA)
    monkeypatch.setattr(settings, "obras_preferidas", "")

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 0
    assert antiga.exists()


def test_atomo_distinto_nem_chega_ao_desempate(tmp_path, monkeypatch):
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, VELHA, dist=0.5)

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 1
    assert antiga.exists()          # não era duplicata: ninguém é aposentado


# --- a BANDA DA SUBSTITUIÇÃO (0,01 <= dist < 0,08) ---------------------------
# Medido no cap. 18: a mesma ideia nas duas edições fica a 0,042-0,128 do gêmeo,
# uma ordem de grandeza acima do `dedup_dist_max`. Com uma régua só, a precedência
# de obra nunca disparava (0 substituições em 249 átomos). Aqui é a régua nova.
BANDA = 0.06


def test_edicao_preferida_troca_na_banda_frouxa(tmp_path, monkeypatch):
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, VELHA, dist=BANDA)

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 1
    assert not antiga.exists()                                   # a velha saiu
    assert (Path(settings.dir_aposentados) / "antiga.md").is_file()


def test_na_banda_SEM_preferencia_o_atomo_novo_e_SALVO(tmp_path, monkeypatch):
    """A régua frouxa não pode virar um descarte novo: fora da troca de edição,
    0,06 continua sendo 'átomo distinto' e o conhecimento entra."""
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, NOVA, dist=BANDA)

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 1
    assert antiga.exists()          # e a antiga fica: ninguém foi aposentado


def test_acima_da_banda_ninguem_e_aposentado(tmp_path, monkeypatch):
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, VELHA, dist=0.20)

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 1
    assert antiga.exists()


def test_botao_zerado_volta_a_reger_so_pelo_dedup_estrito(tmp_path, monkeypatch):
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, VELHA, dist=BANDA)
    monkeypatch.setattr(settings, "obras_dedup_dist_max", 0.0)

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 1
    assert antiga.exists()          # 0,06 está fora do dedup estrito: nada acontece


def test_nota_ja_sumida_do_disco_nao_vira_perda_do_atomo_novo(tmp_path, monkeypatch):
    """A nota pode ter sido apagada à mão entre a indexação e agora. Se não dá
    para aposentar, o comportamento seguro é o histórico: descarta o novo."""
    etl, antiga, _ctx = _cenario(tmp_path, monkeypatch, VELHA)
    antiga.unlink()

    assert asyncio.run(etl._salvar_atomos(ATOMO, "Livro", "TESTE", origem=NOVA)) == 0
