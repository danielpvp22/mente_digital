"""
Segmentação por DONO das tabelas PESSOAIS do SQLite (multiusuário, 2026-08-03).

O que estes testes protegem, em ordem de gravidade:

1. O VAZAMENTO. Um `SELECT` sem `WHERE dono = ?` não dá erro — devolve a memória de
   outra pessoa, calado. Por isso quase todo teste aqui é da forma "o dono B pediu X e
   recebeu VAZIO", e não "o dono A recebeu o dele" (essa metade passaria mesmo com o
   filtro ausente).
2. O IDOR. `conversa_id` e `id` de agendamento trafegam pelo cliente. Ler/cancelar pelo
   id de outro era possível; aqui está a prova de que não é mais.
3. A COLISÃO SILENCIOSA de chave. `lacunas`, `rotinas`, `mestre_atalhos`, `habitos` e
   `perfil_conversa` nasceram com PRIMARY KEY/UNIQUE sobre um texto do USUÁRIO. Com dois
   donos, o UPSERT casava entre PESSOAS e o `INSERT OR IGNORE` de `habitos` DESCARTAVA o
   dia do segundo. É por isso que a migração RECONSTRÓI essas tabelas.
4. A COMPATIBILIDADE. Com `multiusuario_habilitado=False` (default), tudo tem de se
   comportar exatamente como antes — inclusive nos caminhos que rodam sem dono nenhum
   (lifespan, tick do scheduler, ETL idle).

DB em arquivo temporário; nada de GPU, rede ou vault.
"""
from __future__ import annotations

import sqlite3

import pytest

from mente_digital import identidade, telemetry as tele
from mente_digital.telemetry import Database

# ---------------------------------------------------------------------------
# Ferramentas do teste
# ---------------------------------------------------------------------------

# O esquema EXATO de antes da migração — é a documentação viva do que migramos DE.
# Sem ele, "backfill" e "reconstrução de chave" seriam testados contra um banco que já
# nasceu certo, e a migração de verdade (a que roda no telemetria_etl.db do dono, com
# 91 conversas dentro) nunca teria sido exercitada.
_SCHEMA_LEGADO = (
    """CREATE TABLE chat_history
       (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT,
        pergunta TEXT, resposta TEXT, conversa_id TEXT)""",
    """CREATE TABLE agendamentos
       (id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT, mensagem TEXT,
        proximo_disparo TEXT, recorrencia TEXT, payload TEXT,
        status TEXT DEFAULT 'ativo', criado_em TEXT, conversa_id TEXT)""",
    """CREATE TABLE auditoria
       (id INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT, acao TEXT, detalhe TEXT)""",
    """CREATE TABLE srs_cards
       (id INTEGER PRIMARY KEY AUTOINCREMENT, frente TEXT, verso TEXT,
        proxima_revisao TEXT, estagio INTEGER DEFAULT 0, criado_em TEXT)""",
    """CREATE TABLE gatilhos
       (id INTEGER PRIMARY KEY AUTOINCREMENT, evento TEXT, filtro TEXT,
        acao TEXT, descricao TEXT, criado_em TEXT)""",
    """CREATE TABLE lacunas
       (chave TEXT PRIMARY KEY, termos TEXT, n INTEGER DEFAULT 1,
        visto_em TEXT, pesquisado_em TEXT)""",
    """CREATE TABLE temas_quentes
       (chave TEXT PRIMARY KEY, termos TEXT, n INTEGER DEFAULT 1,
        visto_em TEXT, pesquisado_em TEXT)""",
    """CREATE TABLE mestre_nao_reconhecido
       (chave TEXT PRIMARY KEY, comando TEXT, n INTEGER DEFAULT 1, visto_em TEXT)""",
    """CREATE TABLE mestre_frequencia
       (assinatura TEXT PRIMARY KEY, exemplo TEXT, n INTEGER DEFAULT 1,
        sugerido INTEGER DEFAULT 0, visto_em TEXT)""",
    """CREATE TABLE mestre_atalhos
       (nome TEXT PRIMARY KEY, comando TEXT, criado_em TEXT)""",
    """CREATE TABLE habitos
       (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, data TEXT,
        criado_em TEXT, UNIQUE(nome, data))""",
    """CREATE TABLE rotinas (nome TEXT PRIMARY KEY, comando TEXT, criado_em TEXT)""",
    """CREATE TABLE perfil_conversa
       (chave TEXT PRIMARY KEY, valor TEXT, atualizado_em TEXT)""",
)

_PESSOAIS = (
    "chat_history", "agendamentos", "lacunas", "temas_quentes", "srs_cards", "habitos",
    "rotinas", "gatilhos", "mestre_atalhos", "mestre_frequencia",
    "mestre_nao_reconhecido", "perfil_conversa", "auditoria",
)


class _SettingsMultiusuario:
    """Espelha o `settings` real com o multiusuário LIGADO.

    Existe porque o campo `multiusuario_habilitado` nasce em config.py em OUTRA frente
    de trabalho e o pydantic recusa `setattr` de campo inexistente — o teste não pode
    depender da ordem em que as duas metades chegam. `__getattr__` só é chamado quando a
    busca normal falha, então o atributo de instância vence e o resto delega ao real."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.multiusuario_habilitado = True

    def __getattr__(self, nome: str) -> object:
        return getattr(self._real, nome)


@pytest.fixture
def ligado(monkeypatch):
    """Liga o modo multiusuário no módulo sob teste."""
    monkeypatch.setattr(tele, "settings", _SettingsMultiusuario(tele.settings))


def _db(tmp_path, nome: str = "t.db") -> Database:
    d = Database(str(tmp_path / nome))
    d.init()
    return d


def _banco_legado(tmp_path, nome: str = "velho.db") -> str:
    """Cria um banco no esquema PRÉ-migração, com uma linha em cada tabela pessoal."""
    caminho = str(tmp_path / nome)
    conn = sqlite3.connect(caminho)
    try:
        for ddl in _SCHEMA_LEGADO:
            conn.execute(ddl)
        conn.execute("INSERT INTO chat_history (data_hora, pergunta, resposta, conversa_id)"
                     " VALUES ('2026-01-01T10:00:00', 'oi', 'olá', 'c-antiga')")
        conn.execute("INSERT INTO agendamentos (tipo, mensagem, proximo_disparo, status)"
                     " VALUES ('lembrete', 'remédio', '2026-01-01T09:00:00', 'ativo')")
        conn.execute("INSERT INTO auditoria (data_hora, acao, detalhe)"
                     " VALUES ('2026-01-01T10:00:00', 'criar_lembrete', 'remédio')")
        conn.execute("INSERT INTO srs_cards (frente, verso, proxima_revisao, estagio)"
                     " VALUES ('q', 'r', '2026-01-01T00:00:00', 0)")
        conn.execute("INSERT INTO gatilhos (evento, filtro, acao, descricao)"
                     " VALUES ('lista_add', '', '[]', 'gatilho antigo')")
        conn.execute("INSERT INTO lacunas (chave, termos, n, visto_em)"
                     " VALUES ('o que e x', 'x', 4, '2026-01-01T10:00:00')")
        conn.execute("INSERT INTO temas_quentes (chave, termos, n, visto_em)"
                     " VALUES ('poda', 'poda', 7, '2026-01-01T10:00:00')")
        conn.execute("INSERT INTO mestre_nao_reconhecido (chave, comando, n, visto_em)"
                     " VALUES ('faz cafe', 'faz café', 2, '2026-01-01T10:00:00')")
        conn.execute("INSERT INTO mestre_frequencia (assinatura, exemplo, n, sugerido, visto_em)"
                     " VALUES ('diagnostico', 'diagnóstico', 5, 1, '2026-01-01T10:00:00')")
        conn.execute("INSERT INTO mestre_atalhos (nome, comando, criado_em)"
                     " VALUES ('compras', 'adiciona pão na lista', '2026-01-01T10:00:00')")
        conn.execute("INSERT INTO habitos (nome, data, criado_em)"
                     " VALUES ('academia', '2026-01-01', '2026-01-01T10:00:00')")
        conn.execute("INSERT INTO rotinas (nome, comando, criado_em)"
                     " VALUES ('manha', 'lê a lista e cria lembrete', '2026-01-01T10:00:00')")
        conn.execute("INSERT INTO perfil_conversa (chave, valor, atualizado_em)"
                     " VALUES ('conversa', 'gosta de respostas curtas', '2026-01-01T10:00:00')")
        conn.commit()
    finally:
        conn.close()
    return caminho


def _colunas(caminho: str, tabela: str) -> list:
    conn = sqlite3.connect(caminho)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({tabela})").fetchall()]
    finally:
        conn.close()


def _uma(caminho: str, sql: str, params: tuple = ()):
    conn = sqlite3.connect(caminho)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Migração: idempotente, com backfill, sem perder o legado
# ---------------------------------------------------------------------------
def test_init_e_idempotente_em_banco_novo(tmp_path):
    """init() duas vezes não pode explodir nem duplicar coluna."""
    d = _db(tmp_path)
    d.init()
    d.init()
    for tabela in _PESSOAIS:
        cols = _colunas(d.path, tabela)
        assert cols.count("dono") == 1, tabela


def test_init_e_idempotente_em_banco_legado(tmp_path):
    """A migração roda no banco antigo e RODAR DE NOVO é inócuo (o app reinicia)."""
    caminho = _banco_legado(tmp_path)
    d = Database(caminho)
    d.init()
    d.init()
    d.init()
    for tabela in _PESSOAIS:
        assert _colunas(caminho, tabela).count("dono") == 1, tabela
        # A tabela de trabalho da reconstrução não pode ficar para trás.
        assert _uma(caminho, "SELECT name FROM sqlite_master WHERE name = ?",
                    (f"{tabela}_pre_dono",)) is None, tabela


def test_backfill_carimba_dono_padrao(tmp_path):
    """Todo o banco que existia antes é de UMA pessoa — some se não for carimbado."""
    caminho = _banco_legado(tmp_path)
    Database(caminho).init()
    for tabela in _PESSOAIS:
        n_nulos = _uma(caminho, f"SELECT COUNT(*) FROM {tabela} WHERE dono IS NULL")[0]
        n_padrao = _uma(caminho, f"SELECT COUNT(*) FROM {tabela} WHERE dono = ?",
                        (identidade.DONO_PADRAO,))[0]
        assert (n_nulos, n_padrao) == (0, 1), tabela


def test_reconstrucao_preserva_o_conteudo_do_legado(tmp_path):
    """Reconstruir tabela é a parte perigosa: o dado tem de ATRAVESSAR intacto."""
    caminho = _banco_legado(tmp_path)
    Database(caminho).init()
    assert _uma(caminho, "SELECT termos, n FROM lacunas") == ("x", 4)
    assert _uma(caminho, "SELECT comando FROM mestre_atalhos")[0] == "adiciona pão na lista"
    assert _uma(caminho, "SELECT nome, data FROM habitos") == ("academia", "2026-01-01")
    assert _uma(caminho, "SELECT valor FROM perfil_conversa")[0] == "gosta de respostas curtas"
    assert _uma(caminho, "SELECT n, sugerido FROM mestre_frequencia") == (5, 1)


def test_reconstrucao_troca_a_chave_para_composta(tmp_path):
    """A prova de que a PK mudou: a MESMA chave de outro dono agora cabe."""
    caminho = _banco_legado(tmp_path)
    Database(caminho).init()
    conn = sqlite3.connect(caminho)
    try:
        # Com a PK antiga (chave), este INSERT levantaria IntegrityError.
        conn.execute("INSERT INTO lacunas (chave, dono, termos, n) VALUES ('o que e x', 'ana', 'x', 1)")
        conn.execute("INSERT INTO rotinas (nome, dono, comando) VALUES ('manha', 'ana', 'outra')")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM lacunas").fetchone()[0] == 2
    finally:
        conn.close()


def test_indice_por_dono_existe_onde_a_leitura_e_quente(tmp_path):
    d = _db(tmp_path)
    conn = sqlite3.connect(d.path)
    try:
        nomes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    finally:
        conn.close()
    assert {"idx_chat_dono", "idx_agend_dono"} <= nomes


def test_tabelas_da_maquina_ficam_sem_dono(tmp_path):
    """metricas_latencia, log_etl & cia. são o painel da máquina, não memória de alguém."""
    d = _db(tmp_path)
    for tabela in ("metricas_latencia", "log_etl", "base_snapshot", "router_log",
                   "academico_pdfs", "contradicoes"):
        assert "dono" not in _colunas(d.path, tabela), tabela


# ---------------------------------------------------------------------------
# 2. Isolamento: o dono B não enxerga o dado do dono A
# ---------------------------------------------------------------------------
def test_isolamento_do_historico(tmp_path, ligado):
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        d.save_chat("segredo da ana", "resposta", "c-ana")
    with identidade.usar_dono("bruno"):
        assert d.get_history() == []
        assert d.get_conversations() == []
    with identidade.usar_dono("ana"):
        assert [t["q"] for t in d.get_history()] == ["segredo da ana"]
        assert len(d.get_conversations()) == 1


def test_idor_get_conversation_fechado(tmp_path, ligado):
    """O `cid` vem do CLIENTE: pedir o id alheio tem de devolver VAZIO, não a conversa."""
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        d.save_chat("pergunta privada", "resposta privada", "c-ana")
        assert len(d.get_conversation("c-ana")) == 1
    with identidade.usar_dono("bruno"):
        assert d.get_conversation("c-ana") == []      # sabe o id e ainda assim não lê


def test_isolamento_das_listas_pessoais(tmp_path, ligado):
    """Uma varredura por todas as leituras pessoais: o dono B chega e não vê NADA."""
    d = _db(tmp_path)
    agora = "2026-01-01T12:00:00"
    with identidade.usar_dono("ana"):
        d.save_lacuna("o que e x", "x")
        d.save_tema_quente("poda", "poda")
        d.salvar_atalho("compras", "adiciona pão")
        d.rotina_salvar("manha", "lê a lista")
        d.habito_marcar("academia", "2026-01-01")
        d.srs_criar_card("frente", "verso", "2025-01-01T00:00:00")
        d.gatilho_criar("lista_add", "", "[]", "avisa quando add")
        d.registrar_comando_desconhecido("faz café")
        d.salvar_perfil("gosta de respostas curtas")
        d.criar_agendamento("lembrete", "remédio", "2026-01-01T09:00:00")
    with identidade.usar_dono("bruno"):
        assert d.get_lacunas() == []
        assert d.get_temas_quentes(min_reuso=1) == []
        assert d.listar_atalhos() == {}
        assert d.rotinas_listar() == [] and d.rotina_get("manha") is None
        assert d.habitos_nomes() == [] and d.habito_datas("academia") == []
        assert d.srs_vencidos(agora, 10) == [] and d.srs_contar_vencidos(agora) == 0
        assert d.gatilhos_listar() == [] and d.gatilhos_por_evento("lista_add") == []
        assert d.get_comandos_desconhecidos() == []
        assert d.ler_perfil() is None
        assert d.listar_agendamentos() == []
    with identidade.usar_dono("ana"):   # e o dono de verdade continua vendo o dele
        assert len(d.get_lacunas()) == 1
        assert d.listar_atalhos() == {"compras": "adiciona pão"}
        assert d.ler_perfil() == "gosta de respostas curtas"
        assert len(d.listar_agendamentos()) == 1


def test_mesma_chave_para_dois_donos_nao_colide(tmp_path, ligado):
    """O caso que a coluna sozinha NÃO resolveria — chave de texto do usuário."""
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        d.salvar_atalho("compras", "comando da ana")
        d.rotina_salvar("manha", "rotina da ana")
        d.habito_marcar("academia", "2026-01-01")
        d.salvar_perfil("perfil da ana")
        d.save_lacuna("o que e x", "termos ana")
    with identidade.usar_dono("bruno"):
        d.salvar_atalho("compras", "comando do bruno")
        d.rotina_salvar("manha", "rotina do bruno")
        d.habito_marcar("academia", "2026-01-01")     # MESMO hábito, MESMO dia
        d.salvar_perfil("perfil do bruno")
        d.save_lacuna("o que e x", "termos bruno")
        assert d.rotina_get("manha") == "rotina do bruno"
        assert d.habito_datas("academia") == ["2026-01-01"]   # não sumiu no OR IGNORE
        assert d.ler_perfil() == "perfil do bruno"
    with identidade.usar_dono("ana"):   # nada do bruno sobrescreveu nada da ana
        assert d.listar_atalhos() == {"compras": "comando da ana"}
        assert d.rotina_get("manha") == "rotina da ana"
        assert d.habito_datas("academia") == ["2026-01-01"]
        assert d.ler_perfil() == "perfil da ana"
        assert d.get_lacunas()[0] == {"termos": "termos ana", "n": 1}   # n NÃO virou 2


def test_frequencia_de_intencao_conta_por_dono(tmp_path, ligado):
    """O hábito de um não pode acelerar a oferta de atalho ao outro."""
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        for _ in range(3):
            d.registrar_frequencia("diagnostico", "diagnóstico")
        d.marcar_sugerido("diagnostico")
    with identidade.usar_dono("bruno"):
        assert d.registrar_frequencia("diagnostico", "diagnóstico") == (1, False)


# ---------------------------------------------------------------------------
# 3. Escrita por id: o IDOR do agendamento
# ---------------------------------------------------------------------------
def test_idor_de_escrita_no_agendamento_fechado(tmp_path, ligado):
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        ag_id = d.criar_agendamento("lembrete", "remédio", "2026-01-01T09:00:00")
    with identidade.usar_dono("bruno"):
        assert d.cancelar_agendamento(ag_id) is False        # sabe o id e não cancela
        d.atualizar_agendamento(ag_id, status="cancelado")   # nem por update direto
    with identidade.usar_dono("ana"):
        assert len(d.listar_agendamentos()) == 1             # segue ativo
        assert d.cancelar_agendamento(ag_id) is True         # o dono cancela


def test_idor_de_escrita_no_srs_fechado(tmp_path, ligado):
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        d.srs_criar_card("frente", "verso", "2020-01-01T00:00:00")
        card_id = d.srs_vencidos("2026-01-01T00:00:00", 10)[0]["id"]
    with identidade.usar_dono("bruno"):
        d.srs_reagendar(card_id, 5, "2030-01-01T00:00:00")
    with identidade.usar_dono("ana"):
        assert d.srs_vencidos("2026-01-01T00:00:00", 10)[0]["estagio"] == 0


def test_gatilho_remover_de_outro_dono_nao_apaga(tmp_path, ligado):
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        d.gatilho_criar("lista_add", "", "[]", "o gatilho da ana")
        gid = d.gatilhos_listar()[0]["id"]
    with identidade.usar_dono("bruno"):
        assert d.gatilho_remover(gid) is False
    with identidade.usar_dono("ana"):
        assert len(d.gatilhos_listar()) == 1


# ---------------------------------------------------------------------------
# 4. O scheduler: a exceção que ATRAVESSA os donos de propósito
# ---------------------------------------------------------------------------
def test_vencidos_todos_os_donos_atravessa_e_reporta_o_dono(tmp_path, ligado):
    """O tick roda FORA de turno: sem esta porta, o alarme dos outros nunca tocaria."""
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        d.criar_agendamento("lembrete", "remédio da ana", "2026-01-01T09:00:00")
    with identidade.usar_dono("bruno"):
        d.criar_agendamento("lembrete", "reunião do bruno", "2026-01-01T09:30:00")

    # Sem dono no contexto (é o caso REAL do scheduler) e com a porta aberta.
    vencidos = d.get_agendamentos_vencidos("2026-01-01T10:00:00", todos_os_donos=True)
    assert len(vencidos) == 2
    assert {v["dono"] for v in vencidos} == {"ana", "bruno"}   # dá para rotear a fala

    # E o default continua sendo o lado seguro: filtrado por quem está falando.
    with identidade.usar_dono("ana"):
        so_da_ana = d.get_agendamentos_vencidos("2026-01-01T10:00:00")
        assert [v["mensagem"] for v in so_da_ana] == ["remédio da ana"]


def test_pendentes_tem_a_mesma_porta(tmp_path, ligado):
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        ag_id = d.criar_agendamento("lembrete", "pendente da ana", "2026-01-01T09:00:00")
        d.atualizar_agendamento(ag_id, status="pendente_entrega")
    assert len(d.get_agendamentos_pendentes(todos_os_donos=True)) == 1
    with identidade.usar_dono("bruno"):
        assert d.get_agendamentos_pendentes() == []


# ---------------------------------------------------------------------------
# 5. Auditoria: a exceção no outro sentido (linha SEM dono não pode se perder)
# ---------------------------------------------------------------------------
def test_auditoria_grava_sem_dono_e_admin_ve_tudo(tmp_path, ligado):
    """Recusa de acesso é auditada ANTES de existir dono — exigir um perderia a linha
    que mais importa. E a visão de admin (dono=None) enxerga a trilha inteira."""
    d = _db(tmp_path)
    d.registrar_auditoria("acesso_negado", "aparelho revogado")   # sem contexto de dono
    with identidade.usar_dono("ana"):
        d.registrar_auditoria("criar_lembrete", "remédio")

    assert len(d.get_auditoria()) == 2                            # admin: trilha inteira
    assert [a["acao"] for a in d.get_auditoria(dono="ana")] == ["criar_lembrete"]
    assert d.get_auditoria(dono="bruno") == []
    assert _uma(d.path, "SELECT COUNT(*) FROM auditoria WHERE dono IS NULL")[0] == 1


def test_backfill_nao_reescreve_auditoria_sem_dono(tmp_path, ligado):
    """O UPDATE do backfill mora DENTRO do ramo do ALTER: se rodasse a cada boot, ele
    atribuiria a 'daniel' a recusa de acesso que ninguém fez."""
    d = _db(tmp_path)
    d.registrar_auditoria("acesso_negado", "aparelho revogado")
    d.init()   # o app reiniciou
    assert _uma(d.path, "SELECT COUNT(*) FROM auditoria WHERE dono IS NULL")[0] == 1


# ---------------------------------------------------------------------------
# 6. Modo DESLIGADO: nada pode mudar enquanto a função não é ligada
# ---------------------------------------------------------------------------
def test_desligado_funciona_sem_dono_nenhum(tmp_path):
    """Sem a fixture `ligado`: é o estado do repo hoje. Nenhum caminho marca dono —
    lifespan, tick do scheduler e ETL idle rodam assim — e tudo tem de responder."""
    d = _db(tmp_path)
    d.save_chat("pergunta", "resposta", "c1")
    d.salvar_perfil("curto")
    d.save_lacuna("o que e x", "x")
    d.salvar_atalho("compras", "adiciona pão")
    ag_id = d.criar_agendamento("lembrete", "remédio", "2026-01-01T09:00:00")

    assert [t["q"] for t in d.get_history()] == ["pergunta"]
    assert len(d.get_conversations()) == 1
    assert len(d.get_conversation("c1")) == 1
    assert d.ler_perfil() == "curto"
    assert len(d.get_lacunas()) == 1
    assert d.listar_atalhos() == {"compras": "adiciona pão"}
    assert len(d.listar_agendamentos()) == 1
    assert len(d.get_agendamentos_vencidos("2026-01-01T10:00:00")) == 1
    assert d.cancelar_agendamento(ag_id) is True


def test_desligado_le_o_legado_ja_carimbado(tmp_path):
    """A ponte que sustenta o desligado: o backfill escreveu DONO_PADRAO e é ele que o
    `_dono_para_consulta` devolve quando ninguém marcou — as linhas continuam achadas."""
    caminho = _banco_legado(tmp_path)
    d = Database(caminho)
    d.init()
    assert [t["q"] for t in d.get_history()] == ["oi"]
    assert len(d.get_conversation("c-antiga")) == 1
    assert d.ler_perfil() == "gosta de respostas curtas"
    assert d.listar_atalhos() == {"compras": "adiciona pão na lista"}
    assert d.rotina_get("manha") == "lê a lista e cria lembrete"
    assert d.habito_datas("academia") == ["2026-01-01"]
    assert d.get_lacunas() == [{"termos": "x", "n": 4}]
    assert len(d.listar_agendamentos()) == 1


def test_desligado_ainda_honra_o_dono_marcado(tmp_path):
    """DESLIGADO não quer dizer "sem dono": o fallback é `dono_atual() OU DONO_PADRAO`.
    Quem marcou um dono é atendido como esse dono; só quem NÃO marcou cai no padrão.

    ⚠ Isto é uma MEIA-SEGMENTAÇÃO enquanto a chave está desligada, e a armadilha está
    aqui de propósito para ninguém "consertar" por engano: se o WebSocket passar a
    marcar o dono ANTES de a chave subir, o turno é gravado sob 'ana' e o trabalho de
    fundo que NÃO marca (ETL idle, lifespan, tick) lê sob 'daniel' e não acha nada.
    Marcar dono e ligar a chave têm de viajar juntos."""
    d = _db(tmp_path)
    with identidade.usar_dono("ana"):
        d.save_chat("da ana", "resposta", "c1")
        assert [t["q"] for t in d.get_history()] == ["da ana"]     # mesma marcação: acha
    assert d.get_history() == []                                   # sem marcação: padrão


def test_ligado_sem_dono_falha_alto(tmp_path, ligado):
    """O ⚠ de identidade.py: filtro ausente devolve a memória de todo mundo, calado.
    Melhor um erro visível — quem esqueceu de marcar o dono descobre na hora."""
    d = _db(tmp_path)
    with pytest.raises(identidade.DonoIndefinido):
        d.get_history()
    with pytest.raises(identidade.DonoIndefinido):
        d.save_chat("pergunta", "resposta", "c1")
    with pytest.raises(identidade.DonoIndefinido):
        d.get_conversation("c1")
