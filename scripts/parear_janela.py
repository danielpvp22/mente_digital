"""Dá à JANELA NATIVA uma credencial de aparelho — o passo que falta para matar
o token legado.

Por que existe: o `app.py` abria a janela em `?token={access_token}`, o segredo
LEGADO. Virar `MENTE_APARELHOS_TOKEN_LEGADO=false` trancava o dono fora do próprio
aplicativo, e reparear não adiantava — o front sobrescreve o `localStorage` com o
`?token=` da URL a cada abertura, então o token morto apagava a credencial boa no
lance seguinte. Com `MENTE_JANELA_CREDENCIAL` preenchida, a janela passa a levar
uma credencial revogável, como qualquer outro aparelho.

⚠ Vai pelo `POST /api/aparelhos/parear`, e NÃO pelo `RegistroAparelhos` direto.
Em 2026-08-03 uma sessão chamou a camada de baixo passando
`aparelhos_credencial_expira_dias` — campo que não existe — e o `.get(nome, default)`
devolveu 365 dias em silêncio, contra os 90 da política. Pela rota, quem aplica a
política é o servidor, e não há nome de campo para errar.

⚠ A credencial NUNCA é impressa. Ela é segredo de vida longa: sai daqui direto para
o `.env`, e o que aparece na tela é só o tamanho.

USO (com o servidor no ar):
    python scripts/parear_janela.py
    python scripts/parear_janela.py --base https://meu-pc.tailnet.ts.net:8000
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

RAIZ = Path(__file__).resolve().parent.parent
CHAVE_ENV = "MENTE_JANELA_CREDENCIAL"


def env_com(texto: str, chave: str, valor: str) -> str:
    """O `.env` com `chave=valor`, preservando o resto BYTE A BYTE. Puro/testável.

    Três casos, e os três aconteceram na vida real deste projeto:
    a linha existe (substitui no lugar, mantendo a ordem do arquivo), a linha
    existe COMENTADA (o dono deixou o exemplo lá — substitui também, senão o
    script "não faz nada" e ninguém entende por quê), ou não existe (acrescenta
    no fim). Idempotente: rodar duas vezes dá o mesmo arquivo.
    """
    linha = f"{chave}={valor}"
    padrao = re.compile(rf"^\s*#?\s*{re.escape(chave)}\s*=.*$", re.MULTILINE)
    if padrao.search(texto):
        return padrao.sub(linha, texto, count=1)
    if texto and not texto.endswith("\n"):
        texto += "\n"
    return texto + linha + "\n"


def _emitir_codigo(apelido: str, usuario: str) -> Optional[str]:
    """Emite o código pelo MESMO caminho do painel — o registro é a autoridade."""
    sys.path.insert(0, str(RAIZ))
    from mente_digital.config import settings
    from mente_digital.registro_aparelhos import RegistroAparelhos

    registro = RegistroAparelhos(settings.db_telemetria)
    registro.init()
    return registro.emitir_codigo(apelido, settings.aparelhos_teto, usuario)


def _resgatar(base: str, codigo: str) -> Optional[str]:
    """Troca o código pela credencial, pela rota pública de pareamento."""
    pedido = urllib.request.Request(
        f"{base.rstrip('/')}/api/aparelhos/parear",
        data=json.dumps({"codigo": codigo}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=15,
                                    context=ssl.create_default_context()) as resposta:
            corpo = json.loads(resposta.read().decode())
    except urllib.error.HTTPError as erro:
        print(f"  o servidor RECUSOU ({erro.code}): {erro.read().decode()[:200]}")
        return None
    except urllib.error.URLError as erro:
        # ⚠ CERTIFICADO E REDE SÃO DEFEITOS DIFERENTES, e o `urllib` embrulha os
        # dois no MESMO `URLError`. Dizer "não alcancei o servidor" quando o
        # servidor respondeu e só o NOME não conferiu manda procurar no lugar
        # errado — e aqui esse é o caso provável, porque o certificado desta
        # máquina cobre só o nome MagicDNS. Ver `--base` em `main`.
        if isinstance(erro.reason, ssl.SSLCertVerificationError):
            print(f"  o CERTIFICADO não cobre este endereço: {erro.reason}")
            print("     use --base https://<nome-que-o-cert-cobre>:<porta> — o mesmo "
                  "host que a janela nativa carrega.")
        else:
            print(f"  não alcancei o servidor: {erro.reason}")
        return None
    except Exception as erro:                                    # noqa: BLE001
        print(f"  não alcancei o servidor: {erro}")
        return None
    chave = next((k for k in corpo if "credencial" in k.lower()), None)
    if not chave or not corpo.get(chave):
        print(f"  resposta sem credencial. campos: {sorted(corpo)}")
        return None
    return str(corpo[chave])


def main(argv: Optional[list] = None) -> int:
    sys.path.insert(0, str(RAIZ))
    from mente_digital.config import settings

    esquema = "https" if settings.ssl_cert else "http"
    # ⚠ O HOST SAI DA MESMA REGRA DA JANELA, não de `127.0.0.1` cravado. Este
    # script é a PORTA DE SOCORRO — é por ele que se recupera a credencial quando
    # o acesso quebrou — e com o default antigo ele falhava JUSTAMENTE aí: em
    # `https` o `create_default_context` verifica o NOME, e nenhum certificado
    # público pode cobrir um IP de loopback. É a regressão que
    # `app._host_da_janela` já documenta e conserta desde 2026-08-04; o script
    # tinha ficado para trás, e o `except` largo ainda a disfarçava de queda de
    # rede. Reusar a função é o ponto: duas cópias da regra divergem no primeiro
    # conserto de uma — a mesma razão de `aparelhos.avaliar` chamar
    # `acesso.cliente_autorizado` em vez de reimplementá-la.
    from app import _host_da_janela

    host = _host_da_janela(esquema, settings.ssl_cert)
    parser = argparse.ArgumentParser(description="Pareia a janela nativa do PC.")
    parser.add_argument("--base", default=f"{esquema}://{host}:{settings.port}",
                        help="endereço do servidor (default: o mesmo host que a janela nativa usa)")
    parser.add_argument("--apelido", default="janela do PC")
    parser.add_argument("--usuario", default="daniel")
    args = parser.parse_args(argv)

    if not settings.aparelhos_habilitado:
        print("MENTE_APARELHOS_HABILITADO está false — a credencial não seria usada.")
        return 1

    codigo = _emitir_codigo(args.apelido, args.usuario)
    if not codigo:
        print("não consegui emitir o código (teto de aparelhos atingido?).")
        return 1

    credencial = _resgatar(args.base, codigo)
    if not credencial:
        print("pareamento não concluído — nada foi escrito no .env.")
        return 1

    env = RAIZ / ".env"
    texto = env.read_text(encoding="utf-8") if env.exists() else ""
    env.write_text(env_com(texto, CHAVE_ENV, credencial), encoding="utf-8")
    print(f"janela pareada. credencial de {len(credencial)} chars gravada em "
          f"{CHAVE_ENV} (não exibida).")
    print("reinicie o app para a janela passar a usá-la; depois disso o "
          "MENTE_APARELHOS_TOKEN_LEGADO pode ir para false.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
