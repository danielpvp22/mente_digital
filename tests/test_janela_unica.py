"""Instância única: a DECISÃO (pura) e a garantia de que o módulo não envenena o CI.

O que se testa aqui é o comportamento, não a escala: `decidir` é a única parte que
não depende de Windows, e é justamente onde mora a regra que o dono pediu
("clicar de novo traz a janela, não sobe outra").
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mente_digital import janela_unica


class TestDecidir:
    """A tabela-verdade do que fazer ao subir."""

    def test_porta_livre_sobe_normal(self):
        assert janela_unica.decidir(False, tem_janela=False, quer_janela=True) == "subir"

    def test_porta_livre_ignora_janela_fantasma(self):
        """Porta livre manda, mesmo que sobre um HWND de uma instância morrendo:
        sem servidor não há o que restaurar."""
        assert janela_unica.decidir(False, tem_janela=True, quer_janela=True) == "subir"

    def test_app_aberto_com_janela_restaura(self):
        """O caso do dono: clicar no atalho com o app já aberto."""
        assert janela_unica.decidir(True, tem_janela=True, quer_janela=True) == "restaurar"

    def test_servidor_sem_janela_adota(self):
        """`python main.py` de pé: não há janela para trazer, então cria uma —
        que é o comportamento que o app.py já tinha e não pode regredir."""
        assert janela_unica.decidir(True, tem_janela=False, quer_janela=True) == "adotar"

    @pytest.mark.parametrize("tem_janela", [True, False])
    def test_quem_nao_quer_janela_nunca_restaura(self, tem_janela):
        """`--standby`/`--oculto`/`--sem-janela`/`--navegador`: trazer algo para a
        frente seria justamente o contrário do pedido."""
        assert janela_unica.decidir(True, tem_janela, quer_janela=False) == "adotar"


class TestGuardasDePlataforma:
    def test_achar_janela_sem_titulo_devolve_zero(self):
        assert janela_unica.achar_janela("") == 0

    def test_trazer_hwnd_nulo_e_falso(self):
        assert janela_unica.trazer_para_frente(0) is False

    def test_fora_do_windows_nao_toca_win32(self, monkeypatch):
        """Com o seam dizendo 'não é Windows', nada de ctypes acontece.

        ⚠ O seam é `janela_unica.e_windows`, NUNCA `os.name` — trocar `os.name` do
        processo faz todo `Path(...)` levantar num runner POSIX e mata a sessão do
        pytest em INTERNALERROR (visto no CI em 2026-08-03)."""
        monkeypatch.setattr(janela_unica, "e_windows", lambda: False)
        assert janela_unica.achar_janela("Mente Digital") == 0
        assert janela_unica.trazer_para_frente(12345) is False

    def test_import_nao_toca_wintypes(self, tmp_path):
        """`ctypes.wintypes` NÃO EXISTE fora do Windows — importá-lo no nível do
        módulo quebraria o CI (ubuntu-latest) sem quebrar nada aqui. Mesma régua
        de `test_jogo_ativo.py`; roda em subprocesso porque outro teste pode já
        ter importado wintypes neste."""
        prova = tmp_path / "prova_wintypes.py"
        prova.write_text(
            "import sys\n"
            "from mente_digital import janela_unica  # noqa: F401\n"
            "print('VAZOU' if 'ctypes.wintypes' in sys.modules else 'ok')\n",
            encoding="utf-8",
        )
        saida = subprocess.run(
            [sys.executable, str(prova)], capture_output=True, text=True,
            cwd=str(tmp_path.parent), env={**os.environ, "PYTHONPATH": os.getcwd()},
        )
        assert "ok" in saida.stdout, (
            f"importar janela_unica puxou ctypes.wintypes — quebra o CI no Linux. "
            f"stdout={saida.stdout!r} stderr={saida.stderr!r}")
