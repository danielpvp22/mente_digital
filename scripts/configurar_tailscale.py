"""
Liga o TLS do assistente com o certificado do Tailscale — o passo depois do login.

Instalar o Tailscale e autenticar a conta são atos do DONO (o login é SSO no
navegador). Tudo o que vem DEPOIS é mecânico, e mecânico é onde erro humano mora —
por isso está aqui em vez de num passo a passo que alguém segue às 2 da manhã.

    python scripts/configurar_tailscale.py            # mostra o que faria
    python scripts/configurar_tailscale.py --aplicar  # emite o cert e escreve o .env

⚠ A ARMADILHA QUE ESTE SCRIPT TORNA IMPOSSÍVEL. O roteiro manual pede
`tailscale cert <maquina>.<tailnet>.ts.net`, e o erro mais provável de todo o
processo é usar o IP `100.x` no lugar do nome: o certificado é emitido PARA O NOME,
então com IP a verificação falha e o app reporta como falha de conexão genérica — a
tela diz "o PC está desligado" e o dono procura o problema no lugar errado. Aqui o
nome sai do `tailscale status --json` (campo `Self.DNSName`), então não há o que
digitar errado.

⚠ E O NOME VEM COM PONTO NO FIM. `DNSName` é um FQDN absoluto ("maquina.tal.ts.net."
com o ponto raiz). Passar isso ao `tailscale cert` gera um arquivo com ponto no nome
e um SAN que não casa o host que o navegador pede. O ponto é removido.

PURO onde dá: `nome_magicdns` e `escrever_env` não tocam disco nem rede, e são o que
os testes exercitam. Só `main` chama o `tailscale` e escreve o arquivo.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Optional, Tuple

RAIZ = Path(__file__).resolve().parents[1]
ENV = RAIZ / ".env"
DESTINO_CERTS = RAIZ / "dados" / "certs"


def nome_magicdns(status_json: str) -> Optional[str]:
    """O nome MagicDNS desta máquina, a partir do `tailscale status --json`.

    PURO. Devolve None quando o JSON não traz o campo — e o chamador PARA, em vez de
    cair para o IP. Cair para o IP é exatamente o defeito que este script existe para
    tornar impossível."""
    try:
        dados = json.loads(status_json)
    except (ValueError, TypeError):
        return None
    nome = ((dados or {}).get("Self") or {}).get("DNSName")
    if not isinstance(nome, str) or not nome.strip():
        return None
    # FQDN absoluto: o ponto raiz do fim quebraria o nome do arquivo e o SAN.
    nome = nome.strip().rstrip(".")
    # Sanidade: tem de PARECER nome, não IP. Um `100.101.102.103` aqui significaria
    # que o MagicDNS não está ligado, e seguir com ele produziria um cert inútil.
    if not nome or re.fullmatch(r"[\d.]+", nome):
        return None
    return nome


def escrever_env(texto: str, cert: str, chave: str) -> str:
    """O `.env` com `MENTE_SSL_CERT`/`MENTE_SSL_KEY` apontando para os arquivos.

    PURO: recebe e devolve texto. Substitui a linha se ela existir (inclusive
    comentada) e acrescenta no fim se não existir — idempotente, então rodar duas
    vezes não empilha duplicatas que o pydantic resolveria pela última, calado.

    ⚠ Preserva o resto do arquivo BYTE A BYTE. Este `.env` tem 200 linhas de
    comentário que documentam decisões medidas; reescrevê-lo "limpo" apagaria a
    memória do projeto."""
    valores = {"MENTE_SSL_CERT": cert, "MENTE_SSL_KEY": chave}
    linhas = texto.splitlines()
    vistos = set()
    for i, linha in enumerate(linhas):
        for chave_env, valor in valores.items():
            if re.match(rf"^\s*#?\s*{chave_env}\s*=", linha):
                linhas[i] = f"{chave_env}={valor}"
                vistos.add(chave_env)
    faltando = [c for c in valores if c not in vistos]
    if faltando:
        linhas.append("")
        linhas.append("# --- TLS do Tailscale (gerado por scripts/configurar_tailscale.py) ---")
        linhas.append("# ⚠ O certificado expira em 90 DIAS. Renovar é rodar este script de novo.")
        for c in faltando:
            linhas.append(f"{c}={valores[c]}")
    return "\n".join(linhas) + "\n"


def _tailscale() -> Optional[str]:
    """O executável, do PATH ou do caminho padrão de instalação no Windows (o
    instalador não põe no PATH da sessão já aberta — e é sempre a sessão já aberta
    que o dono usa logo depois de instalar)."""
    achado = shutil.which("tailscale")
    if achado:
        return achado
    padrao = Path(r"C:\Program Files\Tailscale\tailscale.exe")
    return str(padrao) if padrao.exists() else None


def _rodar(exe: str, *args: str) -> Tuple[int, str, str]:
    r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=180)  # nosec B603
    return r.returncode, r.stdout, r.stderr


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--aplicar", action="store_true",
                   help="emite o certificado e escreve o .env (sem isto, só mostra)")
    args = p.parse_args(argv)

    exe = _tailscale()
    if not exe:
        print("Tailscale não encontrado. Instale-o e faça login primeiro — esse passo\n"
              "é seu (o login é SSO no navegador). Depois rode este script.",
              file=sys.stderr)
        return 2
    print(f"tailscale: {exe}")

    codigo, saida, erro = _rodar(exe, "status", "--json")
    if codigo != 0:
        print(f"`tailscale status` falhou — você já fez login?\n{erro.strip()}",
              file=sys.stderr)
        return 2

    nome = nome_magicdns(saida)
    if not nome:
        print("Não consegui o nome MagicDNS desta máquina.\n"
              "Quase sempre significa que o MagicDNS está DESLIGADO no admin console.\n"
              "Ligue MagicDNS e HTTPS Certificates lá e rode de novo.\n"
              "⚠ Não vou cair para o IP 100.x: o certificado é emitido para o NOME, e\n"
              "   com IP o app falha dizendo 'o PC está desligado'.", file=sys.stderr)
        return 2
    print(f"nome MagicDNS: {nome}")

    cert = DESTINO_CERTS / f"{nome}.crt"
    chave = DESTINO_CERTS / f"{nome}.key"
    print(f"cert  -> {cert}")
    print(f"chave -> {chave}")

    if not args.aplicar:
        print("\n(dry-run: nada foi emitido nem escrito — use --aplicar)")
        return 0

    DESTINO_CERTS.mkdir(parents=True, exist_ok=True)
    codigo, _saida, erro = _rodar(
        exe, "cert", "--cert-file", str(cert), "--key-file", str(chave), nome)
    if codigo != 0:
        print(f"`tailscale cert` falhou:\n{erro.strip()}\n\n"
              "Se a mensagem fala de HTTPS/certificados, ligue 'HTTPS Certificates'\n"
              "no admin console. O .env NÃO foi tocado.", file=sys.stderr)
        return 1
    # Só escreve o .env DEPOIS de os dois arquivos existirem: um .env apontando para
    # cert inexistente derruba o boot, e derruba justamente quando o dono não está
    # na frente do PC para desfazer.
    if not (cert.exists() and chave.exists()):
        print("o `tailscale cert` disse OK mas os arquivos não estão lá — .env intocado.",
              file=sys.stderr)
        return 1
    print(f"certificado emitido ({cert.stat().st_size} B / {chave.stat().st_size} B)")

    original = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    novo = escrever_env(original, str(cert), str(chave))
    ENV.write_text(novo, encoding="utf-8")
    print(f"\n.env atualizado. Reinicie o assistente e abra no celular:\n"
          f"    https://{nome}:8000\n"
          f"⚠ Use esse NOME, nunca o IP 100.x. E o cert expira em 90 dias — "
          f"renovar é rodar isto de novo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
