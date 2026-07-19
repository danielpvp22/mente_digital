"""
Testes da Auto-recuperação de Índice (#33) — o helper mover_indice_corrompido.
Só toca um diretório temporário; nunca o vault. Sem GPU, sem Chroma real.
"""
import os

import rag


def _criar_banco(diretorio: str, marcador: str = "chroma.sqlite3") -> None:
    os.makedirs(diretorio, exist_ok=True)
    with open(os.path.join(diretorio, marcador), "w", encoding="utf-8") as f:
        f.write("bytes corrompidos")


def test_move_indice_para_corrompido(tmp_path):
    banco = str(tmp_path / "banco_vetorial")
    _criar_banco(banco)

    destino = rag.mover_indice_corrompido(banco)

    assert destino == banco + ".corrompido"
    assert not os.path.exists(banco)              # o dir original saiu do caminho
    assert os.path.isfile(os.path.join(destino, "chroma.sqlite3"))  # preservado p/ inspeção


def test_dir_inexistente_devolve_none(tmp_path):
    assert rag.mover_indice_corrompido(str(tmp_path / "nao_existe")) is None


def test_corrompido_anterior_e_substituido(tmp_path):
    banco = str(tmp_path / "banco_vetorial")
    # já existe um .corrompido de uma recuperação passada, com conteúdo VELHO
    _criar_banco(banco + ".corrompido", marcador="velho.txt")
    _criar_banco(banco, marcador="novo.txt")

    destino = rag.mover_indice_corrompido(banco)

    assert destino == banco + ".corrompido"
    # só a cópia MAIS RECENTE sobrevive — a velha foi descartada (não vaza disco)
    assert os.path.isfile(os.path.join(destino, "novo.txt"))
    assert not os.path.exists(os.path.join(destino, "velho.txt"))
