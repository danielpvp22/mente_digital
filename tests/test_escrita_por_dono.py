"""
Quem ESCREVE tem de escolher a pasta — e quem LÊ só alcança o próprio escopo.

Com o multiusuário, a fronteira de privacidade deixou de ser um filtro de metadado e
passou a ser a PASTA/COLEÇÃO (ver o bloco `multiusuario_ligado` em rag.py). Isso
transferiu uma obrigação nova para os dois módulos que GRAVAM no vault:

- `etl.py` — o átomo colhido de conversa/web/pre-fetch é do DONO daquele turno
  (`Pessoal/<dono>/`); só a ingestão de OBRA vai para o `Acervo/` comum.
- `tools.py` — `listar_notas`/`ler_nota` faziam `glob` no vault INTEIRO, passando ao
  largo do Chroma: nenhum filtro de índice as alcançava, e a auditoria marcou como
  vazamento direto entre usuários. Nota, lista e captura escreviam num caminho único
  na raiz, então a lista de compras de um era literalmente o arquivo do outro.

Estes testes cobrem os dois lados do contrato, e o mais importante deles é o último
bloco: com `multiusuario_habilitado=False` (o default) TUDO tem de continuar caindo
exatamente onde caía. Tudo com fakes — sem GPU, sem rede, sem Chroma.
"""
import os
from pathlib import Path

import pytest

from mente_digital import identidade
from mente_digital import tools
from mente_digital.config import Settings, settings
from mente_digital.etl import (EtlProcessor, append_chat_dump, caminho_chat_dump,
                               donos_do_ciclo, raiz_dos_atomos)
from mente_digital.state import AppContext

from conftest import FakeDoc, FakeLlama


# ==========================================================================
# Cenário: um vault de tmp_path com dois donos e o acervo comum
# ==========================================================================
@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Aponta o `settings` GLOBAL para um vault temporário.

    O global, e não uma `Settings` nova: `etl`, `tools` e `rag` importam o mesmo
    objeto, e o gate `multiusuario_ligado` o lê a cada chamada. Uma instância
    separada faria o gate responder por um vault e os caminhos por outro — a suíte
    ficaria verde medindo dois mundos diferentes."""
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault"))
    monkeypatch.setattr(settings, "arquivo_chat_dump", str(tmp_path / "dump.md"))
    for p in (settings.caminho_acervo, settings.dir_figuras,
              settings.caminho_pessoal("ana"), settings.caminho_pessoal("bruno")):
        p.mkdir(parents=True, exist_ok=True)
    return tmp_path / "vault"


@pytest.fixture
def multi(vault, monkeypatch):
    """O vault acima, com o multiusuário LIGADO."""
    monkeypatch.setattr(settings, "multiusuario_habilitado", True)
    return vault


class FakeVectorStore:
    """VectorStore falso com as DUAS portas que o ETL usa: a crua (`_store`, o modo
    de um usuário só) e a pública (`recuperar`, que faz o fan-in acervo+pessoal)."""

    def __init__(self, cru=(), por_escopo=None) -> None:
        self._store = self          # basta ser não-None para o ETL considerar "há loja"
        self.cru = list(cru)
        self.por_escopo = por_escopo or {}
        self.syncs = 0
        self.usou_recuperar = 0

    def similarity_search_with_score(self, texto, k):     # a porta CRUA
        return self.cru[:k]

    async def recuperar(self, consulta, k=None):          # a porta PÚBLICA (fan-in)
        self.usou_recuperar += 1
        dono = identidade.dono_atual() or ""
        achados = list(self.por_escopo.get("", [])) + list(self.por_escopo.get(dono, []))
        achados.sort(key=lambda ds: ds[1])
        return achados[: (k or 1)]

    async def sync(self):
        self.syncs += 1


def _etl(monkeypatch, vectorstore=None, resposta="") -> EtlProcessor:
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama([resposta])
    ctx.vectorstore = vectorstore or FakeVectorStore()
    monkeypatch.setattr("mente_digital.etl.db.log_etl", lambda *a, **k: None)
    return EtlProcessor(ctx)


_ATOMO = "## TensorRT\nAcelera o YOLO.\n#zettelkasten_atomico #conhecimento_novo\n"


def _achar(raiz: Path, prefixo: str):
    return sorted(p for p in raiz.rglob(f"{prefixo}_*.md"))


# ==========================================================================
# etl.py — o átomo colhido é de QUEM conversou
# ==========================================================================
async def test_atomo_da_conversa_cai_no_vault_pessoal(multi, monkeypatch):
    etl = _etl(monkeypatch)
    with identidade.usar_dono("ana"):
        assert await etl._salvar_atomos(_ATOMO, "Conversa", "IDLE_CONVERSA") == 1

    assert len(_achar(settings.caminho_pessoal("ana"), "Conversa")) == 1
    # e NADA no acervo comum nem no vault do outro
    assert _achar(settings.caminho_acervo, "Conversa") == []
    assert _achar(settings.caminho_pessoal("bruno"), "Conversa") == []


async def test_atomo_de_livro_cai_no_acervo_comum(multi, monkeypatch):
    # Livro é BIBLIOTECA: os quatro leem. Mesmo colhido enquanto a Ana conversava, o
    # átomo de obra não é memória dela.
    etl = _etl(monkeypatch)
    with identidade.usar_dono("ana"):
        assert await etl._salvar_atomos(_ATOMO, "Livro", "INGESTAO_LIVRO", acervo=True) == 1

    assert len(_achar(settings.caminho_acervo, "Livro")) == 1
    assert _achar(settings.caminho_pessoal("ana"), "Livro") == []


async def test_atomo_sem_dono_falha_alto_em_vez_de_escolher(multi, monkeypatch):
    """O ⚠ de `identidade`: herdar "o último dono" escreveria a memória de uma pessoa
    na pasta de outra, e isso não tem desfazer. Falhar é o desfecho BOM."""
    etl = _etl(monkeypatch)
    with pytest.raises(identidade.DonoIndefinido):
        await etl._salvar_atomos(_ATOMO, "Conversa", "IDLE_CONVERSA")
    assert list((multi).rglob("Conversa_*.md")) == []      # nada escrito pela metade


async def test_dump_da_conversa_e_por_dono(multi):
    with identidade.usar_dono("ana"):
        await append_chat_dump("User", "pergunta da ana")
        caminho_ana = caminho_chat_dump()
    with identidade.usar_dono("bruno"):
        await append_chat_dump("User", "pergunta do bruno")
        caminho_bruno = caminho_chat_dump()

    assert caminho_ana != caminho_bruno
    assert "ana" in Path(caminho_ana).read_text(encoding="utf-8")
    assert "bruno" not in Path(caminho_ana).read_text(encoding="utf-8")


async def test_donos_do_ciclo_ve_pasta_e_dump(multi):
    # A pasta de `Pessoal/` diz quem existe; o dump pega o usuário NOVO, que conversa
    # antes de ter pasta (ela nasce na 1ª escrita). Sem a segunda metade, a primeira
    # conversa dele nunca seria atomizada.
    with identidade.usar_dono("carla"):
        await append_chat_dump("User", "primeira conversa da carla")

    assert donos_do_ciclo() == ["ana", "bruno", "carla"]


async def test_idle_roda_uma_passada_por_dono(multi, monkeypatch):
    etl = _etl(monkeypatch)
    vistos = []

    async def tarefa():
        vistos.append(identidade.dono_atual())

    await etl._por_dono(tarefa)
    assert vistos == ["ana", "bruno"]


async def test_falha_de_um_dono_nao_mata_a_passada_dos_outros(multi, monkeypatch):
    """Um vault pessoal corrompido não pode deixar os outros três sem idle para
    sempre — daí o `except` ser POR DONO (e ir alto no log, nada engolido)."""
    etl = _etl(monkeypatch)
    vistos = []

    async def tarefa():
        dono = identidade.dono_atual()
        vistos.append(dono)
        if dono == "ana":
            raise OSError("vault da ana ilegível")

    erros = []
    monkeypatch.setattr("mente_digital.etl.telemetry.error",
                        lambda *a, **k: erros.append(a))
    await etl._por_dono(tarefa)
    assert vistos == ["ana", "bruno"]     # o bruno rodou mesmo com a ana quebrada
    assert erros                          # e a falha da ana foi REPORTADA


# ==========================================================================
# etl.py — o dedup compara contra acervo + vault pessoal, não só o acervo
# ==========================================================================
async def test_dedup_enxerga_o_vault_pessoal_do_dono(multi, monkeypatch):
    """Ir direto ao `_store` compararia o átomo de um dono só contra o ACERVO: a mesma
    ideia entraria de novo a cada passada de idle, porque o dedup não veria a nota
    pessoal onde ela já está."""
    do_acervo = (FakeDoc("um fato de livro", {"source": "/acervo/x.md"}), 0.42)
    da_ana = (FakeDoc("acelera o yolo", {"source": "/ana/y.md"}), 0.001)
    vs = FakeVectorStore(cru=[do_acervo], por_escopo={"": [do_acervo], "ana": [da_ana]})
    etl = _etl(monkeypatch, vectorstore=vs)

    with identidade.usar_dono("ana"):
        viz = await etl._vizinho_proximo("acelera o yolo")

    assert vs.usou_recuperar == 1                       # passou pela porta com fan-in
    assert viz is not None and viz[0].metadata["source"] == "/ana/y.md"


async def test_atomo_ja_no_vault_pessoal_e_descartado(multi, monkeypatch):
    dup = (FakeDoc("acelera o yolo", {"source": "/ana/y.md"}), 0.0)
    vs = FakeVectorStore(por_escopo={"ana": [dup]})
    etl = _etl(monkeypatch, vectorstore=vs)

    with identidade.usar_dono("ana"):
        salvos = await etl._salvar_atomos(_ATOMO, "Conversa", "IDLE_CONVERSA")

    assert salvos == 0
    assert _achar(settings.caminho_pessoal("ana"), "Conversa") == []


async def test_vizinho_relacionado_usa_a_mesma_porta(multi, monkeypatch):
    """Eram duas cópias abrindo o Chroma cru por conta própria. A banda da contradição
    muda; a CONSULTA (vizinho nº 1 do escopo) é a mesma."""
    dist = (settings.dedup_dist_max + settings.contradicao_dist_max) / 2
    perto = (FakeDoc("tema parecido", {"source": "/ana/z.md"}), dist)
    vs = FakeVectorStore(por_escopo={"ana": [perto]})
    etl = _etl(monkeypatch, vectorstore=vs)

    with identidade.usar_dono("ana"):
        viz = await etl._vizinho_relacionado("um corpo qualquer")

    assert vs.usou_recuperar == 1
    assert viz is not None and viz[0].metadata["source"] == "/ana/z.md"


# ==========================================================================
# tools.py — ler o próprio escopo, nunca o do vizinho
# ==========================================================================
def _semear_notas() -> None:
    (settings.caminho_acervo / "Fotossintese.md").write_text("# Fotossíntese\ncomum",
                                                             encoding="utf-8")
    (settings.dir_figuras / "Figura_estomato.md").write_text("# Figura estomato\nfig",
                                                             encoding="utf-8")
    (settings.caminho_pessoal("ana") / "Diario_da_ana.md").write_text(
        "# Diario da ana\nsegredo da ana", encoding="utf-8")
    (settings.caminho_pessoal("bruno") / "Diario_do_bruno.md").write_text(
        "# Diario do bruno\nsegredo do bruno", encoding="utf-8")


async def test_listar_notas_nao_mostra_a_nota_do_outro(multi):
    _semear_notas()
    with identidade.usar_dono("ana"):
        resposta = await tools._t_listar_notas({}, None)

    assert "Diario_da_ana" in resposta          # a dela
    assert "Fotossintese" in resposta           # o acervo comum
    assert "Figura_estomato" in resposta        # Figuras/ é escopo do acervo
    assert "Diario_do_bruno" not in resposta    # a do vizinho, NÃO


async def test_ler_nota_nao_abre_a_nota_do_outro(multi):
    _semear_notas()
    with identidade.usar_dono("ana"):
        alheia = await tools._t_ler_nota({"titulo": "Diario do bruno"}, None)
        comum = await tools._t_ler_nota({"titulo": "Fotossintese"}, None)

    assert "segredo do bruno" not in alheia
    assert "comum" in comum                     # o acervo continua alcançável


async def test_sem_dono_so_o_acervo(multi):
    """Ausência de dono NEGA o dado pessoal em vez de adivinhar de quem ele é —
    degrada para MENOS informação, nunca para a de outra pessoa."""
    _semear_notas()
    resposta = await tools._t_listar_notas({}, None)

    assert "Fotossintese" in resposta
    assert "Diario_da_ana" not in resposta and "Diario_do_bruno" not in resposta


# ==========================================================================
# tools.py — nota, lista e captura caem na pasta de quem pediu
# ==========================================================================
class FakeCtx:
    def __init__(self) -> None:
        self.sujo = 0

    def marcar_vault_sujo(self) -> None:
        self.sujo += 1


async def test_lista_de_compras_de_um_nao_e_a_do_outro(multi):
    ctx = FakeCtx()
    with identidade.usar_dono("ana"):
        await tools._t_adicionar_item({"lista": "compras", "item": "leite"}, ctx)
    with identidade.usar_dono("bruno"):
        await tools._t_adicionar_item({"lista": "compras", "item": "cerveja"}, ctx)
        do_bruno = await tools._t_ler_lista({"lista": "compras"}, ctx)
    with identidade.usar_dono("ana"):
        da_ana = await tools._t_ler_lista({"lista": "compras"}, ctx)

    assert "leite" in da_ana and "cerveja" not in da_ana
    assert "cerveja" in do_bruno and "leite" not in do_bruno


async def test_captura_rapida_cai_na_inbox_do_dono(multi):
    ctx = FakeCtx()
    with identidade.usar_dono("ana"):
        await tools._t_capturar({"texto": "ideia da ana"}, ctx)
        inbox = tools.arquivo_inbox()

    assert inbox.parent == settings.caminho_pessoal("ana")
    assert "ideia da ana" in inbox.read_text(encoding="utf-8")
    assert not (settings.caminho_pessoal("bruno") / inbox.name).exists()


async def test_salvar_nota_cai_na_pasta_do_dono(multi):
    ctx = FakeCtx()
    with identidade.usar_dono("bruno"):
        await tools._t_salvar_nota({"titulo": "Ideia", "conteudo": "só do bruno"}, ctx)

    assert list(settings.caminho_pessoal("bruno").glob("Ideia_*.md"))
    assert list(settings.caminho_pessoal("ana").glob("Ideia_*.md")) == []


def test_agenda_e_por_dono(multi):
    """O `.ics` que a Ana larga na pasta dela não é compromisso do Bruno. Os LEITORES
    de agenda vivem fora deste módulo (comandos_mestre/scheduler) — aqui garantimos
    que o caminho existe e é distinto; ver o relatório para a fiação."""
    with identidade.usar_dono("ana"):
        da_ana = tools.pasta_do_dono(settings.subpasta_agenda)
    with identidade.usar_dono("bruno"):
        do_bruno = tools.pasta_do_dono(settings.subpasta_agenda)

    assert da_ana != do_bruno
    assert da_ana.parent == settings.caminho_pessoal("ana")


def test_escrita_sem_dono_falha_em_vez_de_cair_na_raiz(multi):
    """Uma nota gravada fora de `Acervo/` e de `Pessoal/<dono>/` não pertence a coleção
    nenhuma: fica INVISÍVEL para a busca. Melhor falhar do que sumir em silêncio."""
    with pytest.raises(identidade.DonoIndefinido):
        tools.raiz_de_escrita()


# ==========================================================================
# DESLIGADO (o default) — tudo cai exatamente onde caía
# ==========================================================================
def test_desligado_atomo_vai_para_conhecimento_novo(vault):
    assert raiz_dos_atomos(settings) == Path(settings.dir_conhecimento_novo)
    assert raiz_dos_atomos(settings, acervo=True) == Path(settings.dir_conhecimento_novo)


def test_desligado_dump_e_o_arquivo_de_sempre(vault):
    assert caminho_chat_dump() == settings.arquivo_chat_dump


def test_desligado_caminhos_das_ferramentas_intactos(vault):
    assert tools.raiz_de_escrita() == Path(settings.caminho_obsidian)
    assert tools.pasta_do_dono(settings.subpasta_listas) == settings.dir_listas
    assert tools.pasta_do_dono(settings.subpasta_agenda) == settings.dir_agenda
    assert tools.arquivo_inbox() == settings.arquivo_inbox
    assert tools.raizes_de_leitura() == [Path(settings.caminho_obsidian)]


def test_desligado_nao_exige_dono(vault):
    """Nenhum caminho de hoje passa a pedir dono: o app de um usuário só continua
    funcionando sem que ninguém marque nada."""
    assert identidade.dono_atual() is None
    tools.raiz_de_escrita()
    tools.arquivo_inbox()
    raiz_dos_atomos(settings)
    caminho_chat_dump(settings)


async def test_desligado_dedup_usa_a_porta_crua(vault, monkeypatch):
    """Byte a byte o de hoje: sem o `{"tipo": "texto"}` que o `recuperar` aplica —
    com ele, uma nota de FIGURA deixaria de poder ser a duplicata encontrada
    (provavelmente melhor, mas é mudança não medida). Ver o TODO em `_vizinho_proximo`."""
    dup = (FakeDoc("acelera o yolo", {"source": "/vault/y.md"}), 0.0)
    vs = FakeVectorStore(cru=[dup], por_escopo={"": [dup]})
    etl = _etl(monkeypatch, vectorstore=vs)

    assert (await etl._vizinho_proximo("acelera o yolo")) is not None
    assert vs.usou_recuperar == 0        # não passou pelo fan-in


async def test_desligado_atomo_cai_onde_sempre_caiu(vault, monkeypatch):
    os.makedirs(settings.dir_conhecimento_novo, exist_ok=True)
    etl = _etl(monkeypatch)

    assert await etl._salvar_atomos(_ATOMO, "Conversa", "IDLE_CONVERSA") == 1
    assert len(_achar(Path(settings.dir_conhecimento_novo), "Conversa")) == 1


# ==========================================================================
# O `Settings` do ctx MANDA — não o `settings` de módulo
# ==========================================================================
async def test_atomo_respeita_o_settings_injetado_no_ctx(tmp_path, monkeypatch):
    """REGRESSÃO (2026-08-03). A 1ª versão de `raiz_dos_atomos` lia o `settings` de
    MÓDULO, furando a injeção que o `EtlProcessor` recebe em `ctx.settings`. Em teste
    os dois objetos não são o mesmo: o `test_preempcao` aponta o do ctx para um
    `tmp_path`, e o átomo foi parar no `Conhecimento_Novo` REAL do dono — duas notas de
    fixture que o próximo `sync` teria indexado como conhecimento de verdade.

    O que trava a volta do defeito é os dois `Settings` DIVERGIREM de propósito, com a
    asserção nos dois lados: o átomo tem de cair no do ctx E não pode aparecer no
    global. Um teste que usasse o mesmo objeto nos dois passaria com o bug de volta."""
    st = Settings(_env_file=None)
    st.caminho_obsidian = str(tmp_path / "vault_do_ctx")
    os.makedirs(st.dir_conhecimento_novo, exist_ok=True)
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path / "vault_global"))
    os.makedirs(settings.dir_conhecimento_novo, exist_ok=True)

    ctx = AppContext(settings=st)                 # <- o ctx aponta para OUTRO vault
    ctx.llama = FakeLlama([""])
    ctx.vectorstore = FakeVectorStore()
    monkeypatch.setattr("mente_digital.etl.db.log_etl", lambda *a, **k: None)

    assert await EtlProcessor(ctx)._salvar_atomos(_ATOMO, "Sintese", "ETL") == 1
    assert len(_achar(Path(st.dir_conhecimento_novo), "Sintese")) == 1
    assert _achar(Path(settings.dir_conhecimento_novo), "Sintese") == []


async def test_desligado_idle_roda_uma_vez_no_contexto_que_chegou(vault, monkeypatch):
    """Nem `usar_dono` entra em cena: uma chamada direta, como sempre foi."""
    etl = _etl(monkeypatch)
    vistos = []

    async def tarefa():
        vistos.append(identidade.dono_atual())

    await etl._por_dono(tarefa)
    assert vistos == [None]
