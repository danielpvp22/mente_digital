"""Fallback de SSL no deep-fetch (resiliência da busca web).

Quando um site tem certificado quebrado (vencido, cadeia inválida, ou hostname
mismatch — visto ao vivo com www.orapha.dev), o httpx recusa com
CERTIFICATE_VERIFY_FAILED e a página é perdida. O detector puro abaixo decide se a
falha é ESPECIFICAMENTE de verificação de certificado — só nesse caso o
`_baixar_pagina` re-tenta sem verificar o cert. Um 500/403/timeout NÃO pode
disparar o downgrade (não é problema de cert, e desativar SSL não ajudaria).
"""
from __future__ import annotations

import ssl

from mente_digital.rag import _erro_de_certificado


def test_ssl_cert_error_direto_e_detectado():
    exc = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )
    assert _erro_de_certificado(exc) is True


def test_ssl_cert_error_encadeado_via_cause():
    # httpx embrulha o erro de SSL num ConnectError; o detector desce a cadeia.
    inner = ssl.SSLCertVerificationError("certificate verify failed: hostname mismatch")
    outer = ConnectionError("connect failed")
    outer.__cause__ = inner
    assert _erro_de_certificado(outer) is True


def test_erro_por_string_quando_tipo_nao_e_ssl():
    # Alguns wrappers só trazem a mensagem — a string ainda delata o erro de cert.
    exc = RuntimeError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "Hostname mismatch, certificate is not valid for 'www.orapha.dev'."
    )
    assert _erro_de_certificado(exc) is True


def test_erro_500_nao_e_de_certificado():
    exc = Exception("Server error '500 Internal Server Error' for url ...")
    assert _erro_de_certificado(exc) is False


def test_timeout_nao_e_de_certificado():
    assert _erro_de_certificado(TimeoutError("timed out")) is False
