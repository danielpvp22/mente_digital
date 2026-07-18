"""
Pesquisa proativa no idle: capturar LACUNAS (RAM+banco falharam), pesquisá-las na web,
atomizar e inserir SEM duplicar. Sem GPU nem rede — fakes de LLM/web/store.
"""
import asyncio

import pytest

from agent import EtlProcessor
from config import settings
from rag import NENHUM
from state import AppContext
from telemetry import Database


# --- lacunas no SQLite ------------------------------------------------------
def _db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init()
    return d


def test_lacuna_upsert_acumula_a_frequencia(tmp_path):
    d = _db(tmp_path)
    d.save_lacuna("tensorrt yolo", "TensorRT YOLO")
    d.save_lacuna("tensorrt yolo", "TensorRT YOLO")     # mesma dúvida, de novo
    d.save_lacuna("guerra canudos", "Guerra de Canudos")

    lac = d.get_lacunas()
    assert lac[0]["termos"] == "TensorRT YOLO"          # a mais frequente primeiro
    assert lac[0]["n"] == 2
    assert {x["termos"] for x in lac} == {"TensorRT YOLO", "Guerra de Canudos"}


def test_lacuna_pesquisada_sai_da_fila(tmp_path):
    d = _db(tmp_path)
    d.save_lacuna("x", "X")
    d.marcar_lacuna_pesquisada("x")
    assert d.get_lacunas() == []                        # já trazida: não re-pesquisa


def test_lacuna_vazia_e_ignorada(tmp_path):
    d = _db(tmp_path)
    d.save_lacuna("", "")
    assert d.get_lacunas() == []


# --- pesquisa proativa (EtlProcessor) ---------------------------------------
class _Local:
    def __init__(self, relevante): self.relevante = relevante; self.melhor_dist = 0.3


class _Store:
    def __init__(self, relevante): self._relevante = relevante; self._store = None; self.sincronizou = False
    async def search(self, *a, **k): return _Local(self._relevante)
    async def sync(self): self.sincronizou = True


class _Web:
    def __init__(self, dados): self.dados = dados; self.chamou = []
    async def search(self, termo, consulta=None): self.chamou.append(termo); return self.dados


class _Llama:
    async def collect(self, *a, **k):
        return "## Fato novo sobre TensorRT\nO TensorRT otimiza a engine YOLO."


def _etl(tmp_path, monkeypatch, *, cobre, dados="corpo web real com fatos"):
    from telemetry import db as db_global
    d = _db(tmp_path)
    # aponta o db global (que o EtlProcessor usa) para o de teste
    monkeypatch.setattr("agent.db", d)
    ctx = AppContext(settings=settings)
    ctx.interactive_idle.set()
    ctx.vectorstore = _Store(relevante=cobre)
    ctx.web = _Web(dados)
    ctx.llama = _Llama()
    etl = EtlProcessor(ctx)
    # isola o alvo: sem dedup real nem escrita em disco
    saved = []
    async def _fake_salvar(texto, prefixo, tipo):
        saved.append((texto, prefixo)); return 1
    monkeypatch.setattr(etl, "_salvar_atomos", _fake_salvar)
    return etl, d, ctx, saved


def test_proativa_pesquisa_lacuna_ausente_do_banco(tmp_path, monkeypatch):
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch, cobre=False)
    d.save_lacuna("tensorrt yolo", "TensorRT YOLO")

    asyncio.run(etl.pesquisa_proativa())

    assert ctx.web.chamou == ["TensorRT YOLO"]          # buscou na web
    assert len(saved) == 1                               # e atomizou
    assert d.get_lacunas() == []                         # marcada como resolvida


def test_proativa_NAO_repesquisa_o_que_o_banco_ja_cobre(tmp_path, monkeypatch):
    # Nível 1 de anti-duplicação: outra passada já trouxe, ou a base cresceu.
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch, cobre=True)
    d.save_lacuna("tensorrt yolo", "TensorRT YOLO")

    asyncio.run(etl.pesquisa_proativa())

    assert ctx.web.chamou == []                          # não gastou busca web
    assert saved == []
    assert d.get_lacunas() == []                         # mas some da fila (resolvida)


def test_proativa_respeita_o_teto_por_ciclo(tmp_path, monkeypatch):
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch, cobre=False)
    monkeypatch.setattr(settings, "idle_pesquisa_max", 2)
    for i in range(5):
        d.save_lacuna(f"lacuna{i}", f"Lacuna {i}")

    asyncio.run(etl.pesquisa_proativa())

    assert len(saved) == 2                               # parou no teto


def test_proativa_web_vazia_nao_atomiza(tmp_path, monkeypatch):
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch, cobre=False, dados=NENHUM)
    d.save_lacuna("nada na web", "Nada na Web")

    asyncio.run(etl.pesquisa_proativa())

    assert saved == []                                   # web sem resultado -> não inventa
    assert d.get_lacunas() == []                         # mas marca (não fica retentando)


def test_proativa_desligada_nao_faz_nada(tmp_path, monkeypatch):
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch, cobre=False)
    monkeypatch.setattr(settings, "idle_pesquisa_proativa", False)
    d.save_lacuna("x", "X")

    asyncio.run(etl.pesquisa_proativa())

    assert ctx.web.chamou == []
    assert saved == []


# --- dedup por átomo (_ja_no_banco / _salvar_atomos) ------------------------
class _StoreComVizinho:
    def __init__(self, dist): self._dist = dist; self._store = self
    def similarity_search_with_score(self, corpo, k):
        return [("doc-existente", self._dist)]


def test_ja_no_banco_pega_quase_identico(tmp_path, monkeypatch):
    ctx = AppContext(settings=settings)
    ctx.vectorstore = _StoreComVizinho(dist=0.02)        # < dedup_dist_max
    etl = EtlProcessor(ctx)
    assert asyncio.run(etl._ja_no_banco("um átomo qualquer")) is True


def test_ja_no_banco_deixa_passar_o_distinto(tmp_path):
    ctx = AppContext(settings=settings)
    ctx.vectorstore = _StoreComVizinho(dist=0.5)         # bem distante -> não é dup
    etl = EtlProcessor(ctx)
    assert asyncio.run(etl._ja_no_banco("átomo distinto")) is False


def test_ja_no_banco_fail_open_sem_embeddings(tmp_path):
    # Sem loja (testes): dedup não pode virar bloqueio de escrita.
    ctx = AppContext(settings=settings)
    ctx.vectorstore = None
    etl = EtlProcessor(ctx)
    assert asyncio.run(etl._ja_no_banco("qualquer")) is False


# --- lacuna trivial não é pesquisada (bug do 'ok' em produção) --------------
def test_proativa_pula_lacuna_trivial(tmp_path, monkeypatch):
    # 'ok' (0 keywords, falso-positivo do VAD/Whisper) escalou e a proativa pesquisou
    # a ETIMOLOGIA de "ok" — 8 átomos-lixo. O backstop marca e não pesquisa.
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch, cobre=False)
    d.save_lacuna("ok", "ok")

    asyncio.run(etl.pesquisa_proativa())

    assert ctx.web.chamou == []                          # não gastou busca web em 'ok'
    assert saved == []                                   # não escreveu átomo
    assert d.get_lacunas() == []                         # marcada, sai da fila


# --- lacuna_pesquisavel: núcleo substantivo ---------------------------------
def test_lacuna_pesquisavel_distingue_lixo_de_assunto_real():
    from agent import lacuna_pesquisavel
    # trivial (0 keywords) e sem-núcleo (moeda + número) -> NÃO pesquisa
    assert lacuna_pesquisavel("ok") is False
    assert lacuna_pesquisavel("dolar 542") is False          # tira num+moeda -> vazio
    # pergunta técnica que SÓ MENCIONA cripto -> núcleo {protocolo,stratum,mineracao}
    # (aplicar e_efemero cru mataria esta — é o ponto do NÚCLEO em vez do gate cru)
    assert lacuna_pesquisavel("protocolo stratum v2 mineracao bitcoin") is True
    assert lacuna_pesquisavel("itens lista compra projeto esp32") is True
    # número CENTRAL não vira lixo se há assunto junto ('crise' não é efêmero)
    assert lacuna_pesquisavel("crise economica 1929") is True


def test_lacuna_pesquisavel_e_secundario_ao_efemero():
    # 'euro hoje' passa por lacuna_pesquisavel (núcleo={hoje}), MAS na escalada é
    # barrado antes pelo `not efemero` da pergunta ORIGINAL (euro é gatilho). Este
    # filtro é o 2º nível — resolve o que o efêmero não pega (número+moeda), não o
    # que ele já pega. Documenta a divisão de trabalho.
    import tools
    from agent import lacuna_pesquisavel
    assert tools.e_efemero("euro hoje") is True              # o efêmero pega
    assert lacuna_pesquisavel("euro hoje") is True           # o núcleo, sozinho, não
