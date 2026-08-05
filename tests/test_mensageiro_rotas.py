"""As rotas do mensageiro — e o GATE das rotas de mestre.

A decisão que este arquivo trava: as rotas de mestre usam `exigir_acesso` +
IDENTIDADE DE MESTRE, e não `exigir_loopback` como as de aparelho.

O `exigir_loopback` guarda o CONTROLE DE ACESSO (convidar, revogar) e ali é obrigatório
— um celular já pareado passaria no gate normal e poderia inscrever o quinto aparelho ou
revogar os outros três. A caixa de mensagens não é isso: é conteúdo, a mesma classe de
`/api/conversas`. E tem a razão funcional que decide: pelo Tailscale o dono NÃO é
loopback, então com aquele gate ele receberia o push no celular e só poderia responder na
escrivaninha — meio canal, justamente no caso em que alguém está travado e precisa dele
agora.

O preço dessa escolha está escrito no `exigir_mestre` e é honesto: enquanto o token
legado abrir a porta, quem o tem entra COMO o dono. Isso já vale para `/api/conversas` e
para toda linha pessoal do banco — não é uma fraqueza que este gate introduz.

TestClient SEM o context manager, como os vizinhos (`test_acesso_rota.py`): o lifespan
não roda e a suíte não carrega modelo nenhum.
"""
from __future__ import annotations

import pytest

from mente_digital import identidade, mensageiro, telemetry
from mente_digital.registro_aparelhos import RegistroAparelhos


class DbEspiao:
    def registrar_auditoria(self, acao: str, detalhe: str) -> None:
        pass


class SchedulerFake:
    """Só registra o que a rota mandou entregar. A entrega DE VERDADE (fila, reentrega,
    ack) é exercitada contra o `SchedulerService` real em `test_mensageiro_entrega.py`;
    reimplementá-la aqui seria testar a cópia."""

    def __init__(self) -> None:
        self.entregues: list[mensageiro.Mensagem] = []

    async def entregar_mensagem(self, msg: mensageiro.Mensagem) -> bool:
        self.entregues.append(msg)
        return True


class CtxFake:
    def __init__(self) -> None:
        self.scheduler = SchedulerFake()
        self.tarefas: list = []

    def track_task(self, coro):
        import asyncio

        tarefa = asyncio.ensure_future(coro)
        self.tarefas.append(tarefa)
        return tarefa


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "rotas_msg.db"))
    telemetry.db.init()
    # Sem token legado o gate libera o loopback — e "testclient" está na lista. Com a
    # identidade por aparelho LIGADA, quem chega sem credencial é o dono padrão (= o
    # mestre) e quem chega com a credencial da Ana é a Ana.
    monkeypatch.setattr(main.settings, "access_token", "")
    monkeypatch.setattr(main.settings, "aparelhos_habilitado", True)
    monkeypatch.setattr(main.settings, "aparelhos_token_legado", True)
    monkeypatch.setattr(main.settings, "multiusuario_habilitado", True)
    registro = RegistroAparelhos(str(tmp_path / "aparelhos.db"), db=DbEspiao())
    registro.init()
    main.app.state.registro = registro
    main.app.state.ctx = CtxFake()
    yield TestClient(main.app), registro
    del main.app.state.registro
    del main.app.state.ctx


def _credencial(registro: RegistroAparelhos, usuario: str) -> str:
    """Um aparelho pareado PARA `usuario` — é assim que o servidor sabe quem é quem."""
    codigo = registro.emitir_codigo("celular", teto=4, usuario=usuario)
    r = registro.parear(codigo, ip="10.0.0.9", teto=4, validade_minutos=10, expira_dias=90)
    assert r.ok, r.motivo
    return r.credencial


def _como(credencial: str) -> dict:
    return {"X-Mente-Token": credencial}


# ==========================================================================
# Enviar e listar
# ==========================================================================
def test_enviar_grava_e_pede_a_entrega_ao_vivo(cliente):
    """O caminho feliz do botão "reportar bug": grava (durável) e empurra (ao vivo),
    nessa ordem — o push nunca é a fonte da verdade."""
    import main

    c, registro = cliente
    r = c.post("/api/mensagens", json={"texto": "a voz travou", "tipo": "bug"},
               headers=_como(_credencial(registro, "ana")))

    assert r.status_code == 200
    corpo = r.json()["mensagem"]
    assert corpo["remetente"] == "ana"
    assert corpo["destinatario"] == identidade.MESTRE     # não vem do corpo do pedido
    assert corpo["tipo"] == mensageiro.TIPO_BUG and corpo["lida"] is False
    assert main.app.state.ctx.scheduler.entregues[0].texto == "a voz travou"


def test_texto_vazio_e_tipo_desconhecido_falham_na_mao_de_quem_digitou(cliente):
    c, registro = cliente
    cred = _como(_credencial(registro, "ana"))
    assert c.post("/api/mensagens", json={"texto": "  "}, headers=cred).status_code == 400
    assert c.post("/api/mensagens", json={"texto": "oi", "tipo": "urgente"},
                  headers=cred).status_code == 400


def test_um_usuario_nao_ve_a_mensagem_do_outro_pela_rota(cliente):
    """O vazamento silencioso, agora ponta a ponta. A Ana e o Bob reportam; a caixa de
    cada um traz só a dele."""
    c, registro = cliente
    ana, bob = _como(_credencial(registro, "ana")), _como(_credencial(registro, "bob"))
    c.post("/api/mensagens", json={"texto": "segredo da ana"}, headers=ana)
    c.post("/api/mensagens", json={"texto": "segredo do bob"}, headers=bob)

    caixa = c.get("/api/mensagens", headers=ana).json()
    assert caixa["usuario"] == "ana"
    assert [m["texto"] for m in caixa["mensagens"]] == ["segredo da ana"]


# ==========================================================================
# O gate do mestre
# ==========================================================================
def test_a_caixa_do_mestre_recusa_quem_nao_e_o_mestre(cliente):
    """403 e não 404: quem chegou aqui está autenticado, e esconder a rota de um usuário
    legítimo só o faria reportar um bug que não existe."""
    c, registro = cliente
    ana = _como(_credencial(registro, "ana"))
    assert c.get("/api/mestre/mensagens", headers=ana).status_code == 403
    assert c.post("/api/mestre/mensagens/1/responder", json={"texto": "oi"},
                  headers=ana).status_code == 403


def test_o_mestre_ve_o_que_todos_mandaram_para_ele(cliente):
    """"Listar tudo" quer dizer tudo que é DELE. Ele recebe de todos porque todos
    escrevem para ele — consequência do desenho, não um poder de leitura."""
    c, registro = cliente
    c.post("/api/mensagens", json={"texto": "da ana", "tipo": "bug"},
           headers=_como(_credencial(registro, "ana")))
    c.post("/api/mensagens", json={"texto": "do bob", "tipo": "pedido"},
           headers=_como(_credencial(registro, "bob")))

    caixa = c.get("/api/mestre/mensagens").json()      # loopback = o dono = o mestre
    assert {m["texto"] for m in caixa["mensagens"]} == {"da ana", "do bob"}
    assert caixa["nao_lidas"] == 2


def test_o_mestre_nao_ve_a_conversa_entre_outros_dois_pela_rota(cliente):
    """O espelho na rota do teste puro: gravada por baixo (nenhuma rota cria uma linha
    A->B), a mensagem entre Ana e Bob NÃO aparece na caixa do mestre. É este caso que
    separa "recebe tudo" de "lê tudo"."""
    c, _registro = cliente
    with identidade.usar_dono("ana"):
        telemetry.db.salvar_mensagem("ana", "bob", "entre ana e bob",
                                     mensageiro.TIPO_LIVRE, mensageiro.agora_iso())

    caixa = c.get("/api/mestre/mensagens").json()
    assert caixa["mensagens"] == []


# ==========================================================================
# Ler e responder
# ==========================================================================
def test_nao_lida_vira_lida_e_repetir_nao_muda_nada(cliente):
    c, registro = cliente
    enviada = c.post("/api/mensagens", json={"texto": "olha isto"},
                     headers=_como(_credencial(registro, "ana"))).json()["mensagem"]

    assert c.post(f"/api/mensagens/{enviada['id']}/lida").json() == {"lida": True}
    assert c.post(f"/api/mensagens/{enviada['id']}/lida").json() == {"lida": False}
    assert c.get("/api/mestre/mensagens").json()["nao_lidas"] == 0


def test_id_alheio_nao_abre_nem_marca(cliente):
    """404 tanto para "não existe" quanto para "não é sua" — distinguir os dois casos
    faria a rota contar quantas mensagens existem na casa."""
    c, registro = cliente
    do_bob = c.post("/api/mensagens", json={"texto": "segredo do bob"},
                    headers=_como(_credencial(registro, "bob"))).json()["mensagem"]

    ana = _como(_credencial(registro, "ana"))
    assert c.post(f"/api/mensagens/{do_bob['id']}/lida", headers=ana).status_code == 404
    assert c.post("/api/mensagens/99999/lida", headers=ana).status_code == 404


def test_quem_escreveu_nao_marca_a_propria_como_lida(cliente):
    """A rota vê a mensagem (é dela, o remetente é ele), mas o `pode_marcar_lida` recusa:
    marcar aqui apagaria o não-lido da caixa do mestre sem ele ter aberto nada."""
    c, registro = cliente
    ana = _como(_credencial(registro, "ana"))
    enviada = c.post("/api/mensagens", json={"texto": "oi"}, headers=ana).json()["mensagem"]

    r = c.post(f"/api/mensagens/{enviada['id']}/lida", headers=ana).json()
    assert r == {"lida": False, "motivo": mensageiro.MOTIVO_ALHEIA}
    assert c.get("/api/mestre/mensagens").json()["nao_lidas"] == 1


def test_responder_inverte_as_pontas_e_marca_a_original_como_lida(cliente):
    """Responder É ler: sem isso a caixa continuaria acusando não-lido para o que o
    mestre acabou de responder. E as pontas saem da original, não de um `para` no corpo."""
    import main

    c, registro = cliente
    ana = _como(_credencial(registro, "ana"))
    original = c.post("/api/mensagens", json={"texto": "não consigo entrar", "tipo": "bug"},
                      headers=ana).json()["mensagem"]

    r = c.post(f"/api/mestre/mensagens/{original['id']}/responder",
               json={"texto": "já liberei"})
    assert r.status_code == 200
    resposta = r.json()["mensagem"]
    assert resposta["remetente"] == identidade.MESTRE and resposta["destinatario"] == "ana"
    assert main.app.state.ctx.scheduler.entregues[-1].destinatario == "ana"
    assert c.get("/api/mestre/mensagens").json()["nao_lidas"] == 0
    # E a Ana vê as duas na caixa dela — a que mandou e a que recebeu.
    assert len(c.get("/api/mensagens", headers=ana).json()["mensagens"]) == 2


def test_responder_id_inexistente_da_404(cliente):
    c, _registro = cliente
    assert c.post("/api/mestre/mensagens/4242/responder",
                  json={"texto": "oi"}).status_code == 404


def test_limite_negativo_nao_anula_o_teto(cliente):
    """`LIMIT -1` no SQLite significa SEM limite, e o número vem do cliente: sem o piso
    do `_pagina`, um `?limite=-1` viraria a forma de pedir a tabela inteira. O filtro por
    dono continua valendo (não é vazamento), mas o teto existe para não virar uma
    varredura por requisição."""
    c, registro = cliente
    ana = _como(_credencial(registro, "ana"))
    c.post("/api/mensagens", json={"texto": "primeira"}, headers=ana)
    c.post("/api/mensagens", json={"texto": "segunda"}, headers=ana)

    r = c.get("/api/mensagens?limite=-1", headers=ana).json()
    assert [m["texto"] for m in r["mensagens"]] == ["segunda"]


# ==========================================================================
# Degradação
# ==========================================================================
def test_sem_agendador_a_mensagem_ainda_e_gravada(cliente):
    """App montada sem lifespan (o caminho de vários testes e de qualquer boot pela
    metade): sem `ctx`, não há push — mas a mensagem existe e aparece na caixa. Falhar a
    rota aqui trocaria uma notificação por uma mensagem não enviada."""
    import main

    c, registro = cliente
    main.app.state.ctx = None

    r = c.post("/api/mensagens", json={"texto": "sem agendador"},
               headers=_como(_credencial(registro, "ana")))

    assert r.status_code == 200
    assert [m["texto"] for m in c.get("/api/mestre/mensagens").json()["mensagens"]] \
        == ["sem agendador"]
