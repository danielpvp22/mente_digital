"""O atalho fixável do Windows: montagem PURA em qualquer plataforma + o `.lnk`
de verdade no Windows.

⚠ NOME: `test_atalho_windows`, não `test_atalho` — este último JÁ EXISTE e cobre
outra coisa (o atalho de intenção falada do `mestre.py`). Ver a docstring de
`mente_digital/atalho_windows.py`.

O teste que importa de fato é `test_aumid_volta_na_releitura`: "escreveu o
arquivo" não é a mesma afirmação que "o Windows vai casar o pin", e é a segunda
que conserta o print do dono. Ele só roda no Windows — o CI é ubuntu-latest e lá
não existe shell para perguntar.
"""
from __future__ import annotations

import ctypes
import inspect
import os
import sys
from pathlib import Path

import pytest

from mente_digital import atalho_windows as atalho

so_windows = pytest.mark.skipif(os.name != "nt", reason="COM/shell só existe no Windows")


class TestMontagemPura:
    """Roda em qualquer plataforma — inclusive no CI Linux."""

    def test_guid_conhecido_vira_os_bytes_certos(self):
        """O GUID é montado em Python (não por `CLSIDFromString`) justamente para
        ser conferível aqui. Passando isto, a constante que o property store
        recebe está certa byte a byte."""
        g = atalho.guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}")
        assert g.Data1 == 0x9F4C2855
        assert g.Data2 == 0x9F79
        assert g.Data3 == 0x4B39
        assert bytes(g.Data4) == bytes.fromhex("A8D0E1D42DE1D5F3")

    def test_guid_aceita_com_e_sem_chaves(self):
        com = atalho.guid("{00021401-0000-0000-C000-000000000046}")
        sem = atalho.guid("00021401-0000-0000-C000-000000000046")
        assert bytes(com) == bytes(sem)

    def test_guid_malformado_levanta(self):
        with pytest.raises(ValueError):
            atalho.guid("nao-e-um-guid")

    def test_estruturas_tem_o_tamanho_que_o_com_espera(self):
        """PROPERTYKEY = GUID(16) + DWORD(4). PROPVARIANT = cabeçalho(8) + 2
        ponteiros. Errar isso não dá erro: o COM lê lixo do lado e grava outra
        coisa."""
        assert ctypes.sizeof(atalho._PROPERTYKEY) == 20
        assert ctypes.sizeof(atalho._PROPVARIANT) == 8 + 2 * ctypes.sizeof(ctypes.c_void_p)

    def test_argumentos_ficam_entre_aspas(self):
        """Caminho com espaço sem aspas vira dois argumentos e o Python não acha o
        script — falha só na máquina de quem tem espaço no caminho."""
        args = atalho.argumentos(Path("D:/meus projetos/mente"))
        assert args.startswith('"') and args.endswith('"')
        assert "app.py" in args

    def test_argumentos_aceita_outro_alvo(self):
        assert "main.py" in atalho.argumentos(Path("D:/x"), alvo="main.py")

    def test_nome_e_o_que_a_busca_do_windows_mostra(self):
        assert atalho.NOME_ATALHO == "Mente Digital.lnk"


class TestGuardaDePlataforma:
    """⚠ O seam é `atalho.e_windows`, NUNCA `os.name` — trocar `os.name` do
    processo faz o pathlib explodir num runner POSIX e mata a sessão do pytest
    (ver conftest)."""

    def test_instalar_fora_do_windows_recusa(self, monkeypatch):
        monkeypatch.setattr(atalho, "e_windows", lambda: False)
        with pytest.raises(RuntimeError, match="Windows"):
            atalho.instalar(Path("D:/x"), "App.Id.1")

    def test_criar_lnk_fora_do_windows_recusa(self, monkeypatch):
        monkeypatch.setattr(atalho, "e_windows", lambda: False)
        with pytest.raises(RuntimeError, match="Windows"):
            atalho.criar_lnk(Path("x.lnk"), "py.exe", "", "", "", "App.Id.1", "d")

    def test_ler_aumid_fora_do_windows_e_none(self, monkeypatch):
        monkeypatch.setattr(atalho, "e_windows", lambda: False)
        assert atalho.ler_aumid(Path("x.lnk")) is None

    def test_area_trabalho_fora_do_windows_cai_no_perfil(self, monkeypatch):
        """Sem Windows não há known folder para perguntar; o palpite não pode
        explodir, só ser um palpite."""
        monkeypatch.setattr(atalho, "e_windows", lambda: False)
        monkeypatch.setenv("USERPROFILE", os.path.join("C:", "Users", "Fulano"))
        assert atalho.pasta_area_trabalho().name == "Desktop"


class TestIdentidadeCasada:
    def test_instalar_exige_app_id_explicito(self):
        """Sem default de propósito: um segundo lugar guardando a constante volta
        a quebrar o pin no dia em que uma das cópias mudar — em silêncio."""
        assinatura = inspect.signature(atalho.instalar)
        assert assinatura.parameters["app_id"].default is inspect.Parameter.empty

    def test_o_app_passa_o_mesmo_app_id_do_processo(self):
        """O AUMID do PROCESSO (`identidade_windows`) e o do ATALHO têm de ser o
        MESMO texto — é a igualdade que faz o Windows casar o pin. Divergir não dá
        erro nenhum: só faz o pin voltar a apontar para o interpretador."""
        import app

        fonte = inspect.getsource(app._gerir_atalho)
        assert "atalho.instalar(BASE_DIR, APP_ID)" in fonte
        assert app.APP_ID == "MenteDigital.Assistente.Local.1"


@so_windows
class TestNoWindows:
    def test_aumid_volta_na_releitura(self, tmp_path):
        """O TESTE QUE IMPORTA: sem `System.AppUserModel.ID` no `.lnk`, o Windows
        sintetiza o pin a partir do `python.exe` e o ícone volta a ser a cobrinha.
        Escrever e reler prova que a propriedade pegou."""
        destino = tmp_path / "Mente Digital.lnk"
        atalho.criar_lnk(
            destino=destino, alvo=sys.executable, args='"D:/x/app.py"',
            dir_trabalho=str(tmp_path), icone="", app_id="MenteDigital.Teste.1",
            descricao="teste",
        )
        assert destino.exists()
        assert atalho.ler_aumid(destino) == "MenteDigital.Teste.1"

    def test_save_aceita_caminho_relativo(self, tmp_path, monkeypatch):
        """`IPersistFile::Save` devolve E_INVALIDARG num caminho relativo (custou
        depuração em 2026-08-05). `criar_lnk` resolve para absoluto ANTES de
        salvar, então passar um relativo tem de funcionar mesmo assim."""
        monkeypatch.chdir(tmp_path)
        criado = atalho.criar_lnk(
            destino=Path("relativo.lnk"), alvo=sys.executable, args="",
            dir_trabalho=str(tmp_path), icone="", app_id="MenteDigital.Teste.2",
            descricao="teste",
        )
        assert criado.is_absolute()
        assert (tmp_path / "relativo.lnk").exists()

    def test_lnk_sem_a_propriedade_le_none(self, tmp_path):
        """Um atalho comum (sem AUMID) devolve None, não explode — é o que
        distingue 'atalho velho' de 'atalho instalado'."""
        destino = tmp_path / "comum.lnk"
        atalho.criar_lnk(
            destino=destino, alvo=sys.executable, args="",
            dir_trabalho=str(tmp_path), icone="", app_id="", descricao="",
        )
        assert atalho.ler_aumid(destino) in (None, "")

    def test_pastas_de_destino_existem(self):
        assert atalho.pasta_menu_iniciar().name == "Programs"
        assert atalho.pasta_area_trabalho().exists()
