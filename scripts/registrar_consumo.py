"""
Wattímetro AVULSO — registra o consumo com o assistente FECHADO (durante o jogo).

Por que existe: o acumulador (`consumo.py`) é alimentado pelo tick do scheduler, ou
seja, só enquanto o `main.py` está no ar. Mas a hora em que o PC mais gasta é
justamente a que o dono fecha o assistente para jogar — e aí o dia inteiro fica com
um buraco na cobertura bem no pico. Este script preenche esse buraco.

⚠ NÃO É O SENSOR QUE ATRAPALHA O JOGO — É O APP. Amostrar a NVML custa ~1 ms e não
toca o kernel; quem pesa é o `main.py` segurando o LLM (~4-5 GB de VRAM) e o Whisper
em cuda. Por isso a resposta não é "deixe o assistente aberto para medir": é este
processo, que não importa torch, não sobe modelo e não abre porta. Mesma filosofia do
`vigia.py`, e pelo mesmo motivo — há teste em subprocesso que falha se o torch entrar.

⚠ NUNCA RODE JUNTO COM O ASSISTENTE. `RegistroConsumo` guarda o baseline do contador
EM RAM, por instância: duas instâncias vivas leem o mesmo contador monotônico da NVML,
cada uma calcula o delta desde a SUA última leitura e as duas somam no banco — a
energia conta DOBRADO. Não é um erro que apareça no gráfico como falha; aparece como
um dia caro, que é indistinguível de um dia em que se jogou muito. Por isso a guarda é
a primeira coisa que roda, e ela recusa em vez de avisar.

⚠ A CPU SOME DURANTE A PARTIDA, E ISSO É DE PROPÓSITO. O `jogo_ativo.py` desabilita o
PawnIO (`pnputil /disable-device`) quando o Tarkov sobe, porque o BattlEye divide o
mesmo andar do kernel. Sem driver não há MSR, e sem MSR não há watt de pacote. O
`tomada.estimar` já trata ausência como o caso NORMAL: a parcela da CPU vira a faixa
`repouso..PPT` e o total sai mais largo, dizendo por quê. O que ela nunca vira é zero —
um zero aqui viraria "a CPU não gastou nada durante três horas de jogo".

    python scripts/registrar_consumo.py                  # até Ctrl+C
    python scripts/registrar_consumo.py --enquanto-jogo  # sai quando o jogo fechar
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mente_digital import potencia, potencia_cpu, rede, tomada     # noqa: E402
from mente_digital.config import settings                          # noqa: E402
from mente_digital.consumo import RegistroConsumo                  # noqa: E402

#: Margem sobre o intervalo de amostragem, para o laço não depender de acordar no
#: milissegundo certo. O `devido()` do registro é quem manda de verdade.
_PASSO_S = 1.0


def servidor_no_ar() -> bool:
    """O assistente está de pé? Testado por BIND na porta, não por connect — é a
    mesma checagem (~0,2 ms) que o `rede.py` já faz no topo do lifespan."""
    return rede.porta_em_uso(settings.host, settings.port)


def _jogo_em_execucao() -> Optional[str]:
    """Nome do jogo rodando, ou None. Import TARDIO e tolerante: o `--enquanto-jogo`
    é opcional, e um `jogo_ativo` ausente não pode impedir a medição de acontecer."""
    try:
        from mente_digital import jogo_ativo
        nome, _procs, _erro = jogo_ativo._jogo_agora(jogo_ativo.JOGOS_PADRAO)
        return nome
    except Exception:
        return None


def medir_sensores() -> dict:
    """As duas leituras de potência, no formato que o `tomada.a_partir_de_energia` lê.

    ⚠ POR QUE NÃO `energia.medir()`, que é o que o scheduler usa. Aquela função é a
    medição COMPLETA do app e inclui a leitura de VRAM — que é feita por torch, e
    `torch.cuda` INICIALIZA UM CONTEXTO CUDA na placa. Um contexto custa algumas
    centenas de MB de VRAM na mesma GPU em que o jogo está rodando: seria um medidor
    que atrapalha exatamente o que veio medir. Medido: importar `energia` não traz o
    torch; CHAMAR `medir()` traz. Um teste de `sys.modules` só no import passaria
    verde e não protegeria nada — por isso a fumaça deste script mede DEPOIS da
    amostra.

    Aqui só entram os dois sensores que a conta de energia usa, e nenhum deles toca a
    GPU: a NVML é uma DLL de userspace e o watt da CPU é um arquivo publicado pelo
    ajudante elevado. VRAM não entra na conta de watt nenhuma."""
    gpu = potencia.medida_gpu()
    cpu = potencia_cpu.ler_publicacao()
    return {
        "gpu_watts": gpu.watts,
        # `None`, nunca 0: sem o ajudante (ou com o PawnIO fora por causa do
        # BattlEye) o certo é "não sei", e o `tomada` modela a faixa. Um zero aqui
        # afirmaria que a CPU não gastou nada durante horas de jogo.
        "cpu_watts": getattr(cpu, "watts", None),
    }


def uma_amostra(registro: RegistroConsumo) -> Optional[dict]:
    """Uma leitura, se já for devida. Devolve o que foi somado, ou None.

    A conta é a mesma do `SchedulerService._amostrar_consumo` de propósito — só a
    origem da medida difere, pelo motivo explicado em `medir_sensores`."""
    if not registro.devido(settings.consumo_intervalo_seconds):
        return None
    medida = medir_sensores()
    mj = potencia.energia_acumulada_mj()
    parede = tomada.a_partir_de_energia(medida)
    return registro.registrar(
        mj, medida.get("cpu_watts"), parede.total.minimo, parede.total.maximo,
    )


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--enquanto-jogo", action="store_true",
                   help="sai sozinho quando nenhum jogo estiver mais rodando")
    p.add_argument("--espera-jogo", type=float, default=120.0,
                   help="segundos a esperar o jogo APARECER antes de desistir "
                        "(só com --enquanto-jogo; o launcher demora a abrir)")
    p.add_argument("--silencioso", action="store_true", help="não imprime cada amostra")
    args = p.parse_args(argv)

    # A guarda vem ANTES de qualquer coisa — ver o ⚠ do cabeçalho.
    if servidor_no_ar():
        print(f"RECUSADO: o assistente está no ar na porta {settings.port}.\n"
              "          Ele já amostra pelo tick do scheduler; rodar os dois faria a\n"
              "          energia contar DOBRADO. Feche o assistente ou não rode isto.",
              file=sys.stderr)
        return 2
    if not settings.consumo_habilitado:
        print("RECUSADO: MENTE_CONSUMO_HABILITADO=false.", file=sys.stderr)
        return 2

    registro = RegistroConsumo(settings.db_telemetria)
    registro.init()

    parando = {"agora": False}

    def _parar(_sig, _frm):
        parando["agora"] = True

    signal.signal(signal.SIGINT, _parar)
    with_term = getattr(signal, "SIGTERM", None)
    if with_term is not None:
        signal.signal(with_term, _parar)

    print(f"wattímetro avulso: amostrando a cada {settings.consumo_intervalo_seconds:.0f}s "
          f"-> {settings.db_telemetria}")
    if args.enquanto_jogo:
        print(f"modo --enquanto-jogo: espero até {args.espera_jogo:.0f}s o jogo aparecer.")

    t0 = time.monotonic()
    viu_jogo = False
    amostras = 0
    while not parando["agora"]:
        # O jogo pode subir DEPOIS deste script (é o caso normal: o atalho dispara os
        # dois). Por isso a ausência só encerra depois de o jogo ter sido visto ao
        # menos uma vez — ou de estourar a espera inicial.
        if args.enquanto_jogo:
            jogo = _jogo_em_execucao()
            if jogo:
                if not viu_jogo:
                    print(f"jogo detectado: {jogo}")
                viu_jogo = True
            elif viu_jogo:
                print("jogo encerrado — parando.")
                break
            elif (time.monotonic() - t0) > args.espera_jogo:
                print("nenhum jogo apareceu na espera — parando.")
                break

        # Uma falha de contabilidade não pode matar o medidor: a mesma regra do
        # scheduler, e aqui vale mais ainda, porque ninguém está olhando a tela.
        try:
            somado = uma_amostra(registro)
        except Exception as exc:                       # noqa: BLE001
            print(f"[erro] amostra descartada: {exc}", file=sys.stderr)
            somado = None

        if somado is not None:
            amostras += 1
            if not args.silencioso:
                print(f"  #{amostras} {somado}")
        time.sleep(_PASSO_S)

    print(f"fim: {amostras} amostra(s) gravada(s). descartes={registro.descartes()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
