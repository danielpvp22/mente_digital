"""
O AJUDANTE — o único processo elevado do projeto, e ele só sabe ler um watt.

    python scripts/ajudante_watts.py --uma-vez      # diagnóstico: mede, imprime, sai
    python scripts/ajudante_watts.py                # plantão: publica a cada 2 s

Por que ele existe separado: ver o cabeçalho de `mente_digital/potencia_cpu.py`.
Em uma linha — a potência do pacote da CPU no Windows exige RING 0, o `main.py`
escuta em `0.0.0.0` e escreve no vault, e elevar o `main.py` para ler um watt
trocaria um número por uma superfície de ataque. Então quem eleva é ISTO: sem
porta, sem rota, sem parser, sem receber byte de ninguém. Ele MEDE e ESCREVE um
arquivo; o servidor lê. Se ele não estiver de pé, o campo da CPU vem `None` e o
assistente segue exatamente como sempre — nunca zero, porque zero seria
indistinguível de "medi e a CPU não consome nada".

⚠ ELEVAR ISTO CARREGA UM DRIVER EM MODO KERNEL. A LibreHardwareMonitorLib sobe
o `WinRing0x64.sys` para poder executar RDMSR, e esse driver dá acesso irrestrito
a MSR e portas de I/O a quem falar com ele — é uma escolha de configuração de
SEGURANÇA da máquina, e por isso é ato do DONO, com o passo a passo em
INSTALACAO_WATTS.md. Este script não instala nada, não baixa nada e não se eleva
sozinho: sem privilégio ele mede, não consegue, DIZ que não conseguiu e sai.

⚠ E ele NÃO LÊ O `.env`. Quem pode editar um arquivo de texto comum não deve
poder dizer a um processo administrador onde escrever — o caminho de saída vem
da linha de comando de quem elevou, ou do default derivado de `__file__`.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Roda solto (`python scripts/...`), então a raiz precisa entrar no path na mão.
# Só `potencia_cpu` é importado do projeto, e ele é stdlib pura de propósito:
# um import distraído de `config` traria o pydantic — e o `.env` — para dentro
# do processo elevado. Há teste que falha se isso acontecer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mente_digital.potencia_cpu import (  # noqa: E402
    INTERVALO_PADRAO_S,
    caminho_padrao,
    escolher_sensor,
    publicar,
)

# Onde a LibreHardwareMonitorLib.dll costuma estar depois de o dono descompactar
# o release oficial. Ordem: ao lado do projeto primeiro (é o que INSTALACAO_WATTS
# manda fazer), depois os Program Files. Nada é baixado por este script.
_LOCAIS_LHM = (
    Path(__file__).resolve().parent.parent / "dados" / "lhm" / "LibreHardwareMonitorLib.dll",
    Path(r"C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitorLib.dll"),
    Path(r"C:\Program Files (x86)\LibreHardwareMonitor\LibreHardwareMonitorLib.dll"),
)


class SensorIndisponivel(RuntimeError):
    """Não deu para medir. É um ESTADO NORMAL — sem lib, sem driver, sem
    privilégio — e não um defeito: o servidor foi feito para viver sem este
    número. Existe como exceção só para o `main` decidir entre 'tento de novo
    daqui a pouco' e 'saio explicando'."""


def achar_dll(indicado: Optional[str] = None) -> Optional[Path]:
    """Caminho da LibreHardwareMonitorLib.dll, ou None. Puro (só toca `exists`)."""
    if indicado:
        alvo = Path(indicado)
        return alvo if alvo.is_file() else None
    for candidato in _LOCAIS_LHM:
        if candidato.is_file():
            return candidato
    return None


def _abrir_computador(dll: Path):
    """Liga a LHM só na CPU. É AQUI que o driver de kernel sobe — por isso a
    função é pequena e isolada: é o único ponto do projeto que faz isso."""
    import clr                                  # pythonnet; ausência = estado normal

    clr.AddReference(str(dll))
    from LibreHardwareMonitor.Hardware import Computer   # type: ignore

    computador = Computer()
    # Só a CPU. Ligar placa/memória/disco faria o driver tocar em muito mais
    # hardware do que o necessário para responder "quantos watts", e a regra de
    # um processo privilegiado é fazer o MENOS possível.
    computador.IsCpuEnabled = True
    computador.Open()
    return computador


def ler_sensores(computador) -> list[tuple[str, Optional[float]]]:
    """(nome, valor) de todo sensor de POTÊNCIA da CPU. `Update()` por hardware é
    obrigatório: sem ele os sensores vêm com o valor do momento do `Open()` e o
    plantão publicaria o mesmo watt para sempre."""
    from LibreHardwareMonitor.Hardware import HardwareType, SensorType   # type: ignore

    achados: list[tuple[str, Optional[float]]] = []
    for hardware in computador.Hardware:
        if hardware.HardwareType != HardwareType.Cpu:
            continue
        hardware.Update()
        for sensor in hardware.Sensors:
            if sensor.SensorType == SensorType.Power:
                valor = sensor.Value
                achados.append((str(sensor.Name), None if valor is None else float(valor)))
    return achados


def medir(computador) -> tuple[str, float]:
    """Um par (nome do sensor, watts). Levanta `SensorIndisponivel` se não houver.

    O NOME sobe junto de propósito: 'CPU Package' e 'CPU Cores' são números
    diferentes sobre a mesma peça (o segundo ignora o IO die), e a faixa do app
    tem de poder dizer qual dos dois está mostrando."""
    escolhido = escolher_sensor(ler_sensores(computador))
    if escolhido is None:
        raise SensorIndisponivel(
            "nenhum sensor de potência da CPU respondeu — quase sempre é falta de "
            "privilégio (o driver não subiu) ou CPU sem o sensor exposto")
    return escolhido


def _elevado() -> Optional[bool]:
    """True/False se der para saber; None fora do Windows. Só para a MENSAGEM —
    o script não muda de comportamento por causa disto, ele tenta e relata."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                            # noqa: BLE001
        return None


def _explicar_ausencia(erro: BaseException) -> str:
    """A mensagem que o dono vai ler quando não funcionar. Vale o cuidado: este
    script roda uma vez na instalação e depois some numa janela minimizada."""
    admin = _elevado()
    dica = ("Rode o terminal como administrador." if admin is False else
            "Confira se o LibreHardwareMonitorLib.dll é o do release oficial e se o "
            "Memory Integrity (Isolamento de núcleo) não está bloqueando o WinRing0.")
    return f"{erro} — {dica}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Publica a potência da CPU para o Mente Digital.")
    parser.add_argument("--saida", default=None,
                        help="arquivo a publicar (default: dados/potencia_cpu.json)")
    parser.add_argument("--lhm", default=None,
                        help="caminho da LibreHardwareMonitorLib.dll")
    parser.add_argument("--intervalo", type=float, default=INTERVALO_PADRAO_S,
                        help=f"segundos entre publicações (default: {INTERVALO_PADRAO_S})")
    parser.add_argument("--uma-vez", action="store_true",
                        help="mede uma vez, imprime e sai — o modo de conferir a instalação")
    args = parser.parse_args(argv)

    saida = Path(args.saida) if args.saida else caminho_padrao()
    dll = achar_dll(args.lhm)
    if dll is None:
        print("[WATTS] LibreHardwareMonitorLib.dll não encontrada. "
              "Veja INSTALACAO_WATTS.md (passo 2). O assistente segue sem o watt da CPU.",
              flush=True)
        return 2
    try:
        computador = _abrir_computador(dll)
    except ImportError:
        # pythonnet ausente é o estado normal de quem ainda não instalou nada.
        print("[WATTS] pythonnet não instalado (`pip install pythonnet`). "
              "O assistente segue sem o watt da CPU.", flush=True)
        return 2
    except Exception as exc:                     # noqa: BLE001 - relata, não estoura
        print(f"[WATTS] não consegui abrir a LibreHardwareMonitor: {_explicar_ausencia(exc)}",
              flush=True)
        return 2

    try:
        nome, watts = medir(computador)
    except Exception as exc:                     # noqa: BLE001
        print(f"[WATTS] {_explicar_ausencia(exc)}", flush=True)
        return 2

    if args.uma_vez:
        publicar(watts, nome, saida)
        print(f"[WATTS] {nome}: {watts:.1f} W  ->  {saida}", flush=True)
        return 0

    print(f"[WATTS] de plantão: {nome}, publicando em {saida} a cada {args.intervalo:.0f} s. "
          f"Ctrl+C encerra.", flush=True)
    falhas = 0
    try:
        while True:
            try:
                nome, watts = medir(computador)
                publicar(watts, nome, saida)
                falhas = 0
            except Exception as exc:             # noqa: BLE001
                # Uma falha isolada (sensor que some num tique) não derruba o
                # plantão; o servidor já trata publicação VENCIDA como ausente,
                # então parar de publicar é, por si, o aviso correto.
                falhas += 1
                if falhas in (1, 30):
                    print(f"[WATTS] falha ao medir ({falhas}x): {exc}", flush=True)
            time.sleep(max(0.2, args.intervalo))
    except KeyboardInterrupt:
        print("[WATTS] encerrado.", flush=True)
    finally:
        try:
            computador.Close()
        except Exception:                        # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
