"""
Gera um certificado TLS para servir a Mente Digital em HTTPS/WSS na LAN (painel
2026-07, #8 Redes) — o que destrava a VOZ no celular (getUserMedia exige "secure
context" fora de localhost).

DUAS vias, nesta ordem de preferência:

1) mkcert (RECOMENDADO): instala uma Autoridade Certificadora LOCAL. Depois de
   instalar essa CA UMA vez no celular, o HTTPS fica SEM aviso de segurança e o
   WSS funciona de forma confiável (cert auto-assinado "cru" falha de forma
   errática em WSS no iOS/Android mesmo depois de aceitar o aviso da página).

2) openssl (FALLBACK): cert auto-assinado. Funciona, mas o browser mostra aviso e
   você precisa aceitar a exceção por aparelho — e no iOS o WSS pode falhar assim
   mesmo. Use só se não puder instalar o mkcert.

Este script NÃO adiciona dependência Python: ele apenas ORIENTA e, se a ferramenta
existir no PATH, chama-a. Descobre os IPs da LAN para incluir no cert.

USO:
    python scripts/gerar_cert.py            # detecta mkcert/openssl e gera em ./certs
    python scripts/gerar_cert.py --host 192.168.0.10   # força um IP/hostname extra
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mente_digital.config import BASE_DIR  # noqa: E402

CERTS = os.path.join(str(BASE_DIR), "certs")
CERT = os.path.join(CERTS, "mente.crt")
KEY = os.path.join(CERTS, "mente.key")


def _ips_locais() -> list[str]:
    nomes = {"localhost", "127.0.0.1"}
    try:
        nomes.add(socket.gethostname())
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" not in ip:   # ignora IPv6 aqui p/ simplicidade
                nomes.add(ip)
    except OSError:
        pass
    return sorted(nomes)


def _via_mkcert(hosts: list[str]) -> bool:
    if not shutil.which("mkcert"):
        return False
    os.makedirs(CERTS, exist_ok=True)
    print("mkcert encontrado. Instalando a CA local (idempotente)...")
    subprocess.run(["mkcert", "-install"], check=False)
    cmd = ["mkcert", "-cert-file", CERT, "-key-file", KEY, *hosts]
    print("Gerando:", " ".join(cmd))
    ok = subprocess.run(cmd, check=False).returncode == 0
    if ok:
        print("\n[mkcert] No CELULAR: instale a CA local uma vez para HTTPS sem aviso.")
        print("  A raiz da CA está em:", subprocess.run(
            ["mkcert", "-CAROOT"], capture_output=True, text=True).stdout.strip())
    return ok


def _via_openssl(hosts: list[str]) -> bool:
    if not shutil.which("openssl"):
        return False
    os.makedirs(CERTS, exist_ok=True)
    san = ",".join(
        (f"IP:{h}" if h[0].isdigit() else f"DNS:{h}") for h in hosts
    )
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", KEY, "-out", CERT, "-days", "825",
        "-subj", "/CN=mente-digital",
        "-addext", f"subjectAltName={san}",
    ]
    print("openssl (fallback, cert auto-assinado):", " ".join(cmd))
    print("AVISO: o browser vai pedir para aceitar a exceção por aparelho; no iOS o "
          "WSS pode falhar mesmo assim. Prefira o mkcert se puder.")
    return subprocess.run(cmd, check=False).returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", action="append", default=[], help="IP/hostname extra p/ o cert")
    args = ap.parse_args()

    hosts = sorted(set(_ips_locais()) | set(args.host))
    print("Hosts no certificado:", ", ".join(hosts))

    if _via_mkcert(hosts) or _via_openssl(hosts):
        print(f"\nOK. Aponte o .env para:\n  MENTE_SSL_CERT={CERT}\n  MENTE_SSL_KEY={KEY}")
        print("Reinicie o servidor e acesse por https://<ip-da-maquina>:8000")
    else:
        print("\nNem mkcert nem openssl no PATH. Instale um deles:")
        print("  mkcert (recomendado): https://github.com/FiloSottile/mkcert")
        print("  ou openssl (Git for Windows já inclui).")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
