"""
Controle de acesso (painel 2026-07, #7 Redes).

O servidor sobe em 0.0.0.0 e as rotas/WS expõem o vault, o histórico e um endpoint
de ESCRITA (nota → ETL → Zettelkasten permanente) — sem gate, qualquer aparelho da
LAN lê e ENVENENA a base. A regra é uma só, pura e testável:

- COM `MENTE_ACCESS_TOKEN` configurado: exige o token exato (compare_digest — sem
  vazamento de timing), venha de onde vier.
- SEM token: só loopback (127.0.0.1/::1) — a LAN não alcança nada por default.

O Origin do browser, quando presente, tem que apontar para o MESMO host:porta que
serviu a página — barra cross-site WebSocket hijacking a partir de uma aba de outro
site aberta na mesma máquina. Cliente não-browser não manda Origin: o gate dele é o
token/loopback acima.
"""
from __future__ import annotations

import secrets
from typing import Optional
from urllib.parse import urlparse

# "testclient" é o host fixo do TestClient do Starlette — só existe em teste; um
# cliente remoto não escolhe o próprio client.host (vem do socket aceito).
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def cliente_autorizado(
    host: Optional[str], token_recebido: Optional[str], token_esperado: str
) -> bool:
    """True se este cliente pode usar as rotas /api e o WebSocket."""
    if token_esperado:
        return bool(token_recebido) and secrets.compare_digest(token_recebido, token_esperado)
    return (host or "") in _LOOPBACK


def origin_confere(origin: Optional[str], host_header: str) -> bool:
    """True se o Origin (quando existe) aponta para o host que serviu a página."""
    if not origin:
        return True
    try:
        return (urlparse(origin).netloc or "") == (host_header or "")
    except Exception:
        return False
