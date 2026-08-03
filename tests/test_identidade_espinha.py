"""A ESPINHA do multiusuário: do gate até a sessão.

O que estes testes travam é uma coisa só, dita de várias formas: **o servidor passou a
saber de quem é o turno, e a recusa continua não contando nada.**

Antes disto, o gate resolvia a identidade e a jogava fora — `aparelho_id` era escrito em
`request.state` e lido por UMA rota, que só o ecoava de volta. Toda a memória (conversa,
lembrete, nota) era gravada sem dono. Estes testes são o que impede isso de voltar.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from mente_digital import aparelhos as regras
from mente_digital import identidade
from mente_digital.registro_aparelhos import RegistroAparelhos


@pytest.fixture()
def registro(tmp_path):
    reg = RegistroAparelhos(str(tmp_path / "aparelhos.db"))
    reg.init()
    return reg


def _parear(reg: RegistroAparelhos, apelido: str, usuario: str, ip: str = "10.0.0.1"):
    codigo = reg.emitir_codigo(apelido, teto=8, usuario=usuario)
    assert codigo is not None, "o convite falhou — teto?"
    res = reg.parear(codigo, ip, teto=8, validade_minutos=10, expira_dias=0)
    assert res.ok, f"pareamento falhou: {res.motivo}"
    return res


def _autorizar(reg: RegistroAparelhos, credencial, ip="10.0.0.1", **kw):
    padrao = dict(habilitado=True, token_legado="tok-legado", aceita_token_legado=False)
    padrao.update(kw)
    return reg.autorizar(credencial, ip, "/api/teste", **padrao)


# --- O que o gate passou a devolver ------------------------------------------
def test_o_gate_devolve_de_quem_e_o_turno(registro):
    """O coração da mudança. Sem isto, tudo o mais é decoração."""
    ana = _parear(registro, "celular da ana", "ana")

    veredito = _autorizar(registro, ana.credencial)

    assert veredito.autorizado
    assert veredito.usuario == "ana"


def test_dois_aparelhos_de_donos_diferentes_nao_se_confundem(registro):
    a = _parear(registro, "cel A", "ana")
    b = _parear(registro, "cel B", "bruno")

    assert _autorizar(registro, a.credencial).usuario == "ana"
    assert _autorizar(registro, b.credencial).usuario == "bruno"


def test_recusa_nao_conta_de_quem_e_o_aparelho(registro):
    """O `id` é PÚBLICO. Se a recusa dissesse o dono, bastava montar
    `mdk1.<id conhecido>.<lixo>` para descobrir a quem o aparelho pertence —
    a mesma classe de vazamento que o `provou_posse` fechou para os motivos."""
    ana = _parear(registro, "celular da ana", "ana")

    veredito = _autorizar(registro, f"mdk1.{ana.aparelho_id}.segredo_totalmente_errado")

    assert not veredito.autorizado
    assert veredito.usuario is None
    assert veredito.motivo_publico == "credencial_invalida"


def test_aparelho_revogado_nao_devolve_dono(registro):
    ana = _parear(registro, "celular da ana", "ana")
    assert registro.revogar(ana.aparelho_id)

    veredito = _autorizar(registro, ana.credencial)

    assert not veredito.autorizado
    assert veredito.usuario is None


# --- O usuário viaja do convite para o aparelho ------------------------------
def test_o_usuario_e_fixado_no_convite_e_herdado_no_pareamento(registro):
    """Quem escolhe é o dono, na máquina dele — não o aparelho que chega."""
    _parear(registro, "cel da ana", "ana")

    (aparelho,) = [a for a in registro.listar() if a.apelido == "cel da ana"]
    assert aparelho.usuario == "ana"


def test_convite_sem_usuario_cai_no_dono_padrao(registro):
    """Preserva o comportamento single-user de hoje: quem não diz nada é o dono."""
    res = _parear(registro, "tablet", "")

    (aparelho,) = [a for a in registro.listar() if a.id == res.aparelho_id]
    assert aparelho.usuario == identidade.DONO_PADRAO


def test_nome_de_usuario_e_normalizado_no_convite(registro):
    """"Ana Maria" vira `ana_maria` — o nome é pasta E coleção do Chroma."""
    codigo = registro.emitir_codigo("cel", teto=8, usuario="Ana Maria")
    res = registro.parear(codigo, "10.0.0.1", teto=8, validade_minutos=10, expira_dias=0)

    (aparelho,) = [a for a in registro.listar() if a.id == res.aparelho_id]
    assert aparelho.usuario == "ana_maria"


@pytest.mark.parametrize("mau", ["../etc", "..\\Windows", "a/b", "x" * 40, "a.b", "-x"])
def test_nome_que_escaparia_do_vault_e_recusado_no_convite(registro, mau):
    """O nome vira `Pessoal/<usuario>/`. Um `..` aqui seria escrita fora do vault.

    ⚠ String VAZIA não entra nesta lista: ela é o caso "não informou", que cai no dono
    padrão de propósito (ver `test_convite_sem_usuario_cai_no_dono_padrao`). Confundir
    "inválido" com "omitido" foi erro do primeiro rascunho DESTE teste, não do código.
    """
    with pytest.raises(ValueError):
        registro.emitir_codigo("cel", teto=8, usuario=mau)


# --- Migração ----------------------------------------------------------------
def test_banco_anterior_ao_multiusuario_ganha_a_coluna_e_o_backfill(tmp_path):
    """Aparelho que já estava pareado é do dono — mesmo raciocínio do SQLite."""
    caminho = str(tmp_path / "legado.db")
    with sqlite3.connect(caminho) as conn:      # o esquema de ANTES, sem `usuario`
        conn.execute(
            """CREATE TABLE aparelhos
               (id TEXT PRIMARY KEY, apelido TEXT, impressao TEXT, sal TEXT,
                criado_em TEXT, ultimo_uso TEXT, ultimo_ip TEXT,
                revogado_em TEXT, expira_em TEXT)"""
        )
        conn.execute(
            "INSERT INTO aparelhos VALUES ('abc','antigo','imp','sal','2026-01-01',"
            "NULL,NULL,NULL,NULL)"
        )

    reg = RegistroAparelhos(caminho)
    reg.init()

    (aparelho,) = reg.listar()
    assert aparelho.usuario == identidade.DONO_PADRAO


def test_init_e_idempotente(registro):
    _parear(registro, "cel", "ana")
    registro.init()
    registro.init()

    assert len(registro.listar()) == 1
    assert registro.listar()[0].usuario == "ana"


# --- O caminho legado continua existindo -------------------------------------
def test_com_a_funcao_desligada_o_dono_padrao_responde(registro):
    """`aparelhos_habilitado=False` é o comportamento de HOJE — e hoje é single-user,
    então quem passa tem de ser o dono padrão, senão o vault de 25 mil notas ficaria
    invisível para ele no instante em que este código subisse."""
    veredito = _autorizar(registro, "tok-legado", habilitado=False,
                          aceita_token_legado=True)

    assert veredito.autorizado
    assert veredito.usuario == identidade.DONO_PADRAO


def test_token_legado_entra_como_o_dono(registro):
    """⚠ E é exatamente por isso que ele precisa ser desligado depois de parear os
    quatro: enquanto ele abre a porta, QUALQUER um que o tenha entra como o dono."""
    veredito = _autorizar(registro, "tok-legado", aceita_token_legado=True)

    assert veredito.autorizado
    assert veredito.usuario == identidade.DONO_PADRAO


# --- O contrato do ContextVar ------------------------------------------------
def test_sem_dono_marcado_o_acesso_a_dado_pessoal_falha_alto():
    """O default é None de propósito: esquecer de marcar tem de QUEBRAR, não herdar
    o dono anterior nem 'o primeiro que aparecer'. É a lição do `acervo_suspeito`
    aplicada antes do estrago — lá a chave ausente escondia a nota; aqui um default
    que 'funciona' mostraria a nota errada."""
    with identidade.usar_dono(None):
        assert identidade.dono_atual() is None
        with pytest.raises(identidade.DonoIndefinido):
            identidade.exigir_dono()


def test_o_dono_nao_vaza_de_um_escopo_para_o_outro():
    with identidade.usar_dono("ana"):
        assert identidade.exigir_dono() == "ana"
        with identidade.usar_dono("bruno"):
            assert identidade.exigir_dono() == "bruno"
        assert identidade.exigir_dono() == "ana"
    assert identidade.dono_atual() is None


def test_task_filha_herda_o_dono_do_pai():
    """É a garantia que faz a marcação ÚNICA no `run()` do WebSocket alcançar todo
    turno: `create_task` copia o contexto vigente. Se isto quebrar, cada turno passa
    a rodar sem dono e o `exigir_dono` derruba a conversa."""
    async def cenario():
        with identidade.usar_dono("ana"):
            return await asyncio.create_task(_quem_sou())

    async def _quem_sou():
        return identidade.dono_atual()

    assert asyncio.run(cenario()) == "ana"


def test_o_mestre_e_o_dono_padrao_e_isso_e_proposital():
    """O mestre ADMINISTRA (parea, revoga, lê a trilha, recebe alertas). Ele não lê a
    memória dos outros — a topologia escolhida pelo dono foi acervo comum + memória
    privada. Se um dia isso mudar, tem de ser decisão explícita, não efeito colateral."""
    assert identidade.MESTRE == identidade.DONO_PADRAO


# --- Bloqueio progressivo continua valendo -----------------------------------
def test_o_castigo_progressivo_sobrevive_a_mudanca(registro):
    """A identidade nova não pode ter afrouxado a defesa contra força bruta."""
    registro.configurar_bloqueio(2.0, 900.0)
    for _ in range(6):
        _autorizar(registro, "mdk1.0123456789abcdef.credencial_errada_aqui", ip="9.9.9.9")

    veredito = _autorizar(registro, "mdk1.0123456789abcdef.credencial_errada_aqui",
                          ip="9.9.9.9")
    assert veredito.motivo == regras.MOTIVO_BLOQUEADO


def test_teto_global_barra_a_varredura_distribuida(registro):
    """O castigo por IP é cego para o distribuído: com N máquinas, cada uma chega com o
    contador zerado e ganha as tentativas livres. A conta diz que ainda assim é inviável
    (1.000 IPs contra 31^10 na janela do código dá P≈1,3e-11) — este teto existe para que
    a defesa não dependa de a entropia do código continuar sendo o que é."""
    registro.configurar_teto_pareamento(5, 600.0)
    for i in range(5):                       # cada uma de um IP DIFERENTE
        registro.parear("CODIGOERRADO", f"10.0.0.{i}", teto=8,
                        validade_minutos=10, expira_dias=0)

    res = registro.parear("CODIGOERRADO", "10.0.0.200", teto=8,
                          validade_minutos=10, expira_dias=0)

    assert not res.ok
    assert res.motivo == regras.MOTIVO_BLOQUEADO


def test_teto_global_nao_impede_o_pareamento_legitimo(registro):
    """O dono tem de conseguir parear enquanto o teto não estourou — um teto que
    atrapalha o uso normal é desligado pelo usuário, e aí não protege nada."""
    registro.configurar_teto_pareamento(30, 600.0)
    registro.parear("ERRADO", "10.0.0.1", teto=8, validade_minutos=10, expira_dias=0)

    res = _parear(registro, "celular da ana", "ana")

    assert res.ok


def test_teto_global_zero_desliga(registro):
    registro.configurar_teto_pareamento(0, 600.0)
    for i in range(20):
        registro.parear("ERRADO", f"10.0.0.{i}", teto=8, validade_minutos=10, expira_dias=0)

    assert _parear(registro, "cel", "ana").ok


def test_o_teto_global_nao_vale_para_o_gate_normal(registro):
    """De propósito: no gate o segredo tem 256 bits, e um teto GLOBAL ali deixaria um
    atacante trancar os quatro usuários de fora — negação de serviço de bandeja."""
    ana = _parear(registro, "cel", "ana")
    registro.configurar_teto_pareamento(1, 600.0)
    for i in range(10):
        _autorizar(registro, "mdk1.0123456789abcdef.errado", ip=f"10.0.1.{i}")

    assert _autorizar(registro, ana.credencial, ip="10.0.2.1").autorizado


# --- O alerta que chega no celular do dono -----------------------------------
def test_o_castigo_avisa_o_dono(registro):
    """Antes, uma rajada de tentativas só existia na tabela `auditoria` — e ninguém lê a
    trilha enquanto o ataque está acontecendo, que é quando ela serviria."""
    recebidos = []
    registro.ao_alerta = lambda t, d, c: recebidos.append((t, d, c))
    registro.configurar_bloqueio(2.0, 900.0)

    for _ in range(6):
        _autorizar(registro, "mdk1.0123456789abcdef.errado", ip="7.7.7.7")

    assert recebidos, "o bloqueio disparou e o dono não foi avisado"
    assert "7.7.7.7" in recebidos[0][2]


def test_as_duas_primeiras_falhas_nao_avisam(registro):
    """São o dedo trocado do próprio dono. Avisar nelas ensina a ignorar o aviso."""
    recebidos = []
    registro.ao_alerta = lambda t, d, c: recebidos.append(c)

    for _ in range(2):
        _autorizar(registro, "mdk1.0123456789abcdef.errado", ip="7.7.7.8")

    assert recebidos == []


def test_alerta_que_explode_nao_derruba_o_gate(registro):
    """Quem está no meio disto é uma requisição sendo RECUSADA. Trocar uma recusa
    correta por um 500 seria transformar o alarme na porta de entrada."""
    def ouvinte_quebrado(*_a):
        raise RuntimeError("o canal de alerta caiu")

    registro.ao_alerta = ouvinte_quebrado
    registro.configurar_bloqueio(2.0, 900.0)

    for _ in range(6):
        veredito = _autorizar(registro, "mdk1.0123456789abcdef.errado", ip="7.7.7.9")

    assert not veredito.autorizado          # segue recusando, sem estourar


def test_expiracao_nao_devolve_dono(registro):
    codigo = registro.emitir_codigo("cel", teto=8, usuario="ana")
    res = registro.parear(codigo, "10.0.0.1", teto=8, validade_minutos=10, expira_dias=1)
    # Empurra o relógio do registro para depois do vencimento.
    registro._agora = lambda: datetime.now() + timedelta(days=2)

    veredito = _autorizar(registro, res.credencial)

    assert not veredito.autorizado
    assert veredito.usuario is None
