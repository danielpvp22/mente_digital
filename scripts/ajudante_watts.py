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

⚠ ELEVAR ISTO CARREGA UM DRIVER EM MODO KERNEL. A LibreHardwareMonitorLib precisa
de RDMSR, e desde a v0.9.5 quem executa isso é o **PawnIO** (o `WinRing0x64.sys`
saiu de cena — a lib v0.9.6 não embute driver nenhum). É acesso privilegiado a
MSR, ou seja, uma escolha de configuração de SEGURANÇA da máquina, e por isso é
ato do DONO, com o passo a passo em INSTALACAO_WATTS.md. Este script não instala
nada, não baixa nada e não se eleva sozinho: sem privilégio ele mede, não
consegue, DIZ que não conseguiu e sai.

⚠ ELE SAI DA FRENTE QUANDO UM JOGO ABRE. Anti-cheat de kernel (o BattlEye do
Tarkov) divide o mesmo andar que o PawnIO. Enquanto um jogo de `jogo_ativo.py`
estiver rodando, o plantão fecha a lib, DESABILITA o dispositivo PawnIO e para de
publicar — o servidor já trata publicação vencida como ausente, então a parcela
da CPU vira `None` sozinha, que é o comportamento correto e já testado. Quando o
jogo fecha, ele reabilita e volta a medir. `--ignorar-jogos` desliga a guarda.

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

from mente_digital import jogo_ativo  # noqa: E402
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


def _mexer_no_driver(habilitar: bool) -> tuple[bool, str]:
    """Habilita/desabilita o dispositivo PawnIO. (ok, mensagem).

    Roda `pnputil` em vez de parar o serviço porque parar o serviço NÃO
    descarrega o driver — ver o cabeçalho de `jogo_ativo.py`, onde isso está
    medido. Falhar aqui não é fatal: significa "não consegui soltar o kernel",
    e quem decide o que fazer com essa informação é o dono, que está no teclado.
    """
    import subprocess  # local: mantém o import fora do caminho de quem só mede

    cmd = jogo_ativo.comando_pnputil(habilitar)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:                     # noqa: BLE001
        return False, f"{cmd[0]} falhou: {exc}"
    saida = (r.stdout or "").strip() or (r.stderr or "").strip()
    return r.returncode == 0, saida or f"código {r.returncode}"


def _jogo_agora(alvos) -> tuple[Optional[str], list, Optional[str]]:
    """(jogo, processos, erro). Erro não vira exceção: o plantão não pode morrer
    porque a enumeração de processos falhou num tique."""
    try:
        procs = jogo_ativo.processos_com_pid()
        return jogo_ativo.detectar((n for _, n in procs), alvos), procs, None
    except Exception as exc:                     # noqa: BLE001
        return None, [], str(exc)


def _fixar_no_vcache(procs, mascara: int, ja_feitos: set) -> None:
    """Prende o jogo no CCD com V-Cache. Uma vez por PID.

    Por que aqui e não num processo próprio: este já é o único elevado, já roda
    a cada tique e já sabe reconhecer o jogo. Um segundo vigia seria uma segunda
    cópia da mesma regra, e duas cópias divergem no primeiro conserto de uma.

    Uma vez por PID de propósito — reaplicar a cada tique brigaria com o jogo se
    ele mesmo mexesse na afinidade, e encheria o log sem informar nada.
    """
    for pid, nome in jogo_ativo.alvos_de_afinidade(procs):
        if pid in ja_feitos:
            continue
        atual = jogo_ativo.afinidade_atual(pid)
        if not jogo_ativo.precisa_fixar(atual, mascara):
            ja_feitos.add(pid)
            continue
        ok, msg = jogo_ativo.fixar_afinidade(pid, mascara)
        ja_feitos.add(pid)                       # não insiste: falhou, falhou
        atual_txt = "?" if atual is None else f"0x{atual:X}"
        print(f"[WATTS] afinidade de {nome} ({atual_txt} -> 0x{mascara:X}): "
              f"{'ok' if ok else msg}", flush=True)


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
    parser.add_argument("--ignorar-jogos", action="store_true",
                        help="não solta o kernel quando um jogo abrir (desliga a guarda)")
    parser.add_argument("--jogo", action="append", default=None,
                        help="executável extra que dispara a pausa (pode repetir); "
                             "substitui a lista padrão de jogo_ativo.py")
    parser.add_argument("--sem-afinidade", action="store_true",
                        help="não prende o jogo no CCD com V-Cache")
    parser.add_argument("--mascara", default=None,
                        help="máscara de afinidade do CCD com V-Cache (ex.: 0xFFFF). "
                             "O default foi MEDIDO nesta CPU — refaça o benchmark "
                             "antes de reusar noutra máquina")
    args = parser.parse_args(argv)

    saida = Path(args.saida) if args.saida else caminho_padrao()
    alvos = frozenset(map(jogo_ativo.normalizar, args.jogo)) if args.jogo \
        else jogo_ativo.JOGOS_PADRAO
    guarda = not args.ignorar_jogos
    afinidade_on = not args.sem_afinidade
    try:
        mascara = int(args.mascara, 0) if args.mascara else jogo_ativo.MASCARA_VCACHE
    except ValueError:
        print(f"[WATTS] máscara inválida: {args.mascara!r} (use algo como 0xFFFF)",
              flush=True)
        return 2
    if mascara <= 0:
        print("[WATTS] máscara vazia prenderia o jogo em zero núcleo.", flush=True)
        return 2
    pids_fixados: set[int] = set()

    dll = achar_dll(args.lhm)
    if dll is None:
        print("[WATTS] LibreHardwareMonitorLib.dll não encontrada. "
              "Veja INSTALACAO_WATTS.md (passo 2). O assistente segue sem o watt da CPU.",
              flush=True)
        return 2

    def abrir():
        """(computador, código). Relata e devolve None em vez de estourar."""
        try:
            return _abrir_computador(dll), 0
        except ImportError:
            # pythonnet ausente é o estado normal de quem ainda não instalou nada.
            print("[WATTS] pythonnet não instalado (`pip install pythonnet`). "
                  "O assistente segue sem o watt da CPU.", flush=True)
            return None, 2
        except Exception as exc:                 # noqa: BLE001 - relata, não estoura
            print(f"[WATTS] não consegui abrir a LibreHardwareMonitor: "
                  f"{_explicar_ausencia(exc)}", flush=True)
            return None, 2

    def fechar(computador):
        try:
            computador.Close()
        except Exception:                        # noqa: BLE001
            pass

    if args.uma_vez:
        computador, cod = abrir()
        if computador is None:
            return cod
        try:
            nome, watts = medir(computador)
        except Exception as exc:                 # noqa: BLE001
            print(f"[WATTS] {_explicar_ausencia(exc)}", flush=True)
            return 2
        finally:
            fechar(computador)
        publicar(watts, nome, saida)
        print(f"[WATTS] {nome}: {watts:.1f} W  ->  {saida}", flush=True)
        return 0

    # Auto-cura: uma execução anterior pode ter sido MORTA (taskkill /F, queda de
    # energia) enquanto estava pausada, deixando o dispositivo desabilitado. Sem
    # isto, o watt da CPU sumiria para sempre e ninguém saberia por quê — o
    # `finally` lá embaixo não roda num kill duro. Reabilitar é idempotente.
    if guarda:
        ok, msg = _mexer_no_driver(True)
        if not ok:
            print(f"[WATTS] não consegui garantir o PawnIO habilitado: {msg}", flush=True)

    computador, cod = abrir()
    if computador is None:
        return cod

    print(f"[WATTS] de plantão: publicando em {saida} a cada {args.intervalo:.0f} s."
          + ("" if not guarda else
             f" Solto o kernel se abrir: {', '.join(sorted(alvos))}.")
          + ("" if not (guarda and afinidade_on) else
             f" Prendo o jogo em 0x{mascara:X} (CCD do V-Cache).")
          + " Ctrl+C encerra.", flush=True)

    pausado = False
    falhas = 0
    erro_guarda_avisado = False
    try:
        while True:
            if guarda:
                jogo, procs, erro = _jogo_agora(alvos)
                if erro is None and afinidade_on:
                    # Antes da pausa: fixar não depende de medir, e o jogo pode
                    # abrir enquanto já estamos pausados por causa do anti-cheat.
                    _fixar_no_vcache(procs, mascara, pids_fixados)
                    pids_vivos = {p for p, _ in procs}
                    pids_fixados.intersection_update(pids_vivos)
                if erro is not None:
                    # Não dá para saber se há jogo. MANTÉM o estado atual em vez
                    # de adivinhar: pausar por engano mataria a medição em
                    # silêncio, e retomar por engano devolveria o driver ao
                    # kernel no meio de uma partida.
                    if not erro_guarda_avisado:
                        print(f"[WATTS] guarda de jogo falhou ({erro}) — "
                              "mantendo o estado atual.", flush=True)
                        erro_guarda_avisado = True
                else:
                    erro_guarda_avisado = False
                    v = jogo_ativo.decidir(jogo, pausado)
                    if v.acao is jogo_ativo.Acao.PAUSAR:
                        # Ordem importa: fechar a lib PRIMEIRO. Enquanto ela
                        # segura o handle do dispositivo, o pnputil não desabilita.
                        fechar(computador)
                        computador = None
                        ok, msg = _mexer_no_driver(False)
                        pausado = True
                        print(f"[WATTS] {v.motivo}. PawnIO desabilitado: "
                              f"{'ok' if ok else msg}", flush=True)
                    elif v.acao is jogo_ativo.Acao.RETOMAR:
                        ok, msg = _mexer_no_driver(True)
                        if not ok:
                            print(f"[WATTS] {v.motivo}, mas não reabilitei o "
                                  f"PawnIO: {msg}", flush=True)
                        computador, _ = abrir()
                        pausado = computador is None
                        if not pausado:
                            print(f"[WATTS] {v.motivo}.", flush=True)

            if not pausado and computador is not None:
                try:
                    nome, watts = medir(computador)
                    publicar(watts, nome, saida)
                    falhas = 0
                except Exception as exc:         # noqa: BLE001
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
        if computador is not None:
            fechar(computador)
        # Sair pausado deixaria o PawnIO desabilitado e o watt da CPU morto no
        # próximo boot sem explicação nenhuma. Devolver o dispositivo é parte de
        # encerrar direito.
        if guarda and pausado:
            ok, msg = _mexer_no_driver(True)
            print(f"[WATTS] reabilitando o PawnIO na saída: "
                  f"{'ok' if ok else msg}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
