"""
#4 Re-pesquisa de TEMAS QUENTES: além das LACUNAS (o que faltou), o idle também
re-pesquisa os temas que o usuário MAIS REUSA do vault (o Banco respondeu de fato) —
trazendo NOVIDADE via web, com o dedup por átomo descartando o que já se sabe. Ao
contrário da lacuna, aqui o banco JÁ cobre o tema; por isso NÃO há o skip Nível-1.
Sem GPU nem rede — fakes de LLM/web/store.
"""
import asyncio


from mente_digital.agent import EtlProcessor
from mente_digital.config import settings
from mente_digital.rag import NENHUM
from mente_digital.state import AppContext
from mente_digital.telemetry import Database


def _db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init()
    return d


# --- tabela temas_quentes no SQLite -----------------------------------------
def test_tema_quente_upsert_acumula_reuso(tmp_path):
    d = _db(tmp_path)
    d.save_tema_quente("rag em nicho", "RAG em nicho")
    d.save_tema_quente("rag em nicho", "RAG em nicho")   # reusou de novo
    d.save_tema_quente("yolo redes", "YOLO redes")

    temas = d.get_temas_quentes(min_reuso=1)
    assert temas[0]["termos"] == "RAG em nicho"          # o mais reusado primeiro
    assert temas[0]["n"] == 2


def test_tema_quente_respeita_min_reuso(tmp_path):
    d = _db(tmp_path)
    d.save_tema_quente("pouco reuso", "Pouco reuso")     # n=1
    assert d.get_temas_quentes(min_reuso=3) == []        # abaixo do piso: não entra
    d.save_tema_quente("pouco reuso", "Pouco reuso")
    d.save_tema_quente("pouco reuso", "Pouco reuso")     # agora n=3
    assert [t["termos"] for t in d.get_temas_quentes(min_reuso=3)] == ["Pouco reuso"]


def test_tema_pesquisado_sai_pela_janela_de_cooldown(tmp_path):
    d = _db(tmp_path)
    d.save_tema_quente("x", "X")
    d.save_tema_quente("x", "X")
    d.save_tema_quente("x", "X")                          # n=3
    d.marcar_tema_pesquisado("x")
    # dentro do cooldown: não re-pesquisa
    assert d.get_temas_quentes(min_reuso=1, nao_pesquisadas_ha_dias=14) == []
    # cooldown zero: já pode de novo (para o operador que quiser agressivo)
    assert [t["termos"] for t in d.get_temas_quentes(min_reuso=1, nao_pesquisadas_ha_dias=0)] == ["X"]


def test_tema_quente_vazio_e_ignorado(tmp_path):
    d = _db(tmp_path)
    d.save_tema_quente("", "")
    assert d.get_temas_quentes(min_reuso=1) == []


# --- pesquisa_temas_quentes (EtlProcessor) ----------------------------------
class _Store:
    def __init__(self): self._store = None; self.sincronizou = False
    async def sync(self): self.sincronizou = True


class _Web:
    def __init__(self, dados): self.dados = dados; self.chamou = []
    async def search(self, termo, consulta=None, on_colheita=None): self.chamou.append(termo); return self.dados


class _Llama:
    async def collect(self, *a, **k):
        return "## Novidade sobre RAG\nUm avanço recente em RAG de nicho."


def _etl(tmp_path, monkeypatch, *, dados="corpo web real com fatos novos"):
    from mente_digital.telemetry import db as _  # noqa: F401  (garante que o módulo carregou)
    d = _db(tmp_path)
    monkeypatch.setattr("mente_digital.etl.db", d)                     # o EtlProcessor usa etl.db
    ctx = AppContext(settings=settings)
    ctx.interactive_idle.set()
    ctx.vectorstore = _Store()
    ctx.web = _Web(dados)
    ctx.llama = _Llama()
    etl = EtlProcessor(ctx)
    saved = []
    async def _fake_salvar(texto, prefixo, tipo):
        saved.append((texto, prefixo, tipo)); return 1
    monkeypatch.setattr(etl, "_salvar_atomos", _fake_salvar)
    return etl, d, ctx, saved


def _hot(d, chave, termos, n=3):
    for _ in range(n):
        d.save_tema_quente(chave, termos)


def test_repesquisa_tema_mesmo_com_banco_cobrindo(tmp_path, monkeypatch):
    # O CERNE do #4: ao contrário da lacuna, NÃO pula porque o banco cobre — o valor
    # é a NOVIDADE que a web traz (o dedup por átomo, aqui fakeado, filtra o já-sabido).
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch)
    _hot(d, "rag em nicho", "RAG em nicho")

    asyncio.run(etl.pesquisa_temas_quentes())

    assert ctx.web.chamou == ["RAG em nicho"]             # buscou na web
    assert len(saved) == 1                                # e atomizou a novidade
    assert saved[0][1] == "TemaQuente"                    # prefixo/origem própria
    assert d.get_temas_quentes(min_reuso=1) == []         # entrou em cooldown


def test_repesquisa_respeita_o_teto_por_ciclo(tmp_path, monkeypatch):
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "idle_temas_max", 2)
    monkeypatch.setattr(settings, "idle_temas_min_reuso", 1)
    for i in range(5):
        _hot(d, f"tema{i}", f"Tema {i}", n=1)

    asyncio.run(etl.pesquisa_temas_quentes())

    assert len(saved) == 2                                # parou no teto


def test_repesquisa_web_vazia_nao_atomiza_mas_entra_cooldown(tmp_path, monkeypatch):
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch, dados=NENHUM)
    _hot(d, "sem novidade", "Sem novidade")

    asyncio.run(etl.pesquisa_temas_quentes())

    assert saved == []                                    # web sem resultado -> não inventa
    assert d.get_temas_quentes(min_reuso=1) == []         # mas marca (não fica retentando)


def test_repesquisa_desligada_nao_faz_nada(tmp_path, monkeypatch):
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "idle_pesquisa_temas", False)
    _hot(d, "x", "X")

    asyncio.run(etl.pesquisa_temas_quentes())

    assert ctx.web.chamou == []
    assert saved == []


def test_repesquisa_pula_tema_trivial(tmp_path, monkeypatch):
    # Mesmo backstop da lacuna: 'ok' (0 keywords) não vira busca web nem átomo.
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "idle_temas_min_reuso", 1)
    _hot(d, "ok", "ok", n=1)

    asyncio.run(etl.pesquisa_temas_quentes())

    assert ctx.web.chamou == []
    assert saved == []
    assert d.get_temas_quentes(min_reuso=1) == []         # marcado, sai da fila


def test_repesquisa_so_pega_favoritos_de_verdade(tmp_path, monkeypatch):
    # Piso de reuso: um tema perguntado uma única vez (n=1) não é "favorito".
    etl, d, ctx, saved = _etl(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "idle_temas_min_reuso", 3)
    _hot(d, "curioso uma vez", "Curioso uma vez", n=1)

    asyncio.run(etl.pesquisa_temas_quentes())

    assert ctx.web.chamou == []                           # n=1 < 3: não re-pesquisa
    assert saved == []
