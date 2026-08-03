"""
A rota `/api/acesso` — a sondagem que separa RECUSA de queda de rede.

Por que ela existe, medido em 2026-08-03 (uvicorn + Chrome, `close(1008)` nas duas
posições possíveis do gate do WebSocket):

    recusa ANTES do accept  ->  o uvicorn responde HTTP 403 ao handshake e o
                                navegador entrega `code=1006, reason=""`
    recusa DEPOIS do accept ->  chega o 1008 de verdade, com motivo

O primeiro caso é o do aparelho REVOGADO que reabre o app — e ele é, no JS, byte a
byte igual ao WiFi caído. Sem uma pergunta em HTTP, o front só pode reconectar para
sempre contra um servidor que já disse não.

Então o que estes testes travam é o CONTRATO de que o front depende:
1. a rota é gateada (não vira oráculo para quem não passou);
2. o 401 traz o MOTIVO no corpo (é o que separa "revogado" de "digitei errado");
3. o motivo é o público — id inexistente e segredo errado respondem igual.

TestClient SEM o context manager, como os vizinhos: o lifespan não roda e a suíte
não carrega modelo nenhum.
"""
from __future__ import annotations

import pytest

from mente_digital import aparelhos as regras
from mente_digital.registro_aparelhos import RegistroAparelhos


class DbEspiao:
    def registrar_auditoria(self, acao: str, detalhe: str) -> None:
        pass


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    # Sem token legado o gate libera o loopback — e "testclient" está na lista.
    monkeypatch.setattr(main.settings, "access_token", "")
    monkeypatch.setattr(main.settings, "aparelhos_habilitado", True)
    monkeypatch.setattr(main.settings, "aparelhos_token_legado", True)
    registro = RegistroAparelhos(str(tmp_path / "aparelhos.db"), db=DbEspiao())
    registro.init()
    main.app.state.registro = registro
    yield TestClient(main.app), registro
    del main.app.state.registro


def _parear(registro: RegistroAparelhos) -> str:
    codigo = registro.emitir_codigo("celular", teto=4)
    r = registro.parear(codigo, ip="10.0.0.9", teto=4, validade_minutos=10, expira_dias=90)
    assert r.ok, r.motivo
    return r.credencial


def test_loopback_passa_e_diz_que_nao_tem_identidade(cliente):
    """A máquina do dono entra sem credencial — e a resposta diz `aparelho_id: None`
    em vez de inventar uma. É essa distinção que deixa o app avisar "você ainda está
    no acesso antigo" em vez de fingir que já migrou."""
    c, _ = cliente
    r = c.get("/api/acesso")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["ok"] is True
    assert corpo["aparelho_id"] is None
    assert corpo["aparelhos_habilitado"] is True


def test_credencial_valida_diz_QUAL_aparelho(cliente):
    c, registro = cliente
    credencial = _parear(registro)
    r = c.get("/api/acesso", headers={"X-Mente-Token": credencial})
    assert r.status_code == 200
    esperado = regras.partir_credencial(credencial)[0]
    assert r.json()["aparelho_id"] == esperado


def test_revogado_responde_401_COM_O_MOTIVO(cliente):
    """O teste que sustenta o conserto do front: sem `motivo` no corpo, o celular
    revogado e o celular com o token errado contam a mesma história — e só o
    segundo tem conserto do lado de quem está segurando o aparelho."""
    c, registro = cliente
    credencial = _parear(registro)
    registro.revogar(regras.partir_credencial(credencial)[0])

    r = c.get("/api/acesso", headers={"X-Mente-Token": credencial})
    assert r.status_code == 401
    assert r.json()["detail"]["motivo"] == regras.MOTIVO_REVOGADO


def test_id_de_revogado_com_segredo_CHUTADO_nao_confirma_a_revogacao(cliente):
    """O buraco que a revisão adversária de 2026-08-03 achou (pré-existente ao PR):
    o `id` é PÚBLICO — aparece na tela de pareamento do celular e em `/api/aparelhos`
    —, e a credencial é `mdk1.<id>.<segredo>`. Bastava montar
    `mdk1.<id conhecido>.<lixo>` para o servidor confirmar que aquele aparelho está
    revogado, sem acertar segredo nenhum. O bloqueio progressivo impede VARRER ids,
    não uma sondagem única contra um id que a pessoa já viu.

    Quem tem o segredo continua vendo "revogado" (é o teste acima) — é ele que faz o
    app mostrar a tela certa. Quem só chutou recebe o genérico."""
    c, registro = cliente
    credencial = _parear(registro)
    aparelho_id = regras.partir_credencial(credencial)[0]
    registro.revogar(aparelho_id)

    chute = regras.montar_credencial(aparelho_id, regras.gerar_segredo())
    r = c.get("/api/acesso", headers={"X-Mente-Token": chute})
    assert r.status_code == 401
    assert r.json()["detail"]["motivo"] == "credencial_invalida"


def test_a_auditoria_do_dono_continua_sabendo_que_era_revogado():
    """O motivo GROSSO é só da resposta HTTP. Na trilha, o dono precisa distinguir
    "sondaram um aparelho que eu revoguei" de "chutaram um id que não existe" —
    esconder isso dele seria trocar um problema por outro."""
    from datetime import datetime

    ap = regras.Aparelho(id="a" * 16, apelido="celular", impressao="x", sal="y",
                         criado_em="2026-08-03T00:00:00", revogado_em="2026-08-03T01:00:00")
    v = regras.avaliar(regras.Situacao(
        habilitado=True, host="10.0.0.9",
        credencial=regras.montar_credencial("a" * 16, regras.gerar_segredo()),
        token_legado="", aceita_token_legado=True, agora=datetime(2026, 8, 3, 2, 0),
        aparelho=ap, segredo_confere=False))

    assert v.motivo == regras.MOTIVO_REVOGADO          # trilha: fina
    assert v.motivo_publico == "credencial_invalida"   # resposta: grossa


def test_id_inexistente_nao_vira_oraculo_de_enumeracao(cliente):
    """Sondar um id que não existe tem de responder IGUAL a acertar o id e errar o
    segredo. Se diferisse, a rota de sondagem viraria a ferramenta mais barata para
    descobrir quais aparelhos existem."""
    c, registro = cliente
    credencial = _parear(registro)
    aparelho_id = regras.partir_credencial(credencial)[0]

    inexistente = c.get("/api/acesso", headers={
        "X-Mente-Token": regras.montar_credencial("0" * 16, regras.gerar_segredo())})
    segredo_errado = c.get("/api/acesso", headers={
        "X-Mente-Token": regras.montar_credencial(aparelho_id, regras.gerar_segredo())})

    assert inexistente.status_code == segredo_errado.status_code == 401
    assert (inexistente.json()["detail"]["motivo"]
            == segredo_errado.json()["detail"]["motivo"]
            == "credencial_invalida")


def test_sem_registro_a_rota_cai_no_gate_de_sempre(cliente, monkeypatch):
    """App montada sem lifespan (o caminho de vários testes de rota e de qualquer
    boot pela metade): sem registro, o gate é o `acesso.cliente_autorizado` de
    sempre. Estourar aqui trocaria um gate que funciona por um 500 em toda rota."""
    import main

    c, _ = cliente
    del main.app.state.registro
    monkeypatch.setattr(main.settings, "access_token", "segredo-do-dono")
    try:
        assert c.get("/api/acesso").status_code == 401
        r = c.get("/api/acesso", headers={"X-Mente-Token": "segredo-do-dono"})
        assert r.status_code == 200 and r.json()["aparelho_id"] is None
    finally:
        main.app.state.registro = None      # o fixture faz o `del` no teardown
