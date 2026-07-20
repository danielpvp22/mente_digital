"""
Controle de acesso (#7, painel 2026-07): sem token configurado, só loopback; com
token, exige o segredo exato; Origin (quando presente) deve bater com o Host.
Regra pura em acesso.py — testável sem servidor.
"""
from __future__ import annotations

from acesso import cliente_autorizado, origin_confere


# -- token/loopback ------------------------------------------------------------
def test_sem_token_configurado_loopback_entra():
    assert cliente_autorizado("127.0.0.1", None, "") is True
    assert cliente_autorizado("::1", None, "") is True


def test_sem_token_configurado_lan_fica_fora():
    # O caso do painel: aparelho da LAN lendo o vault / injetando nota.
    assert cliente_autorizado("192.168.0.42", None, "") is False
    assert cliente_autorizado("10.0.0.7", "qualquer", "") is False


def test_com_token_o_segredo_libera_qualquer_host():
    assert cliente_autorizado("192.168.0.42", "s3gr3do", "s3gr3do") is True


def test_com_token_errado_ou_ausente_nega_ate_loopback():
    # Com token configurado a regra é o token — inclusive para localhost.
    assert cliente_autorizado("127.0.0.1", "errado", "s3gr3do") is False
    assert cliente_autorizado("127.0.0.1", None, "s3gr3do") is False


def test_host_desconhecido_sem_token_nega():
    assert cliente_autorizado(None, None, "") is False


# -- Origin --------------------------------------------------------------------
def test_origin_igual_ao_host_passa():
    assert origin_confere("http://192.168.0.10:8000", "192.168.0.10:8000") is True
    assert origin_confere("https://meu-pc:8443", "meu-pc:8443") is True


def test_origin_de_outro_site_e_bloqueado():
    # Cross-site WebSocket hijacking: aba de site alheio tentando abrir o WS local.
    assert origin_confere("http://site-malicioso.com", "127.0.0.1:8000") is False
    assert origin_confere("null", "127.0.0.1:8000") is False


def test_sem_origin_passa_pois_nao_e_browser():
    # Cliente não-browser (curl, script): sem Origin — o gate dele é token/loopback.
    assert origin_confere(None, "127.0.0.1:8000") is True
    assert origin_confere("", "127.0.0.1:8000") is True
