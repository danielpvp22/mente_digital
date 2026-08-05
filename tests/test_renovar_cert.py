"""A régua da renovação do certificado.

O que estes testes protegem é uma promessa com três meses de prazo: o cert do
Tailscale vale 90 dias, e vencido o sintoma que chega ao dono é "o PC está
desligado" — mentira, com tudo funcionando. A decisão de renovar é pura e mora
aqui; o que toca disco e processo fica no script.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_CAMINHO = Path(__file__).resolve().parent.parent / "scripts" / "renovar_cert.py"
_spec = importlib.util.spec_from_file_location("renovar_cert", _CAMINHO)
renovar_cert = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(renovar_cert)

AGORA = datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc)


def test_conta_os_dias_que_faltam():
    assert renovar_cert.dias_para_vencer(AGORA + timedelta(days=90), AGORA) == 90
    assert renovar_cert.dias_para_vencer(AGORA + timedelta(days=1), AGORA) == 1


def test_arredonda_para_baixo():
    """Meio dia de folga não é um dia de folga."""
    assert renovar_cert.dias_para_vencer(AGORA + timedelta(days=2, hours=23), AGORA) == 2


def test_cert_ja_vencido_da_negativo_e_renova():
    dias = renovar_cert.dias_para_vencer(AGORA - timedelta(days=3), AGORA)
    assert dias < 0
    assert renovar_cert.deve_renovar(dias, 30)


def test_longe_do_vencimento_nao_faz_nada():
    """A tarefa agendada roda com frequência de propósito; quase toda execução
    tem de ser um no-op barato, senão renovar vira ruído diário."""
    assert not renovar_cert.deve_renovar(60, 30)


def test_no_limiar_ja_renova():
    """`<=` e não `<`: o dia do limiar é o último em que ainda há folga para uma
    falha e uma nova tentativa."""
    assert renovar_cert.deve_renovar(30, 30)


def test_validade_ilegivel_RENOVA_em_vez_de_assumir_folga():
    """`None` é ignorância, não folga. Um cert ilegível é justamente o caso em
    que mais vale reemitir — e reemitir à toa custa segundos, enquanto assumir
    folga custa o acesso remoto inteiro por até três meses."""
    assert renovar_cert.deve_renovar(None, 30)


def test_arquivo_inexistente_nao_explode():
    """O script roda sozinho numa tarefa agendada: exceção aqui é falha muda."""
    assert renovar_cert.validade_do_cert("") is None
    assert renovar_cert.validade_do_cert(r"C:\nao\existe\cert.crt") is None


def test_arquivo_que_nao_e_certificado_nao_explode(tmp_path):
    falso = tmp_path / "cert.crt"
    falso.write_text("isto não é um PEM", encoding="utf-8")
    assert renovar_cert.validade_do_cert(str(falso)) is None
