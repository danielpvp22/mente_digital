"""
Início automático com o Windows.

Só a montagem é testada, e de propósito: `instalar`/`remover` escrevem na pasta
Inicializar do usuário que roda a suíte, e um teste que mexe no logon de alguém é
um teste que ninguém quer rodar duas vezes. O que pode dar errado de verdade é o
conteúdo do `.vbs` — caminho com espaço sem aspas falha em SILÊNCIO: o Windows
não mostra erro de script de inicialização em lugar nenhum que o dono veja.
"""
from __future__ import annotations

from pathlib import Path

from mente_digital import inicializacao


def test_caminho_vai_para_a_pasta_inicializar(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\Fulano\AppData\Roaming")
    alvo = inicializacao.caminho_atalho()
    assert alvo.parent.name == "Startup"
    assert alvo.name == "Mente Digital.vbs"


def test_vbs_escapa_aspas_de_caminho_com_espaco():
    """VBScript escapa aspas DOBRANDO-AS. "Program Files" e "Mente Digital" são a
    regra, não a exceção."""
    vbs = inicializacao.script_vbs(
        r"C:\Program Files\Python\pythonw.exe",
        r"D:\Meus Projetos\mente digital\app.py",
        r"D:\Meus Projetos\mente digital",
    )
    assert r'""C:\Program Files\Python\pythonw.exe""' in vbs
    assert r'""D:\Meus Projetos\mente digital\app.py""' in vbs
    assert 'sh.CurrentDirectory = "D:\\Meus Projetos\\mente digital"' in vbs


def test_vbs_roda_oculto_e_sem_esperar():
    """O 0 é o modo de janela (oculto) e o False é 'não espere terminar'. Trocar o
    0 por 1 devolveria o console preto que este arquivo existe para evitar; trocar
    o False por True deixaria o script de logon preso ao app o dia inteiro."""
    vbs = inicializacao.script_vbs("py.exe", "app.py", "C:\\proj")
    assert vbs.rstrip().endswith(", 0, False")


def test_vbs_leva_o_VIGIA_por_padrao():
    """⚠ Mudou em 2026-08-02: antes era `--standby`, que sobe o servidor inteiro e
    só solta a VRAM — ficavam ~7,7 GB de RAM comprometidos a noite toda. O dono viu
    a medida e escolheu as duas camadas: o logon deixa de plantão o VIGIA, que não
    carrega modelo nenhum."""
    vbs = inicializacao.script_vbs("py.exe", "app.py", "C:\\proj")
    assert "--vigia" in vbs
    assert "--standby" not in vbs


def test_vbs_explica_como_desfazer():
    """O arquivo é auditável a olho: quem abrir no bloco de notas tem de entender
    o que é e como sair disso."""
    vbs = inicializacao.script_vbs("py.exe", "app.py", "C:\\proj")
    assert "--remover-inicio" in vbs


def test_interpretador_prefere_o_pythonw(tmp_path):
    """Sem o `w`, o app viveria atrás de um console preto que o Alt+Tab acha."""
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    assert inicializacao.interpretador(str(tmp_path / "python.exe")).endswith("pythonw.exe")


def test_interpretador_cai_no_python_normal_se_nao_houver_w(tmp_path):
    """Feio porém funcional vence não instalar."""
    exe = tmp_path / "python.exe"
    exe.write_text("")
    assert inicializacao.interpretador(str(exe)) == str(exe)


def test_instalar_e_remover_num_appdata_de_mentira(monkeypatch, tmp_path):
    """O ciclo completo, com a pasta Inicializar redirecionada para o tmp — a
    única forma honesta de exercitar a escrita sem tocar no logon de ninguém."""
    monkeypatch.setattr(inicializacao.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert inicializacao.instalado() is False
    destino = inicializacao.instalar(Path(r"D:\projetos\mente_digital"))
    assert destino.exists() and inicializacao.instalado() is True
    assert "app.py" in destino.read_text(encoding="utf-16")

    assert inicializacao.remover() is True
    assert inicializacao.instalado() is False
    assert inicializacao.remover() is False        # idempotente


def test_o_arquivo_sai_em_utf16_com_bom(monkeypatch, tmp_path):
    """⚠ O Windows Script Host lê `.vbs` como ANSI a menos que haja BOM de
    UTF-16. Gravado em UTF-8 (o primeiro palpite, e o que estava aqui em
    2026-08-02), os acentos chegavam como lixo — "MODO ECONOMIA â€” sobe o
    assistente". Só sujava comentário, mas o dia em que uma string acentuada
    entrar no script o logon quebra EM SILÊNCIO: falha de script de
    inicialização não aparece em lugar nenhum que o dono veja."""
    monkeypatch.setattr(inicializacao.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    destino = inicializacao.instalar(Path(r"D:\projetos\mente_digital"))
    bruto = destino.read_bytes()
    assert bruto[:2] == b"\xff\xfe"                       # BOM de UTF-16 LE
    assert "VIGIA" in bruto.decode("utf-16")
