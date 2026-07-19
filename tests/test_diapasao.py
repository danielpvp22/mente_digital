"""
Diapasão (#36): perfil de COMO o dono prefere conversar. Parser puro, a instrução
que ele vira, o gatilho-mestre e a persistência (chave única 'conversa').
"""
import pytest

import diapasao
import mestre
import telemetry


# ----------------------- parser puro -----------------------
def test_perfil_extraido_e_capado():
    p = diapasao.parse_perfil("  Prefere respostas curtas e diretas, com exemplos práticos.  ")
    assert p == "Prefere respostas curtas e diretas, com exemplos práticos."


def test_nada_vira_none():
    assert diapasao.parse_perfil("NADA") is None
    assert diapasao.parse_perfil("nada.") is None
    assert diapasao.parse_perfil("") is None


def test_limite_de_tamanho():
    longo = "x" * 1000
    assert len(diapasao.parse_perfil(longo)) == 400


# ----------------------- a instrução gerada -----------------------
def test_instrucao_do_perfil():
    instr = diapasao.instrucao_do_perfil("gosta de analogias")
    assert "gosta de analogias" in instr
    assert "nao imite" in instr.lower() or "não imite" in instr.lower()


def test_instrucao_vazia_sem_perfil():
    assert diapasao.instrucao_do_perfil(None) == ""
    assert diapasao.instrucao_do_perfil("  ") == ""


# ----------------------- gatilho-mestre -----------------------
def test_comando_perfil():
    assert mestre.comando_perfil("mestre, como você me vê?") is True
    assert mestre.comando_perfil("qual meu perfil de conversa?") is True
    assert mestre.comando_perfil("que horas são?") is False


# ----------------------- persistência -----------------------
@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry.db, "path", str(tmp_path / "t.db"))
    telemetry.db.init()
    return telemetry.db


def test_salva_e_le_perfil(db):
    assert db.ler_perfil() is None
    db.salvar_perfil("respostas curtas")
    assert db.ler_perfil() == "respostas curtas"


def test_perfil_e_sobrescrito_nao_duplicado(db):
    db.salvar_perfil("versão 1")
    db.salvar_perfil("versão 2")
    assert db.ler_perfil() == "versão 2"   # chave única 'conversa' -> upsert
