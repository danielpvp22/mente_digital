"""
Detector de Contradição (#24): parser puro do veredito, gatilho-mestre e a
persistência (tabela contradicoes: par ordenado, dedup, só as não-resolvidas).
"""
import pytest

from mente_digital import contradicao
from mente_digital import mestre
from mente_digital import telemetry


# ----------------------- parser do veredito (puro) -----------------------
def test_sim_com_motivo():
    assert contradicao.parse_veredito("SIM: uma diz 500, a outra diz 800") == "uma diz 500, a outra diz 800"
    assert contradicao.parse_veredito("SIM - datas opostas") == "datas opostas"


def test_sim_solto_tem_motivo_padrao():
    assert contradicao.parse_veredito("SIM") == "contradição detectada (sem detalhe)"


def test_nao_e_prosa_nao_disparam():
    assert contradicao.parse_veredito("NAO, são complementares") is None
    assert contradicao.parse_veredito("As notas parecem SIM contraditórias") is None  # 'sim' no meio
    assert contradicao.parse_veredito("") is None


# ----------------------- gatilho-mestre -----------------------
def test_comando_contradicoes():
    assert mestre.comando_contradicoes("mestre, alguma contradição?") is True
    assert mestre.comando_contradicoes("tem notas conflitantes?") is True
    assert mestre.comando_contradicoes("qual a capital do Japão?") is False


# ----------------------- persistência -----------------------
@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "t.db"))
    telemetry.db.init()
    return telemetry.db


def test_registra_e_lista(db):
    assert db.registrar_contradicao("a.md", "b.md", "500 vs 800") is True
    abertas = db.contradicoes_abertas()
    assert len(abertas) == 1
    assert abertas[0]["resumo"] == "500 vs 800"


def test_par_e_ordenado_e_deduplicado(db):
    assert db.registrar_contradicao("b.md", "a.md", "primeiro") is True
    # o MESMO par na ordem inversa não vira registro novo (UNIQUE sobre par ordenado)
    assert db.registrar_contradicao("a.md", "b.md", "de novo") is False
    assert len(db.contradicoes_abertas()) == 1


def test_par_degenerado_e_rejeitado(db):
    assert db.registrar_contradicao("a.md", "a.md", "consigo mesmo") is False
    assert db.registrar_contradicao("", "b.md", "sem fonte") is False
    assert db.contradicoes_abertas() == []
