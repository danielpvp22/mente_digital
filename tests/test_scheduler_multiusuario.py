"""Multiusuário no agendador: o alarme de um NÃO pode tocar no celular do outro — e o
de ninguém pode parar de tocar.

Este arquivo guarda os dois defeitos silenciosos que o multiusuário criou aqui, que são
espelhos um do outro:

  (a) O TICK RODA SEM DONO. Ele parte do relógio, não de um turno. Com a segmentação
      ligada, uma consulta filtrada levantaria `DonoIndefinido`, o `except` do
      `run_forever` engoliria, e os alarmes das QUATRO pessoas parariam sem uma linha de
      erro. Daí `todos_os_donos=True` — e daí um teste que atravessa dois donos num tick.

  (b) O PUSH ERA BROADCAST. `list(ctx.sessoes)` mandava o lembrete a toda sessão viva;
      com quatro usuários isso é conteúdo pessoal entregue a estranho. Vazamento que não
      levanta exceção, não aparece no log e ainda parece funcionar.

Como em `test_rag_multiusuario.py`, boa parte do que se afirma aqui é uma AUSÊNCIA: o
que a sessão do outro NÃO recebeu. E o último bloco é o contrato do interruptor —
`multiusuario_habilitado=False` tem de se comportar byte a byte como o broadcast de
sempre, porque é assim que a espinha inteira mergeia sem mexer na máquina do dono.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from mente_digital import identidade, telemetry
from mente_digital.config import settings
from mente_digital.registro_aparelhos import RegistroAparelhos
from mente_digital.scheduler import SchedulerService
from mente_digital.state import AppContext

from conftest import FakeTts


# ==========================================================================
# Fakes
# ==========================================================================
class FakeSession:
    """Uma sessão viva. `usuario` é o atributo que o `ws.py`/`main.py` carimba no accept
    — o scheduler o lê por `getattr` justamente porque ele nasceu depois desta classe."""

    def __init__(self, usuario=None) -> None:
        self.usuario = usuario
        self.recebidos: list[dict] = []

    async def safe_send(self, data: dict) -> bool:
        self.recebidos.append(data)
        return True


class SessaoAntiga:
    """A sessão de ANTES do multiusuário: nem sequer tem o atributo `usuario`.

    Existe para provar que o `getattr(s, "usuario", None)` não é preciosismo — enquanto o
    lado do WS não aterrissa, um alarme não pode morrer com AttributeError."""

    def __init__(self) -> None:
        self.recebidos: list[dict] = []

    async def safe_send(self, data: dict) -> bool:
        self.recebidos.append(data)
        return True


class FakeCtx:
    def __init__(self, sessoes=()) -> None:
        self.sessoes = set(sessoes)
        self.tts = FakeTts()
        self.web = None
        self.llama = None
        self.interactive_idle = asyncio.Event()
        self.interactive_idle.set()
        self.tarefas: list = []
        self.descansando = False
        self.idle_em_andamento = False
        self.ultima_interacao = time.monotonic()

    def segundos_sem_uso(self, agora=None) -> float:
        return max(0.0, (agora if agora is not None else time.monotonic()) - self.ultima_interacao)

    async def liberar_vram(self):
        return set()

    def track_task(self, coro):
        tarefa = asyncio.ensure_future(coro)
        self.tarefas.append(tarefa)
        return tarefa

    def track_task_threadsafe(self, coro) -> bool:
        """Este fake NUNCA tem loop capturado — ele existe para o caminho DEGRADADO
        (vigia / ctx fora do lifespan). O agendamento de verdade é exercitado com o
        `AppContext` real, em `test_o_alerta_nascido_no_gate_atravessa_o_to_thread`;
        reimplementá-lo aqui seria testar a cópia em vez do original."""
        coro.close()
        return False


class DbMudo:
    """A trilha de auditoria não é o assunto destes testes."""

    def registrar_auditoria(self, acao: str, detalhe: str) -> None:
        pass


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    """Aponta o singleton `db` para um SQLite temporário e cria o schema (com a coluna
    `dono` da migração)."""
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "sched_mu.db"))
    telemetry.db.init()
    return telemetry.db


@pytest.fixture(autouse=True)
def sem_trabalho_de_fundo(monkeypatch):
    """Desliga tudo o que o `tick` faz ALÉM dos alarmes.

    Não é conveniência: com backup/ingestão/consolidação/OCR/pesquisa ligados (são os
    defaults desta máquina), cada `tick()` sem sessão dispara tasks de fundo que tocam
    disco e GPU. O assunto aqui é roteamento de push, e um teste que também zipa o vault
    mede outra coisa."""
    for campo, valor in (("backup_habilitado", False), ("ingestao_habilitada", False),
                         ("consolidacao_habilitada", False), ("academico_habilitado", False),
                         ("ocr_habilitado", False), ("vram_monitor_habilitado", False),
                         ("idle_standby_minutos", 0),
                         ("pesquisa_agendada_intervalo_seconds", 0)):
        monkeypatch.setattr(settings, campo, valor)


@pytest.fixture
def multiusuario(monkeypatch):
    """A flag LIGADA. Um objeto só de settings serve aos dois módulos (o `telemetry`
    importa o mesmo singleton), então isto liga a segmentação do banco junto."""
    monkeypatch.setattr(settings, "multiusuario_habilitado", True)
    return settings


def _proativos(sessao) -> list[dict]:
    return [m for m in sessao.recebidos if m.get("tipo") == "proativo"]


def _textos(sessao) -> str:
    return " | ".join(m.get("texto", "") for m in sessao.recebidos)


def _agendar(dono: str, mensagem: str, quando: datetime, recorrencia=None) -> int:
    with identidade.usar_dono(dono):
        return telemetry.db.criar_agendamento(
            "lembrete", mensagem, quando.isoformat(), recorrencia)


# ==========================================================================
# (a) O tick roda SEM dono — e mesmo assim toca o alarme de todo mundo
# ==========================================================================
async def test_o_tick_atravessa_todos_os_donos(db_tmp, multiusuario):
    """O TESTE que impede o pior defeito possível aqui: alarmes parando em silêncio.

    Repare que nenhum `usar_dono` envolve o `tick()` — é assim que o loop de fundo roda
    de verdade. Sem `todos_os_donos=True` a consulta levantaria `DonoIndefinido`, o
    `except` do `run_forever` engoliria e este teste veria zero disparos."""
    cel_ana, cel_bob = FakeSession("ana"), FakeSession("bob")
    sched = SchedulerService(FakeCtx([cel_ana, cel_bob]))
    passado = datetime.now() - timedelta(minutes=1)
    _agendar("ana", "remédio da ana", passado)
    _agendar("bob", "reunião do bob", passado)

    await sched.tick()

    assert "remédio da ana" in _textos(cel_ana)
    assert "reunião do bob" in _textos(cel_bob)
    # E os dois viraram pendente_entrega (pessimista até o ack), cada um sob o seu dono.
    assert len(db_tmp.get_agendamentos_pendentes(todos_os_donos=True)) == 2


async def test_o_alarme_de_um_nao_toca_no_celular_do_outro(db_tmp, multiusuario):
    """O outro lado da moeda: o push deixou de ser broadcast."""
    cel_ana, cel_bob = FakeSession("ana"), FakeSession("bob")
    sched = SchedulerService(FakeCtx([cel_ana, cel_bob]))
    _agendar("ana", "buscar o resultado do exame", datetime.now() - timedelta(minutes=1))

    await sched.tick()

    assert _proativos(cel_ana), "o dono do lembrete tem de ouvi-lo"
    assert cel_bob.recebidos == [], "a sessão do outro não pode receber NADA do turno alheio"


async def test_a_recorrencia_e_reagendada_sob_o_dono_certo(db_tmp, multiusuario):
    """Reprogramar é um UPDATE com `AND dono`: fora da identidade certa ele não casa
    linha nenhuma, o status nunca muda e o alarme volta a cada tick, para sempre."""
    cel_ana = FakeSession("ana")
    sched = SchedulerService(FakeCtx([cel_ana]))
    ag_id = _agendar("ana", "alongar", datetime.now() - timedelta(minutes=1), "diario")

    await sched.tick()
    await sched.confirmar_entrega(str(ag_id))

    with identidade.usar_dono("ana"):
        ativos = db_tmp.listar_agendamentos()
    assert len(ativos) == 1 and ativos[0]["id"] == ag_id
    assert datetime.fromisoformat(ativos[0]["proximo_disparo"]) > datetime.now()
    with identidade.usar_dono("bob"):
        assert db_tmp.listar_agendamentos() == []       # e não migrou para o vizinho


async def test_o_pendente_e_reentregue_ao_dono_certo(db_tmp, multiusuario):
    """Disparo sem ouvinte espera — mas espera O DONO, não "o primeiro que conectar"."""
    ctx = FakeCtx([])
    sched = SchedulerService(ctx)
    _agendar("ana", "tomar a segunda dose", datetime.now() - timedelta(minutes=1))

    await sched.tick()
    assert len(db_tmp.get_agendamentos_pendentes(todos_os_donos=True)) == 1

    cel_bob = FakeSession("bob")            # quem conecta primeiro é OUTRA pessoa
    ctx.sessoes.add(cel_bob)
    await sched.tick()
    assert cel_bob.recebidos == []
    assert len(db_tmp.get_agendamentos_pendentes(todos_os_donos=True)) == 1  # continua esperando

    cel_ana = FakeSession("ana")
    ctx.sessoes.add(cel_ana)
    await sched.tick()
    assert "segunda dose" in _textos(cel_ana)
    assert cel_bob.recebidos == []


async def test_entregar_pendentes_do_accept_nao_le_a_linha_alheia(db_tmp, multiusuario):
    """No accept já se sabe de quem é a conexão, então a consulta sai FILTRADA — a linha
    do outro nem é lida (mais forte que "não foi entregue")."""
    cel_bob = FakeSession("bob")
    sched = SchedulerService(FakeCtx([cel_bob]))
    _agendar("ana", "segredo da ana", datetime.now() - timedelta(minutes=1))
    with identidade.usar_dono("ana"):
        await sched.tick()                  # vira pendente (sem sessão da ana)

    await sched.entregar_pendentes(dono="bob")
    assert cel_bob.recebidos == []

    cel_ana = FakeSession("ana")
    sched.ctx.sessoes.add(cel_ana)
    await sched.entregar_pendentes(dono="ana")
    assert "segredo da ana" in _textos(cel_ana)


async def test_entregar_pendentes_sem_dono_com_a_flag_ligada_nao_entrega_nada(
        db_tmp, multiusuario, monkeypatch):
    """Conexão sem identidade + segmentação ligada: entregar "para o padrão" seria o
    vazamento. Não entrega — e AVISA, porque isso é sintoma de accept sem dono."""
    avisos: list = []
    monkeypatch.setattr(telemetry.telemetry, "warn", lambda mod, msg: avisos.append(msg))
    cel = FakeSession("daniel")
    sched = SchedulerService(FakeCtx([cel]))
    _agendar("daniel", "qualquer coisa", datetime.now() - timedelta(minutes=1))
    with identidade.usar_dono("daniel"):
        await sched.tick()
    cel.recebidos.clear()

    await sched.entregar_pendentes()        # sem dono no contexto

    assert cel.recebidos == []
    assert any("sem dono no contexto" in a for a in avisos), avisos


async def test_ack_de_outro_dono_e_recusado_e_o_legitimo_ainda_funciona(db_tmp, multiusuario):
    """O ack chega com um id que o CLIENTE escolhe, e a espera é um dicionário global:
    sem conferir o dono, bastava acertar o número para concluir o lembrete alheio (que
    então nunca tocaria)."""
    cel_ana = FakeSession("ana")
    sched = SchedulerService(FakeCtx([cel_ana]))
    ag_id = _agendar("ana", "não conclua isto por mim", datetime.now() - timedelta(minutes=1))
    await sched.tick()

    with identidade.usar_dono("bob"):
        await sched.confirmar_entrega(str(ag_id))
    assert len(db_tmp.get_agendamentos_pendentes(todos_os_donos=True)) == 1

    with identidade.usar_dono("ana"):
        await sched.confirmar_entrega(str(ag_id))
    assert db_tmp.get_agendamentos_pendentes(todos_os_donos=True) == []


async def test_sessao_sem_o_campo_usuario_nao_recebe_e_o_silencio_e_dito(
        db_tmp, multiusuario, monkeypatch):
    """Enquanto o `usuario` não aterrissa no ws: nada de AttributeError derrubando o
    alarme, nada de push a quem não se sabe identificar, e nada de sumiço calado."""
    avisos: list = []
    monkeypatch.setattr(telemetry.telemetry, "warn", lambda mod, msg: avisos.append(msg))
    antiga, cel_ana = SessaoAntiga(), FakeSession("ana")
    sched = SchedulerService(FakeCtx([antiga, cel_ana]))
    _agendar("ana", "coisa da ana", datetime.now() - timedelta(minutes=1))

    await sched.tick()

    assert "coisa da ana" in _textos(cel_ana)
    assert antiga.recebidos == []
    assert any("sem usuário identificado" in a for a in avisos), avisos


def test_dono_ausente_cai_no_padrao_em_vez_de_travar(monkeypatch):
    """Linha anterior ao backfill: assumir o padrão é o mal menor — recusar deixaria o
    UPDATE sem casar nada e o alarme dispararia a cada tick, para sempre. Mas o palpite
    é DITO."""
    avisos: list = []
    monkeypatch.setattr(telemetry.telemetry, "warn", lambda mod, msg: avisos.append(msg))
    sched = SchedulerService(FakeCtx([]))
    assert sched._dono_do({"id": 7, "dono": None}) == identidade.DONO_PADRAO
    assert avisos and "sem dono" in avisos[0]


# ==========================================================================
# Os dois conceitos de "tem sessão" — confundi-los é regressão nos dois sentidos
# ==========================================================================
def test_ha_sessoes_e_sobre_hardware_e_nao_sobre_dono(multiusuario, monkeypatch):
    """`_ha_sessoes` é a pergunta da GPU (tem ALGUÉM conversando? então o crawler
    espera); `_sessoes_do_dono` é a da privacidade. Se o gate de fundo passasse a olhar
    só o dono, o trabalho pesado voltaria a atropelar a conversa de outra pessoa —
    regressão de latência, não de vazamento."""
    monkeypatch.setattr(settings, "pesquisa_agendada_intervalo_seconds", 7200)
    sched = SchedulerService(FakeCtx([FakeSession("bob")]))

    assert sched._ha_sessoes() is True
    assert sched._sessoes_do_dono("ana") == []
    assert sched._pesquisa_idle_devida(datetime.now()) is False


# ==========================================================================
# Alerta de segurança para o MESTRE (2026-08-03)
# ==========================================================================
async def test_o_alerta_de_seguranca_chega_ao_mestre_e_so_a_ele(db_tmp, multiusuario):
    """O MESTRE é quem administra o acesso — é o celular dele que recebe. E o alerta é
    conteúdo de segurança da casa: não é para a sessão dos outros três."""
    cel_mestre = FakeSession(identidade.MESTRE)
    cel_ana = FakeSession("ana")
    sched = SchedulerService(FakeCtx([cel_mestre, cel_ana]))

    assert await sched.alertar_mestre("12 falhas de autenticação", "ip=192.168.0.9") is True

    cards = [m for m in cel_mestre.recebidos if m.get("tipo") == "seguranca"]
    assert cards and cards[0]["titulo"] == "12 falhas de autenticação"
    assert cards[0]["detalhe"] == "ip=192.168.0.9"
    # A bolha falada vai junto: é ela que o front de hoje sabe exibir (e ackar).
    assert any("192.168.0.9" in m.get("texto", "") for m in _proativos(cel_mestre))
    assert cel_ana.recebidos == []


async def test_o_alerta_e_estrangulado_por_janela(db_tmp, multiusuario):
    """Sem a janela, QUEM ESCOLHE quantas notificações o dono recebe é o atacante: cada
    401 vira um push. Chaves diferentes (IPs diferentes) continuam passando."""
    cel = FakeSession(identidade.MESTRE)
    sched = SchedulerService(FakeCtx([cel]))

    assert await sched.alertar_mestre("rajada", "ip=1.1.1.1", chave="auth:1.1.1.1") is True
    assert await sched.alertar_mestre("rajada", "ip=1.1.1.1", chave="auth:1.1.1.1") is False
    assert await sched.alertar_mestre("rajada", "ip=2.2.2.2", chave="auth:2.2.2.2") is True

    assert len([m for m in cel.recebidos if m.get("tipo") == "seguranca"]) == 2

    # Passada a janela, o mesmo IP volta a alertar (é rajada NOVA, não spam da antiga).
    sched._alertado_em["auth:1.1.1.1"] = datetime.now() - timedelta(hours=1)
    assert await sched.alertar_mestre("rajada", "ip=1.1.1.1", chave="auth:1.1.1.1") is True


async def test_alerta_sem_ouvinte_vira_pendente_e_chega_na_proxima_conexao(
        db_tmp, multiusuario):
    """Reuso da fila durável dos lembretes em vez de máquina nova: é ela que já sabe
    reentregar, esperar o ack e não duplicar. O ataque costuma acontecer justamente com
    o dono longe do computador."""
    ctx = FakeCtx([])
    sched = SchedulerService(ctx)

    assert await sched.alertar_mestre("aparelho revogado tentou entrar", "ip=10.0.0.4") is False
    pendentes = db_tmp.get_agendamentos_pendentes(todos_os_donos=True)
    assert len(pendentes) == 1
    assert pendentes[0]["tipo"] == "seguranca" and pendentes[0]["dono"] == identidade.MESTRE

    cel = FakeSession(identidade.MESTRE)
    ctx.sessoes.add(cel)
    await sched.entregar_pendentes(dono=identidade.MESTRE)

    # O card é remontado a partir do payload — o alerta reentregue é igual ao ao-vivo.
    cards = [m for m in cel.recebidos if m.get("tipo") == "seguranca"]
    assert cards and cards[0]["titulo"] == "aparelho revogado tentou entrar"
    assert any("10.0.0.4" in m.get("texto", "") for m in _proativos(cel))

    ack = str(pendentes[0]["id"])
    with identidade.usar_dono(identidade.MESTRE):
        await sched.confirmar_entrega(ack)
    assert db_tmp.get_agendamentos_pendentes(todos_os_donos=True) == []


async def test_o_gancho_sincrono_do_registro_de_aparelhos(db_tmp, multiusuario):
    """A ASSINATURA combinada com o lado do acesso: `(titulo, detalhe, chave) -> None`,
    síncrona, sem levantar. Quem chama está no meio de NEGAR um acesso — uma falha de
    notificação não pode virar falha do gate."""
    cel = FakeSession(identidade.MESTRE)
    ctx = FakeCtx([cel])
    sched = SchedulerService(ctx)

    sched.alertar_seguranca("8 falhas seguidas", "ip=203.0.113.7 em 40s", "auth:203.0.113.7")
    await asyncio.gather(*ctx.tarefas)

    assert any(m.get("tipo") == "seguranca" for m in cel.recebidos)
    # A janela é conferida ANTES de criar a task: uma rajada não vira 500 tasks.
    sched.alertar_seguranca("8 falhas seguidas", "ip=203.0.113.7 em 41s", "auth:203.0.113.7")
    assert len(ctx.tarefas) == 1


async def test_o_gancho_fora_do_loop_nao_levanta_e_deixa_rastro(db_tmp, monkeypatch):
    """Sem NENHUM loop a que recorrer (o processo do vigia, um ctx montado fora do
    lifespan): não dá para agendar a fala, mas o alerta não pode sumir só porque veio da
    thread errada. Note o `FakeCtx` SEM `capturar_loop()` — é essa a diferença para o
    teste abaixo, que é o caminho de produção."""
    erros: list = []
    monkeypatch.setattr(telemetry.telemetry, "error",
                        lambda mod, msg, exc=None: erros.append(msg))
    sched = SchedulerService(FakeCtx([]))

    await asyncio.to_thread(sched.alertar_seguranca, "rajada", "ip=1.2.3.4", "auth:1.2.3.4")

    assert any("sem loop" in e for e in erros), erros


async def test_o_alerta_nascido_no_gate_atravessa_o_to_thread(tmp_path, db_tmp, multiusuario):
    """A REGRESSÃO de 2026-08-04 — o traceback que o boot imprimiu e engoliu.

    O gate de acesso NÃO roda no event loop: `main.py` chama `registro.autorizar` por
    `asyncio.to_thread` (ele abre SQLite, e chamá-lo direto travava o loop a cada
    requisição — inclusive as sem credencial nenhuma). Só que é de DENTRO dele que o
    castigo por força bruta dispara o alerta, e `asyncio.get_running_loop()` numa thread
    de trabalho levanta. Resultado medido: todo alerta nascido do gate — que é a origem
    de praticamente todos — morria no log, exatamente quando servia.

    Por isso o caminho aqui é o de produção inteiro (registro real, castigo real,
    `to_thread` real): o defeito não estava em nenhum dos dois lados isolados, e sim na
    fronteira entre eles. Só o socket é fake."""
    ctx = AppContext(settings=settings)
    ctx.tts = FakeTts()
    cel = FakeSession(identidade.MESTRE)
    ctx.sessoes.add(cel)
    ctx.capturar_loop()                      # o que o lifespan faz
    sched = SchedulerService(ctx)

    registro = RegistroAparelhos(str(tmp_path / "aparelhos.db"), db=DbMudo())
    registro.init()
    registro.ao_alerta = sched.alertar_seguranca
    registro.configurar_bloqueio(2.0, 900.0)

    for _ in range(3):                       # na 3ª o castigo dispara (medido no boot)
        await asyncio.to_thread(
            registro.autorizar, "mdk1.0123456789abcdef.errado", "127.0.0.1", "/api/conversas",
            habilitado=True, token_legado="", aceita_token_legado=False)

    # `call_soon_threadsafe` só entrega na próxima passada do loop; a espera é pela
    # CRIAÇÃO da task, e o `drenar_tasks` cuida da execução dela.
    await asyncio.sleep(0.05)
    await ctx.drenar_tasks(timeout=5.0)

    assert any(m.get("tipo") == "seguranca" for m in cel.recebidos), cel.recebidos
    assert any("127.0.0.1" in m.get("texto", "") for m in _proativos(cel))


# ==========================================================================
# O interruptor: desligado = o broadcast de sempre, byte a byte
# ==========================================================================
async def test_desligado_o_push_volta_a_ser_broadcast(db_tmp):
    """Contrato do interruptor: sem multiusuário todo mundo é o mesmo dono, e o alarme
    aparece nas DUAS cascas abertas (janela do PC + celular) como sempre apareceu —
    inclusive na sessão que traz um `usuario` qualquer."""
    janela, celular, antiga = FakeSession("daniel"), FakeSession("bob"), SessaoAntiga()
    sched = SchedulerService(FakeCtx([janela, celular, antiga]))
    telemetry.db.criar_agendamento(
        "lembrete", "reunião às 15h", (datetime.now() - timedelta(minutes=1)).isoformat())

    await sched.tick()

    for sessao in (janela, celular, antiga):
        assert "reunião às 15h" in _textos(sessao)


async def test_desligado_o_accept_entrega_sem_dono_nenhum(db_tmp):
    """A chamada de hoje (`entregar_pendentes()` sem argumento, sem ContextVar) continua
    valendo: o `_dono_para_consulta` cai em DONO_PADRAO, que é o carimbo do backfill."""
    ctx = FakeCtx([])
    sched = SchedulerService(ctx)
    telemetry.db.criar_agendamento(
        "lembrete", "levar o carro", (datetime.now() - timedelta(minutes=1)).isoformat())
    await sched.tick()

    sessao = FakeSession()
    ctx.sessoes.add(sessao)
    await sched.entregar_pendentes()

    assert "levar o carro" in _textos(sessao)
