"""Renova o certificado do Tailscale ANTES de ele vencer — e reinicia o vigia.

Por que existe: o cert do `tailscale cert` vale 90 dias. Vencido, o app Android
não fala mais com o PC e o sintoma que chega ao dono é "o PC está desligado" —
mentira, com tudo funcionando. Uma data no calendário a três meses de distância é
exatamente o tipo de compromisso que se perde.

⚠ O VIGIA É REINICIADO JUNTO, e isso não é zelo: ele serve TLS com o mesmo par de
arquivos e os lê no START. Renovar sem reiniciá-lo deixa o plantão servindo um
cert vencido enquanto o assistente já serve o novo — e como o app Android DERIVA o
endereço do vigia trocando só a porta (mantendo o esquema), o celular volta a
dizer que o PC está desligado. Foi o que aconteceu em 2026-08-04, quando o vigia
havia subido ANTES de o cert existir.

O gatilho é a VALIDADE, não o calendário: a tarefa agendada roda com frequência e
este script só age quando falta pouco. Assim uma execução perdida (PC desligado no
dia) não vira cert vencido — a próxima passada resolve.

USO:
    python scripts/renovar_cert.py            # renova só se estiver perto de vencer
    python scripts/renovar_cert.py --forcar   # renova agora
    python scripts/renovar_cert.py --limiar 45
"""
from __future__ import annotations

import argparse
import ssl
import subprocess  # nosec B404
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RAIZ = Path(__file__).resolve().parent.parent
LIMIAR_PADRAO_DIAS = 30
LOG = RAIZ / "dados" / "logs" / "renovacao_cert.log"


def dizer(msg: str) -> None:
    """Escreve na tela E no arquivo.

    A tarefa agendada roda sem console: sem o arquivo, uma renovação que falhou
    às 3h30 fica indistinguível de uma que nunca rodou — e a diferença só
    apareceria em novembro, na forma de "o PC está desligado" no celular.
    """
    print(msg)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(msg.rstrip() + "\n")
    except Exception as erro:                                # noqa: BLE001
        # Não derruba a renovação por causa do log, mas também não se cala.
        print(f"  (não consegui escrever em {LOG}: {erro})")


def dias_para_vencer(fim: datetime, agora: datetime) -> int:
    """Quantos dias faltam. PURO — o instante é INJETADO, como em `agenda.py`.

    Arredonda para BAIXO de propósito: meio dia de folga não é um dia de folga, e
    aqui o erro barato é renovar cedo demais.
    """
    return (fim - agora).days


def deve_renovar(dias: Optional[int], limiar: int) -> bool:
    """`None` = não consegui ler a validade, e aí RENOVA.

    A ignorância não pode ser confundida com folga: um cert ilegível é o caso em
    que mais vale reemitir, e reemitir à toa custa segundos.
    """
    return dias is None or dias <= limiar


def validade_do_cert(caminho: str) -> Optional[datetime]:
    """Quando o certificado vence, lido do arquivo.

    ⚠ `ssl._ssl._test_decode_cert` é API PRIVADA — mesma escolha (e mesmo risco)
    já assumidos no `app.py._nome_no_certificado`: o `cryptography` NÃO está nesta
    env, e trazer uma dependência para ler uma data seria caro. Se ela sumir numa
    versão nova do Python, `None` faz o script renovar, que é o lado seguro.
    """
    if not caminho or not Path(caminho).exists():
        return None
    try:
        dados = ssl._ssl._test_decode_cert(caminho)          # type: ignore[attr-defined]
        bruto = dados.get("notAfter")
        if not bruto:
            return None
        # Formato do OpenSSL: 'Nov  2 21:03:00 2026 GMT'.
        return datetime.strptime(bruto, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except Exception:                                        # noqa: BLE001
        return None


def _reiniciar_vigia() -> bool:
    """Derruba o plantão e o sobe de novo, para ele reler o par de arquivos.

    Mata pela PORTA e não pelo nome do processo: `pythonw.exe` é o mesmo
    executável do assistente, e um `taskkill` por nome levaria os dois.
    """
    sys.path.insert(0, str(RAIZ))
    from mente_digital.config import settings

    # ⚠ NÃO use `vigia.subir_app`: apesar do nome, ele levanta o ASSISTENTE
    # (`app.py --oculto`), que é o que o plantão faz quando o celular bate. Aqui o
    # que precisa voltar é o próprio plantão.
    achados = subprocess.run(                                        # nosec B603 B607
        ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, check=False)
    alvo = f":{settings.vigia_port}"
    pids = {
        linha.split()[-1]
        for linha in achados.stdout.splitlines()
        if alvo in linha and "LISTENING" in linha.upper()
    }
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"],               # nosec B603 B607
                       capture_output=True, check=False)
    return _subir_vigia()


def _subir_vigia() -> bool:
    """Sobe o plantão desgrudado, como a Inicializar do Windows faz."""
    exe = Path(sys.executable)
    candidato = exe.with_name("pythonw.exe")
    py = str(candidato if candidato.exists() else exe)
    try:
        subprocess.Popen(                                             # nosec B603
            [py, str(RAIZ / "app.py"), "--vigia"], cwd=str(RAIZ),
            creationflags=0x00000008 | 0x00000200, close_fds=True)
        return True
    except Exception as erro:                                         # noqa: BLE001
        dizer(f"  não consegui subir o vigia: {erro}")
        return False


def main(argv: Optional[list] = None) -> int:
    sys.path.insert(0, str(RAIZ))
    from mente_digital.config import settings

    parser = argparse.ArgumentParser(description="Renova o cert do Tailscale se preciso.")
    parser.add_argument("--limiar", type=int, default=LIMIAR_PADRAO_DIAS,
                        help=f"renova quando faltarem N dias (default: {LIMIAR_PADRAO_DIAS})")
    parser.add_argument("--forcar", action="store_true")
    args = parser.parse_args(argv)

    agora = datetime.now(timezone.utc)
    fim = validade_do_cert(settings.ssl_cert)
    dias = dias_para_vencer(fim, agora) if fim else None
    carimbo = agora.strftime("%Y-%m-%d %H:%M")

    if not args.forcar and not deve_renovar(dias, args.limiar):
        dizer(f"[{carimbo}] cert válido por mais {dias} dias — nada a fazer.")
        return 0

    quanto = "ilegível" if dias is None else f"{dias} dias"
    dizer(f"[{carimbo}] renovando (validade restante: {quanto}).")
    passo = subprocess.run(                                           # nosec B603
        [sys.executable, str(RAIZ / "scripts" / "configurar_tailscale.py"), "--aplicar"],
        cwd=str(RAIZ), capture_output=True, text=True, check=False)
    dizer(passo.stdout[-1500:])
    if passo.returncode != 0:
        dizer(f"  configurar_tailscale falhou ({passo.returncode}); vigia intocado.")
        dizer(passo.stderr[-600:])
        return 1

    dizer("  reiniciando o vigia para ele reler o par cert/chave…")
    dizer("  vigia reiniciado." if _reiniciar_vigia() else "  ⚠ vigia NÃO subiu.")
    novo = validade_do_cert(settings.ssl_cert)
    if novo:
        dizer(f"  cert novo vence em {dias_para_vencer(novo, agora)} dias.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
