"""
A ponte entre o AJUDANTE elevado e o servidor que NÃO é elevado.

Por que existe um segundo processo para ler um watt: no Windows a potência do
pacote da CPU não sai sem RING 0. Foram medidos seis caminhos sem driver em
2026-08-03 e os seis vieram vazios — `Win32_PerfFormattedData_PowerMeterCounter_
EnergyMeter` sem instância, `CIM_PowerSupply` sem instância, `CallNtPower
Information` sem campo de potência, `root\\WMI` sem classe de energia, `psutil`
sem API no Windows, e `kernel32.ReadMsr`/`ntdll.NtReadMsr` que simplesmente não
existem. O que resta é o MSR `0xC001029B` da AMD via RDMSR, e RDMSR é instrução
privilegiada: exige um driver em modo kernel.

⚠ E o `main.py` NÃO PODE SER O ELEVADO. Ele escuta em `0.0.0.0`, tem rota que
ESCREVE no vault e vai ganhar acesso remoto pelo celular. Rodá-lo como
administrador para ler um watt trocaria um número por uma superfície de ataque —
a decisão do dono, no mesmo ato em que pediu a medida. Então quem eleva é um
processo mínimo e separado (`scripts/ajudante_watts.py`), e o servidor só LÊ o
que ele publicou.

CANAL: um ARQUIVO em dados/, escrito atomicamente. Foram pesados três:

  - porta em 127.0.0.1 → o elevado viraria SERVIDOR: socket de escuta dentro do
    processo privilegiado, parser de HTTP dentro do processo privilegiado, e
    qualquer coisa rodando como usuário (inclusive uma aba do navegador) podendo
    falar com ele. É o oposto do que R1 pediu.
  - named pipe → sem servidor de rede, mas ainda é o elevado LENDO de um canal
    que o não-elevado abre, mais um quebra-cabeça de DACL para acertar quem pode
    conectar. Mais código privilegiado para o mesmo número.
  - arquivo → o elevado só ESCREVE, o servidor só LÊ. O processo privilegiado
    não tem porta, não tem parser, não recebe byte de ninguém. A direção da
    confiança fica certa: nada que o lado sem privilégio produza chega ao lado
    com privilégio.

⚠ E O QUE O ARQUIVO **NÃO** É: uma fronteira de segurança. Ele mora numa pasta
que o usuário escreve, então um processo comum PODE plantar um número mentiroso
ali. A consequência é um watt errado na faixa do app — nada mais —, e é por isso
que a leitura abaixo trata o conteúdo como ENTRADA HOSTIL: extrai um float dentro
de faixa e um rótulo curto de charset restrito, e nunca um caminho, um comando ou
qualquer coisa que vire ação.

STDLIB PURA, sem UM import do projeto — nem `config`. É o espírito do `vigia.py`
levado um passo adiante, e por um motivo concreto: `config` lê o `.env`, o `.env`
é escrito pelo usuário, e o processo que lê o `.env` aqui é o ELEVADO. Quem
pode editar um arquivo de texto comum não deve poder dizer a um processo
administrador onde escrever. O caminho vem da linha de comando de quem eleva, ou
do default derivado de `__file__`. (Há teste que falha se um import pesado entrar.)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

# Teto de sanidade do watt publicado. Nenhuma CPU de desktop chega perto; acima
# disso é unidade errada ou lixo, e a faixa do app não deve exibir.
WATTS_MAXIMO = 2000.0

# Quanto tempo uma publicação continua valendo. Existe porque um ajudante MORTO
# deixa o último arquivo no disco: sem prazo, a faixa mostraria para sempre o
# watt do instante em que ele caiu, e ninguém saberia que o número congelou.
VALIDADE_PADRAO_S = 15.0

# De quanto em quanto o ajudante republica. Cinco publicações cabem na validade
# acima, então um tranco eventual não apaga o número da tela.
INTERVALO_PADRAO_S = 2.0

# Charset do rótulo de origem. Ele atravessa para o front, e "conteúdo de arquivo
# que o usuário escreve virando texto de página" é a definição de entrada não
# confiável — mesmo que o front use textContent, o estreitamento é de graça.
_ROTULO_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:_-+#()/")
_ROTULO_MAX = 48


@dataclass(frozen=True)
class Leitura:
    """Watt da CPU com a procedência e, quando não há watt, o MOTIVO.

    O motivo não é enfeite: "o ajudante não está de pé" e "o ajudante está de pé
    e publicando lixo" são problemas diferentes, com conserto diferente, e ambos
    aparecem como um campo `None` na tela. Sem o motivo, o dono só descobriria
    qual dos dois reencenando a instalação inteira."""

    watts: Optional[float]
    origem: Optional[str]
    motivo: Optional[str] = None    # None | "ausente" | "invalido" | "vencido"


def caminho_padrao() -> Path:
    """`dados/potencia_cpu.json` na raiz do repo, derivado de `__file__`.

    De `__file__` e não de um caminho absoluto: é a mesma regra do `BASE_DIR` do
    `config.py` — o projeto roda de qualquer pasta e em qualquer máquina. Aqui
    importa em dobro, porque quem chama isto é um processo elevado que não pode
    ir perguntar o caminho a um arquivo de configuração editável."""
    return Path(__file__).resolve().parent.parent / "dados" / "potencia_cpu.json"


def _rotulo_limpo(bruto: object) -> Optional[str]:
    """Origem publicada → rótulo curto e inofensivo, ou None."""
    if not isinstance(bruto, str):
        return None
    limpo = "".join(c for c in bruto if c in _ROTULO_OK).strip()
    return limpo[:_ROTULO_MAX] or None


# --------------------------------------------------------------------------- #
# Lado do SERVIDOR (sem privilégio): só lê                                     #
# --------------------------------------------------------------------------- #
def ler_publicacao(
    caminho: Optional[Path] = None,
    validade_s: float = VALIDADE_PADRAO_S,
    agora: Optional[float] = None,
) -> Leitura:
    """O que o ajudante publicou, se ainda vale. `agora` INJETADO para o teste.

    Nunca levanta e nunca devolve 0.0 por engano: ajudante fora do ar dá
    `watts=None` com motivo, e o app segue — a regra que o `energia.py` cravou,
    porque zero seria indistinguível de "medi e a CPU não consome nada"."""
    alvo = Path(caminho) if caminho else caminho_padrao()
    instante = float(agora) if agora is not None else time.time()
    try:
        bruto = alvo.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Leitura(None, None, "ausente")
    except OSError:
        return Leitura(None, None, "ausente")

    try:
        dados = json.loads(bruto)
    except (ValueError, TypeError):
        # Publicação parcial é ESPERADA se alguém escrever sem `os.replace`; o
        # `publicar` abaixo é atômico justamente para isto nunca acontecer com o
        # nosso ajudante, mas o leitor não confia no escritor.
        return Leitura(None, None, "invalido")
    if not isinstance(dados, dict):
        return Leitura(None, None, "invalido")

    watts, carimbo = dados.get("watts"), dados.get("ts")
    # `bool` é subclasse de `int` no Python: sem esta exclusão, `{"watts": true}`
    # viraria 1,0 W e seria exibido como medição.
    if isinstance(watts, bool) or not isinstance(watts, (int, float)):
        return Leitura(None, None, "invalido")
    if isinstance(carimbo, bool) or not isinstance(carimbo, (int, float)):
        return Leitura(None, None, "invalido")
    valor = float(watts)
    if valor != valor or valor <= 0.0 or valor > WATTS_MAXIMO:   # NaN falha o 1º teste
        return Leitura(None, None, "invalido")
    # `abs`: relógio adiantado no lado que publica é tão suspeito quanto atrasado,
    # e um carimbo no futuro faria a publicação "nunca vencer".
    if abs(instante - float(carimbo)) > validade_s:
        return Leitura(None, None, "vencido")
    return Leitura(valor, _rotulo_limpo(dados.get("origem")), None)


# --------------------------------------------------------------------------- #
# Lado do AJUDANTE (elevado): só escreve                                       #
# --------------------------------------------------------------------------- #
def publicar(
    watts: float,
    origem: str,
    caminho: Optional[Path] = None,
    agora: Optional[float] = None,
) -> Path:
    """Grava a medida ATOMICAMENTE (tmp no mesmo diretório + `os.replace`).

    Atômico não é preciosismo: o leitor roda a cada 20 s sem qualquer combinação
    com o escritor, então uma escrita em duas etapas seria lida pela metade mais
    cedo ou mais tarde — e um JSON truncado é exatamente o "invalido" que faria
    o dono caçar um defeito que não existe."""
    alvo = Path(caminho) if caminho else caminho_padrao()
    alvo.parent.mkdir(parents=True, exist_ok=True)
    corpo = json.dumps({
        "watts": round(float(watts), 2),
        "origem": str(origem)[:_ROTULO_MAX],
        "ts": float(agora) if agora is not None else time.time(),
        "pid": os.getpid(),
    })
    temporario = alvo.with_name(alvo.name + f".{os.getpid()}.tmp")
    temporario.write_text(corpo, encoding="utf-8")
    os.replace(temporario, alvo)      # atômico no mesmo volume, Windows incluso
    return alvo


def escolher_sensor(
    sensores: Sequence[tuple[str, Optional[float]]] | Iterable[tuple[str, Optional[float]]],
) -> Optional[tuple[str, float]]:
    """Qual sensor de potência é a potência de PACOTE. Puro/testável.

    A LibreHardwareMonitorLib entrega vários sensores de potência por CPU, e eles
    NÃO são intercambiáveis: num 7950X3D, "CPU Cores" deixa de fora o IO die e a
    Infinity Fabric, que consomem dezenas de watts. Pegar o primeiro da lista
    daria um número plausível e errado — o pior tipo.

    Ordem: quem diz "package" ganha; depois quem fala de CPU/socket; e o resto
    só entra se não houver mais nada, sempre com o NOME preservado, para o rótulo
    na tela dizer de qual sensor aquele watt saiu."""
    candidatos = [(nome, float(valor)) for nome, valor in sensores
                  if isinstance(nome, str) and isinstance(valor, (int, float))
                  and not isinstance(valor, bool) and 0.0 < float(valor) <= WATTS_MAXIMO]
    if not candidatos:
        return None

    def prioridade(item: tuple[str, float]) -> int:
        nome = item[0].lower()
        if "package" in nome:
            return 0
        if "cpu" in nome or "socket" in nome:
            return 1
        return 2

    return min(candidatos, key=prioridade)
