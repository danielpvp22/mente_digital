"""O bilhete do plantão vira CONVERSA — e o rótulo da bandeja.

A segunda metade de `vez.py`. A primeira (o vigia recusar e guardar) só é
aceitável porque existe esta: sem o dreno, o pedido fica num arquivo que ninguém
lê, e o resultado para quem pede seria PIOR que o de antes — um "não" definitivo
em vez de um PC que sobe.
"""
from __future__ import annotations

import asyncio

import pytest

from mente_digital import identidade, mensageiro, vez


class _Sessao:
    def __init__(self, usuario=None):
        self.usuario = usuario


class _SchedFake:
    """O mínimo do `SchedulerService` que o dreno toca. Herdar do real puxaria o
    ctx inteiro; o que está sob teste é a ORQUESTRAÇÃO, não a entrega."""

    def __init__(self, arquivo, jogo=None):
        self._arquivo = arquivo
        self._jogo = jogo
        self.entregues = []
        self.gravadas = []

    # as peças reais, copiadas por composição no teste
    _drenar_pedidos_de_acesso = None      # preenchido no fixture


@pytest.fixture
def sched(tmp_path, monkeypatch):
    """Monta o dreno real sobre um arquivo temporário e um banco falso."""
    from mente_digital import scheduler as sch

    class S(sch.SchedulerService):
        def __init__(self):                       # noqa: D107 - sem o ctx real
            self.entregues = []
            self.gravadas = []
            self._jogo = None
            self._arquivo = tmp_path / "dados" / "pedidos_de_acesso.jsonl"

        def _arquivo_pedidos(self):
            return self._arquivo

        def _jogo_rodando(self):
            return bool(self._jogo)

        async def entregar_mensagem(self, msg):
            self.entregues.append(msg)
            return True

    def _salvar(remetente, destinatario, texto, tipo, criada_em):
        alvo.gravadas.append((remetente, destinatario, texto, tipo))
        return len(alvo.gravadas)

    alvo = S()
    monkeypatch.setattr(sch.db, "salvar_mensagem", _salvar)
    return alvo


def _escrever(sched, *pedidos):
    caminho = sched._arquivo
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text("\n".join(vez.linha(p) for p in pedidos) + "\n", encoding="utf-8")


def test_sem_arquivo_o_dreno_nao_faz_nada(sched):
    """O caminho normal — ninguém tentou entrar. Roda a cada tique do scheduler,
    então tem de custar um `stat` e sair."""
    assert asyncio.run(sched._drenar_pedidos_de_acesso()) == 0
    assert sched.gravadas == []


def test_uma_pessoa_vira_UMA_mensagem_para_o_mestre(sched):
    """Cinco tiques da tela de carregamento de um celular são uma pessoa esperando.
    Cinco mensagens fariam o dono achar que a casa inteira bateu na porta."""
    _escrever(sched, *[vez.Pedido("ana", f"2026-08-08T21:4{i}:00") for i in range(5)])
    assert asyncio.run(sched._drenar_pedidos_de_acesso()) == 1
    para_o_mestre = [g for g in sched.gravadas if g[1] == identidade.MESTRE]
    assert len(para_o_mestre) == 1
    remetente, _, texto, tipo = para_o_mestre[0]
    assert remetente == "ana"
    assert tipo == mensageiro.TIPO_ACESSO
    assert "5x" in texto and "21:44" in texto


def test_o_remetente_e_o_PEDINTE_para_o_responder_funcionar(sched):
    """`mensageiro.responder` deriva as pontas da mensagem original. Se o remetente
    fosse um nome de serviço ("sistema"), a estimativa que o dono digitasse não
    chegaria a pessoa nenhuma — e ele não teria como saber disso."""
    _escrever(sched, vez.Pedido("ana", "2026-08-08T21:40:00"))
    asyncio.run(sched._drenar_pedidos_de_acesso())
    original = sched.entregues[0]
    remetente, destinatario, _ = mensageiro.responder(original, "já libero")
    assert remetente == identidade.MESTRE
    assert destinatario == "ana"


def test_duas_pessoas_viram_duas_conversas(sched):
    """Uma mensagem só, somando as duas, não teria a quem responder."""
    _escrever(sched, vez.Pedido("ana", "2026-08-08T21:40:00"),
              vez.Pedido("bruno", "2026-08-08T21:41:00"))
    assert asyncio.run(sched._drenar_pedidos_de_acesso()) == 2
    remetentes = {g[0] for g in sched.gravadas if g[1] == identidade.MESTRE}
    assert remetentes == {"ana", "bruno"}


def test_com_o_jogo_FECHADO_o_pedinte_e_avisado_de_que_liberou(sched):
    """A promessa que fecha o ciclo. Sem ela a pessoa fica esperando um aviso que
    nunca vem e volta a bater na porta por conta."""
    sched._jogo = None
    _escrever(sched, vez.Pedido("ana", "2026-08-08T21:40:00"))
    asyncio.run(sched._drenar_pedidos_de_acesso())
    para_ana = [g for g in sched.gravadas if g[1] == "ana"]
    assert len(para_ana) == 1
    assert "disponível" in para_ana[0][2]


def test_com_o_jogo_ABERTO_so_o_dono_e_avisado(sched):
    """O app subiu por outro caminho (o dono abriu). Prometer disponibilidade no
    meio de uma partida seria mentir para a pessoa errada."""
    sched._jogo = "escapefromtarkov.exe"
    _escrever(sched, vez.Pedido("ana", "2026-08-08T21:40:00"))
    asyncio.run(sched._drenar_pedidos_de_acesso())
    assert [g[1] for g in sched.gravadas] == [identidade.MESTRE]


def test_sem_identidade_ninguem_recebe_o_liberou(sched):
    """⚠ Sem usuário não há caixa para entregar. Mandar o "liberou" ao
    destinatário padrão faria o MESTRE receber um aviso escrito para outra
    pessoa — e ele é justamente quem já sabe que liberou."""
    sched._jogo = None
    _escrever(sched, vez.Pedido("alguém", "2026-08-08T21:40:00"))
    asyncio.run(sched._drenar_pedidos_de_acesso())
    assert all(g[1] == identidade.MESTRE for g in sched.gravadas)


def test_o_arquivo_e_APAGADO_para_o_recado_nao_repetir(sched):
    """⚠ Apagar ANTES de processar. Se a entrega falhar perde-se o AVISO, mas a
    linha já está na tabela `mensagens`, que é a fonte durável. O contrário é que
    seria grave: um erro no meio deixaria o arquivo intacto e o dono receberia o
    mesmo recado a cada 20 s, para sempre."""
    _escrever(sched, vez.Pedido("ana", "2026-08-08T21:40:00"))
    asyncio.run(sched._drenar_pedidos_de_acesso())
    assert not sched._arquivo.exists()
    sched.gravadas.clear()
    assert asyncio.run(sched._drenar_pedidos_de_acesso()) == 0
    assert sched.gravadas == []


def test_arquivo_so_com_lixo_nao_gera_recado(sched):
    """Um arquivo meio escrito não pode virar 'alguém quis usar' sem alguém."""
    sched._arquivo.parent.mkdir(parents=True, exist_ok=True)
    sched._arquivo.write_text("{quebrado\nnão é json\n", encoding="utf-8")
    assert asyncio.run(sched._drenar_pedidos_de_acesso()) == 0
    assert sched.gravadas == []


# --------------------------------------------------------------------------- #
# O rótulo da bandeja                                                          #
# --------------------------------------------------------------------------- #
def test_o_rotulo_sai_das_sessoes_vivas():
    assert vez.resumo_de_uso(s.usuario for s in
                             [_Sessao("ana"), _Sessao(None)]) == "ana está usando agora"


def test_sessao_sem_usuario_nao_vira_None_no_icone():
    """⚠ Com o multiusuário DESLIGADO nenhuma sessão tem `usuario`, e esse é o
    caminho default. `str(None)` é a string 'None', que é verdadeira — sem o
    filtro por tipo o dono leria 'None está usando agora' na bandeja."""
    assert vez.resumo_de_uso(s.usuario for s in [_Sessao(None), _Sessao(None)]) == ""


def test_o_usando_vence_o_descansando_no_rotulo():
    """Se há alguém usando, o assistente por definição não está descansando —
    mostrar as duas coisas juntas faria o dono duvidar da que importa."""
    from mente_digital import bandeja as bnd

    b = bnd.Bandeja.__new__(bnd.Bandeja)
    b.ativa, b._icone = False, None
    b.ligado, b.consolidando, b.usando = False, False, ""
    b.marcar(ligado=False, usando="ana está usando agora")
    assert b.usando == "ana está usando agora"
    assert b.ligado is False          # o estado de energia não é sobrescrito
