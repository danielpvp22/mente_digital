"""
Quanto o PC INTEIRO puxa da TOMADA — estimativa honesta, ancorada no que é MEDIDO.

Por que existe: `potencia.py` já sabe o watt da GPU (NVML) e `potencia_cpu.py` o do
pacote da CPU (ajudante elevado). Somados, os dois respondem "quanto os dois chips
gastam" — que NÃO é a pergunta do dono. Ele paga a conta da PAREDE, e entre os chips
e a parede há a perda da fonte, a placa-mãe, a memória, os discos, a refrigeração, os
periféricos e dois monitores que nem passam pela fonte. Este módulo faz essa ponte, e
carrega em cada parcela se ela foi MEDIDA ou MODELADA.

⚠ ISTO NÃO É UM WATTÍMETRO. Duas das parcelas vêm de sensor; o resto vem de modelo.
Um número único e redondo aqui seria precisão falsa — por isso tudo trafega como
`Faixa` (min–max) e o resultado lista, por nome, o que foi estimado. Quem exibir tem
de dizer isso, como o tooltip do `index.html` já diz para o watt da GPU. A régua é a
mesma de `energia.somar_watts`: um número plausível não levanta suspeita nenhuma, e
por isso é mais perigoso que um campo vazio.

TRÊS ERROS DE MODELAGEM QUE ESTE MÓDULO EVITA DE PROPÓSITO — os três produzem um
número plausível, que é exatamente o que ninguém confere:

 1. MONITOR NÃO PASSA PELA FONTE DO PC. Ele tem cabo próprio na tomada. Jogá-lo na
    carga DC erra DUAS vezes: infla o denominador da fração de carga (mudando a
    eficiência lida da curva) e ainda aplica a perda da fonte sobre algo que não
    passa por ela. Aqui isso é impossível por construção, não por convenção: cada
    `Parcela` carrega `na_fonte`, e a soma parte as duas turmas antes de dividir.
 2. O ESPELHO DISSO: teclado, mouse e headset USB são alimentados PELA fonte do PC.
    "Periférico" soa como "coisa externa" e o reflexo é mandá-lo para o lado da
    parede junto com o monitor — mas o watt do teclado sai do rail de 5 V, e sofre a
    perda da fonte como qualquer outro. Cabo próprio é o critério, não o tamanho.
 3. O WATT DO PACOTE DA CPU NÃO É O QUE A FONTE ENTREGA. O MSR reporta o que o
    pacote consome DEPOIS do VRM da placa-mãe, que converte 12 V em ~1,1 V perdendo
    ~10%. Somar o pacote cru subestima em ~15 W sob carga. A perda entra como parcela
    PRÓPRIA e visível (`vrm_eficiencia`), em vez de sumir dentro de outro número.
    A GPU não leva essa correção: TGP e NVML já são potência DE PLACA, depois do VRM
    dela — aplicar o desconto nas duas seria contar a mesma perda duas vezes.

⚠ E A FRAGILIDADE QUE FICA: a curva da fonte abaixo de 20% de carga. A norma 80 PLUS
Gold só exige 20/50/100% — o ponto de 10% (e o de 5%) são ESTIMATIVA do comportamento
típico da classe, não norma e não medição desta fonte. Ver `CURVA_80PLUS_GOLD`.

PURO: nenhum IO, nenhum relógio, nenhum import do projeto. As duas medições entram
como argumento (`estimar(gpu_watts=..., cpu_watts=...)`), do mesmo jeito que o `agora`
entra em `agenda.parse_quando` — quem tem o sensor é `energia.medir()`, e a fiação
entre os dois é de quem chama.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence

# Teto de sanidade de uma parcela ISOLADA (uma GPU, uma CPU, um monitor). Mesmo
# espírito de `potencia_cpu.WATTS_MAXIMO`: acima disto é unidade errada ou lixo, não
# um componente de PC doméstico — e um watt errado aqui vira uma conta de luz errada.
WATTS_MAXIMO_PARCELA = 2000.0

# Teto ESTRUTURAL de uma Faixa qualquer, inclusive de somas e totais. Existe só para
# barrar NaN/inf e absurdo grosseiro na composição; a régua apertada é a de cima, e
# se aplica na porta de entrada (`estimar`), onde os números do sensor chegam.
_FAIXA_TETO_W = 100_000.0

# Curva de eficiência × fração de carga da fonte.
#
# ⚠ PROCEDÊNCIA, que é o que decide quanto confiar em cada ponto:
#   - 20%/50%/100% → 87%/90%/87% é o REQUISITO da classe 80 PLUS Gold a 115 V. É o
#     PISO da certificação, não a medida desta unidade: reviews independentes da
#     RM850e mediram ~88,7% / ~91,2% / ~87,3% nos mesmos três pontos. Ficamos com o
#     piso de propósito — assim o erro do modelo é para CIMA (superestima a conta),
#     que é o lado seguro de errar quando se fala de gasto.
#   - 10% e 5% → NÃO EXISTEM na exigência Gold (só o Titanium tem ponto de 10%) e não
#     foram medidos aqui. São estimativa do rolloff típico da classe, e é o trecho
#     mais frágil deste módulo. Importa pouco na prática: com a GPU em repouso esta
#     máquina já fica perto de 20% de carga, então o interpolador raramente desce.
#
# Interpolação linear entre pontos; fora dos extremos, CLAMPA (ver `eficiencia_fonte`).
CURVA_80PLUS_GOLD: tuple[tuple[float, float], ...] = (
    (0.05, 0.78),
    (0.10, 0.85),
    (0.20, 0.87),
    (0.50, 0.90),
    (1.00, 0.87),
)


# --------------------------------------------------------------------------- #
# Tipos                                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Faixa:
    """Um intervalo de watts. `minimo == maximo` é um ponto (o caso do sensor).

    Por que faixa e não número: metade das parcelas aqui não tem sensor, e a
    incerteza delas é grande de verdade — dois monitores valem ~1 W em standby e
    ~90 W acesos no talo. Colapsar isso num "valor típico" entregaria um número
    que ninguém consegue conferir e que estaria errado quase sempre. A faixa diz o
    que se sabe e, principalmente, o que não se sabe."""

    minimo: float
    maximo: float

    def __post_init__(self) -> None:
        for valor, rotulo in ((self.minimo, "minimo"), (self.maximo, "maximo")):
            # `valor != valor` é o teste de NaN que não precisa de import. NaN passa
            # calado por qualquer comparação e contaminaria todas as somas adiante.
            if valor != valor or valor < 0.0 or valor > _FAIXA_TETO_W:
                raise ValueError(f"Faixa.{rotulo} fora de faixa plausível: {valor!r} W")
        if self.minimo > self.maximo:
            raise ValueError(f"Faixa invertida: {self.minimo} > {self.maximo}")

    @property
    def centro(self) -> float:
        """O meio do intervalo. ⚠ Não é "o valor provável" — a distribuição real
        dentro da faixa é desconhecida. Serve para ordenar e para um resumo de uma
        linha, não para responder "quanto gasto"."""
        return (self.minimo + self.maximo) / 2.0

    @property
    def e_ponto(self) -> bool:
        return self.minimo == self.maximo

    def __add__(self, outra: "Faixa") -> "Faixa":
        if not isinstance(outra, Faixa):
            return NotImplemented
        return Faixa(self.minimo + outra.minimo, self.maximo + outra.maximo)

    def escalar(self, fator: float) -> "Faixa":
        """A faixa multiplicada por uma contagem (2 monitores, 3 ventoinhas)."""
        if fator != fator or fator < 0.0:
            raise ValueError(f"fator de escala inválido: {fator!r}")
        return Faixa(self.minimo * fator, self.maximo * fator)


def ponto(watts: float) -> Faixa:
    """Faixa de largura zero — o formato de uma parcela MEDIDA."""
    return Faixa(watts, watts)


@dataclass(frozen=True)
class Parcela:
    """Um pedaço do consumo, com a procedência e o CAMINHO ELÉTRICO junto.

    `na_fonte` não é metadado decorativo: é ele que impede o erro nº 1 do cabeçalho.
    Enquanto a informação viver na estrutura, nenhum somatório futuro consegue
    empurrar um monitor para dentro da conta da fonte por descuido."""

    nome: str
    faixa: Faixa
    medido: bool
    fonte: str          # em uma expressão curta: de onde este número veio
    na_fonte: bool = True   # False = tem cabo próprio na tomada (monitor)


@dataclass(frozen=True)
class Placa:
    """Uma placa de vídeo, para o cenário sem medição (e para simular a troca)."""

    nome: str
    tgp_w: float        # o teto oficial do fabricante
    repouso_w: float    # o piso, com a máquina acordada e sem trabalho
    fonte: str          # procedência dos dois números acima


@dataclass(frozen=True)
class Cpu:
    """Um processador. ⚠ O teto usado é o PPT, não o TDP.

    TDP é especificação de DISSIPAÇÃO (o que o cooler precisa dar conta), não de
    consumo: o 7950X3D é 120 W de TDP e puxa até 162 W de PPT do socket. Modelar
    pelo TDP subestimaria o pico em 42 W — e daria um número plausível."""

    nome: str
    tdp_w: float        # fica registrado porque é o número que todo mundo cita…
    ppt_w: float        # …mas é ESTE que a fonte entrega no pico
    repouso_w: float
    fonte: str


# --------------------------------------------------------------------------- #
# O hardware do dono (defaults; tudo trocável pelo Cenario)                    #
# --------------------------------------------------------------------------- #
# TGP oficial NVIDIA: 320 W. Corroborado NESTA máquina pelo próprio projeto — o
# `potencia.limite_gpu()` (enforced power limit da NVML) leu 320 W em 2026-08-03.
# O repouso de 96 W é MEDIDO aqui (o mesmo número que o cabeçalho de `potencia.py`
# registra como "96 W parada"); é alto para uma 3080 porque dois monitores mantêm os
# clocks de memória subidos — e é por isso que ele não muda na linha de baixo.
RTX_3080 = Placa(
    nome="RTX 3080 10 GB",
    tgp_w=320.0,
    repouso_w=96.0,
    fonte="TGP oficial NVIDIA (320 W), confirmado pela NVML nesta máquina; repouso medido aqui",
)

# TGP oficial NVIDIA: 360 W (a placa pede fonte de 850 W no mínimo — exatamente a
# que já está na máquina). ⚠ O repouso repetido dos 96 W da 3080 é DELIBERADO e é
# uma suposição, não uma medida: o repouso alto aqui é do CONJUNTO (dois monitores
# segurando os clocks de VRAM), não da placa, e trocar a placa não muda esse motivo.
# Repetir mantém a comparação 3080→5080 limpa: só o teto muda. Se a 5080 chegar e o
# repouso medido for outro, é este campo que se corrige — e só ele.
# ⚠ Modelos de fabricante com OC de fábrica sobem o power limit (400 W+ existem);
# quando a placa chegar, o número honesto sai do `potencia.limite_gpu()` dela.
RTX_5080 = Placa(
    nome="RTX 5080 16 GB",
    tgp_w=360.0,
    repouso_w=96.0,
    fonte="TGP oficial NVIDIA (360 W); repouso HERDADO da 3080 desta máquina (suposição)",
)

RYZEN_7950X3D = Cpu(
    nome="Ryzen 9 7950X3D",
    tdp_w=120.0,
    ppt_w=162.0,
    repouso_w=30.0,
    fonte="TDP/PPT oficiais AMD (120 W / 162 W); repouso estimado",
)


@dataclass(frozen=True)
class Cenario:
    """A máquina inteira em números. Tudo aqui é trocável — os defaults descrevem o
    PC do dono, e as parcelas sem sensor são ESTIMATIVA declarada (campo `fonte` de
    cada `Parcela` no resultado).

    Trocar a GPU é trocar UM campo: `dataclasses.replace(CENARIO_PADRAO, placa=RTX_5080)`
    — ou o `CENARIO_5080` já pronto logo abaixo."""

    placa: Placa = RTX_3080
    cpu: Cpu = RYZEN_7950X3D

    fonte_nome: str = "Corsair RM850e (850 W, 80 Plus Gold, full modular)"
    fonte_capacidade_w: float = 850.0
    fonte_curva: tuple[tuple[float, float], ...] = CURVA_80PLUS_GOLD

    # Eficiência do VRM da placa-mãe (12 V → tensão de núcleo). Ver erro nº 3 no
    # cabeçalho. 1.0 desliga a correção, se o dono preferir só o número do MSR.
    vrm_eficiencia: float = 0.90

    # Placa-mãe + DDR5 + NVMe + chipset, SEM o VRM da CPU (que é parcela à parte,
    # para não contar a mesma perda duas vezes). Estimativa: base da placa ~20-30 W,
    # DIMMs ~3-8 W cada, NVMe ~2-8 W, chipset ~7-14 W.
    placa_mae_w: Faixa = Faixa(30.0, 70.0)

    # Water cooler 240 (bomba ~3-8 W + 2 fans no radiador) mais as fans do gabinete.
    refrigeracao_w: Faixa = Faixa(8.0, 25.0)

    # ⚠ Erro nº 2 do cabeçalho: isto é carga DC. Teclado, mouse, headset e webcam
    # comem do rail de 5 V da fonte do PC. RGB no teclado sozinho pode valer 5 W.
    perifericos_usb_w: Faixa = Faixa(2.0, 12.0)

    # Monitores — CABO PRÓPRIO NA TOMADA (erro nº 1). Nunca entram na carga DC.
    monitores: int = 2
    # None = não sabemos se estão acesos, e a faixa cobre standby..aceso. É a mesma
    # regra do resto do projeto: ausência de informação não vira um chute silencioso.
    monitores_ligados: Optional[bool] = None
    # 24-27" IPS. Painel grande/4K/HDR passa disto — se for o caso, suba o campo.
    monitor_ligado_w: Faixa = Faixa(18.0, 45.0)
    monitor_standby_w: Faixa = Faixa(0.3, 1.0)


CENARIO_PADRAO = Cenario()
# O "e quando a 5080 chegar?" — um campo de diferença, para o número da troca sair
# do mesmo modelo e não de uma segunda conta feita à mão.
CENARIO_5080 = replace(CENARIO_PADRAO, placa=RTX_5080)


@dataclass(frozen=True)
class Tomada:
    """O resultado. Discrimina o caminho elétrico e o que é chute.

    `total` é o que um wattímetro de parede leria com tudo ligado na mesma régua:
    o PC visto pela fonte MAIS o que tem cabo próprio."""

    total: Faixa                        # W na parede, PC + periféricos de tomada própria
    pc_dc: Faixa                        # carga DC, antes da perda da fonte
    pc_parede: Faixa                    # pc_dc dividido pela eficiência
    fora_da_fonte: Faixa                # monitores e afins
    eficiencia: Faixa                   # a eficiência aplicada nos dois extremos da carga
    fracao_de_carga: Faixa              # pc_dc / capacidade da fonte (0-1), para diagnóstico
    parcelas: tuple[Parcela, ...]
    avisos: tuple[str, ...] = ()

    @property
    def estimados(self) -> tuple[str, ...]:
        """Nomes das parcelas que NÃO vieram de sensor. Nunca vem vazio: placa-mãe,
        refrigeração, periféricos e monitores não têm medição neste projeto, então
        o total é sempre, em parte, modelo. A estrutura diz isso em vez de deixar o
        leitor supor."""
        return tuple(p.nome for p in self.parcelas if not p.medido)

    @property
    def medidos(self) -> tuple[str, ...]:
        return tuple(p.nome for p in self.parcelas if p.medido)

    def resumo(self) -> str:
        """Uma linha para log/tela, com a incerteza na frente e não no rodapé.

        Lidera pela FAIXA de propósito: quem lê "cerca de 458 W" e só depois o
        intervalo guarda o 458. O centro vai entre parênteses, e a lista do que é
        estimativa vai junto — sem ela a frase vira uma afirmação de medição.

        ⚠ Sem "≈" e sem outro símbolo fora do cp1252: esta frase é candidata a log,
        e o console do Windows nesta env é cp1252 — `print` de U+2248 levanta
        UnicodeEncodeError e derruba quem só queria registrar uma linha (aconteceu
        na 1ª execução deste módulo). Acentuação comum é segura; matemática não."""
        faixa = f"{self.total.minimo:.0f} a {self.total.maximo:.0f} W na tomada"
        faixa = f"{faixa} (centro em cerca de {self.total.centro:.0f} W)"
        if self.medidos:
            faixa += f"; medido: {', '.join(self.medidos)}"
        if self.estimados:
            faixa += f"; estimado: {', '.join(self.estimados)}"
        return faixa.replace(".", ",")


# --------------------------------------------------------------------------- #
# A curva da fonte (pura)                                                      #
# --------------------------------------------------------------------------- #
def fracao_de_carga(carga_dc_w: float, capacidade_w: float) -> float:
    """Quanto da fonte está sendo usado, de 0 a 1 (pode passar de 1 em sobrecarga)."""
    if capacidade_w != capacidade_w or capacidade_w <= 0.0:
        raise ValueError(f"capacidade da fonte inválida: {capacidade_w!r} W")
    if carga_dc_w != carga_dc_w or carga_dc_w < 0.0:
        raise ValueError(f"carga DC inválida: {carga_dc_w!r} W")
    return carga_dc_w / capacidade_w


def eficiencia_fonte(
    carga_dc_w: float,
    capacidade_w: float,
    curva: Sequence[tuple[float, float]] = CURVA_80PLUS_GOLD,
) -> float:
    """A eficiência da fonte NAQUELA carga, interpolada na curva.

    Fora dos extremos da curva, CLAMPA em vez de extrapolar: a reta que passa pelos
    dois pontos mais baixos, prolongada até 0% de carga, devolveria uma eficiência
    otimista e inventada justamente na região onde a fonte é pior — extrapolar aqui
    seria produzir número onde não há dado, que é o oposto do propósito do módulo."""
    pontos = sorted((float(x), float(y)) for x, y in curva)
    if not pontos:
        raise ValueError("curva de eficiência vazia")
    for x, y in pontos:
        if x <= 0.0 or y <= 0.0 or y > 1.0:
            raise ValueError(f"ponto inválido na curva de eficiência: ({x!r}, {y!r})")

    fracao = fracao_de_carga(carga_dc_w, capacidade_w)
    if fracao <= pontos[0][0]:
        return pontos[0][1]
    if fracao >= pontos[-1][0]:
        return pontos[-1][1]
    for (x0, y0), (x1, y1) in zip(pontos, pontos[1:]):
        if x0 <= fracao <= x1:
            return y0 + (y1 - y0) * (fracao - x0) / (x1 - x0)
    # Inalcançável: os dois clamps acima já cobrem fora-do-intervalo, e o laço cobre
    # o miolo. Fica explícito porque "cair fora do for" silenciosamente devolveria
    # None e viraria um TypeError três frames adiante, longe da causa.
    raise ValueError(f"fração de carga não coberta pela curva: {fracao!r}")


# --------------------------------------------------------------------------- #
# A estimativa                                                                 #
# --------------------------------------------------------------------------- #
def _validar_medicao(valor: Optional[float], rotulo: str) -> Optional[float]:
    """Medição que chega da porta de fora. None é ausência LEGÍTIMA (o ajudante da
    CPU não está de pé quase nunca) e segue adiante; lixo LEVANTA.

    Levanta em vez de devolver None de propósito: um sensor devolvendo -5 W ou
    9.999 W é defeito, e transformá-lo em "ausente" faria a estimativa continuar
    com a parcela modelada, entregando um total plausível que esconde o defeito."""
    if valor is None:
        return None
    if isinstance(valor, bool):
        # `bool` é subclasse de `int`: sem isto, `True` viraria 1,0 W medido — a
        # mesma armadilha que `potencia_cpu.ler_publicacao` já documenta.
        raise ValueError(f"{rotulo}: booleano não é medição de potência")
    if not isinstance(valor, (int, float)):
        raise ValueError(f"{rotulo}: esperava número, veio {type(valor).__name__}")
    numero = float(valor)
    if numero != numero or numero < 0.0 or numero > WATTS_MAXIMO_PARCELA:
        raise ValueError(f"{rotulo}: {numero!r} W está fora de qualquer faixa plausível")
    return numero


def _somar(parcelas: Sequence[Parcela], na_fonte: bool) -> Faixa:
    """Soma só um dos lados do caminho elétrico. É aqui que o erro nº 1 morre."""
    total = Faixa(0.0, 0.0)
    for parcela in parcelas:
        if parcela.na_fonte is na_fonte:
            total = total + parcela.faixa
    return total


def _parcela_gpu(gpu_watts: Optional[float], cenario: Cenario) -> Parcela:
    if gpu_watts is not None:
        # NVML entrega potência DE PLACA (o que os conectores + slot entregam), que
        # é exatamente o que a fonte vê. Nenhuma correção de VRM aqui — ver erro nº 3.
        return Parcela("GPU", ponto(gpu_watts), True, "NVML (potência do dispositivo)")
    return Parcela(
        "GPU", Faixa(cenario.placa.repouso_w, cenario.placa.tgp_w), False,
        f"{cenario.placa.nome}: {cenario.placa.fonte}",
    )


def _parcela_cpu(cpu_watts: Optional[float], cenario: Cenario) -> Parcela:
    if cpu_watts is not None:
        return Parcela("CPU", ponto(cpu_watts), True, "ajudante elevado (MSR, potência de pacote)")
    return Parcela(
        "CPU", Faixa(cenario.cpu.repouso_w, cenario.cpu.ppt_w), False,
        f"{cenario.cpu.nome}: {cenario.cpu.fonte}",
    )


def _parcela_monitores(cenario: Cenario) -> Parcela:
    """⚠ `na_fonte=False`. Ver o erro nº 1 no cabeçalho do módulo."""
    if cenario.monitores < 0:
        raise ValueError(f"contagem de monitores inválida: {cenario.monitores}")
    if cenario.monitores_ligados is True:
        por_monitor, nota = cenario.monitor_ligado_w, "acesos"
    elif cenario.monitores_ligados is False:
        por_monitor, nota = cenario.monitor_standby_w, "em standby"
    else:
        # Não sabemos, e a faixa DIZ que não sabemos em vez de arbitrar.
        por_monitor = Faixa(cenario.monitor_standby_w.minimo, cenario.monitor_ligado_w.maximo)
        nota = "estado desconhecido (standby..acesos)"
    return Parcela(
        f"monitores ({cenario.monitores}×)", por_monitor.escalar(cenario.monitores),
        False, f"estimativa, {nota} — tomada própria, fora da fonte", na_fonte=False,
    )


def estimar(
    gpu_watts: Optional[float] = None,
    cpu_watts: Optional[float] = None,
    cenario: Cenario = CENARIO_PADRAO,
) -> Tomada:
    """A estimativa da parede. As duas medições entram por argumento; o que falta
    vira faixa modelada e é declarado como tal em `Tomada.estimados`.

    Ausência é o caso NORMAL para a CPU: ler o watt de pacote no Windows exige
    driver de kernel (ver `potencia_cpu.py`), então o ajudante elevado quase nunca
    está de pé. Nesse caso a parcela da CPU vira `repouso..PPT` e o total continua
    saindo — mais largo, e dizendo por quê. O que ela nunca vira é zero."""
    gpu = _validar_medicao(gpu_watts, "gpu_watts")
    cpu = _validar_medicao(cpu_watts, "cpu_watts")
    if not 0.0 < cenario.vrm_eficiencia <= 1.0:
        raise ValueError(f"vrm_eficiencia fora de (0, 1]: {cenario.vrm_eficiencia!r}")

    parcela_cpu = _parcela_cpu(cpu, cenario)
    # A perda do VRM é DERIVADA da parcela da CPU e aparece separada, para o dono ver
    # de onde saiu. Sempre estimada, mesmo quando o watt da CPU é medido: o sensor
    # mede o pacote, não o conversor que o alimenta.
    perda_vrm = Faixa(
        parcela_cpu.faixa.minimo * (1.0 / cenario.vrm_eficiencia - 1.0),
        parcela_cpu.faixa.maximo * (1.0 / cenario.vrm_eficiencia - 1.0),
    )

    parcelas = (
        _parcela_gpu(gpu, cenario),
        parcela_cpu,
        Parcela("perda do VRM da CPU", perda_vrm, False,
                f"modelo: VRM da placa-mãe a {cenario.vrm_eficiencia:.0%}"),
        Parcela("placa-mãe, memória e discos", cenario.placa_mae_w, False, "estimativa"),
        Parcela("refrigeração", cenario.refrigeracao_w, False,
                "estimativa: water cooler 240 (bomba + 2 fans) e fans do gabinete"),
        Parcela("periféricos USB", cenario.perifericos_usb_w, False,
                "estimativa — alimentados PELA fonte do PC"),
        _parcela_monitores(cenario),
    )

    dc = _somar(parcelas, na_fonte=True)
    fora = _somar(parcelas, na_fonte=False)

    # A eficiência é lida nos DOIS extremos da carga: com uma faixa larga (CPU sem
    # sensor), o mínimo e o máximo caem em pontos bem diferentes da curva, e usar um
    # só deles enviesaria uma das pontas.
    eff_min_carga = eficiencia_fonte(dc.minimo, cenario.fonte_capacidade_w, cenario.fonte_curva)
    eff_max_carga = eficiencia_fonte(dc.maximo, cenario.fonte_capacidade_w, cenario.fonte_curva)
    parede_a, parede_b = dc.minimo / eff_min_carga, dc.maximo / eff_max_carga
    # `sorted` em vez de assumir a ordem: dividir por eficiências diferentes PODERIA,
    # com uma curva esquisita, inverter as pontas — e `Faixa` levanta se isso ocorrer.
    # Ordenar aqui deixa o resultado correto em vez de estourar longe da causa.
    pc_parede = Faixa(*sorted((parede_a, parede_b)))

    avisos: list[str] = []
    if dc.maximo > cenario.fonte_capacidade_w:
        avisos.append(
            f"carga DC de pico ({dc.maximo:.0f} W) acima da capacidade da fonte "
            f"({cenario.fonte_capacidade_w:.0f} W) — a estimativa segue, mas o cenário não fecha")
    menor_ponto = min(x for x, _ in cenario.fonte_curva)
    if fracao_de_carga(dc.minimo, cenario.fonte_capacidade_w) < menor_ponto:
        avisos.append(
            f"carga DC mínima abaixo de {menor_ponto:.0%} da fonte — eficiência clampada "
            f"no menor ponto da curva (extrapolar ali seria inventar)")

    return Tomada(
        total=pc_parede + fora,
        pc_dc=dc,
        pc_parede=pc_parede,
        fora_da_fonte=fora,
        eficiencia=Faixa(*sorted((eff_min_carga, eff_max_carga))),
        fracao_de_carga=Faixa(*sorted((
            fracao_de_carga(dc.minimo, cenario.fonte_capacidade_w),
            fracao_de_carga(dc.maximo, cenario.fonte_capacidade_w),
        ))),
        parcelas=parcelas,
        avisos=tuple(avisos),
    )


def a_partir_de_energia(medida: dict, cenario: Cenario = CENARIO_PADRAO) -> Tomada:
    """Ponte para o dicionário que `energia.medir()` devolve. Pura: recebe o dict
    pronto, não importa `energia` (que abriria ctypes e SQLite dentro deste módulo).

    Existe para eliminar um erro de fiação com número plausível: o dict tem
    `gpu_watts`, `cpu_watts` E `watts_total` — passar o total já somado no lugar da
    GPU produziria uma conta errada que ninguém nota. Aqui os nomes ficam num só
    lugar. Campo ausente é `None` e cai na parcela modelada, como manda o contrato.

    `monitores_ligados` (2026-08-04) segue a MESMA regra dos watts: vem pronto no
    dict, e este módulo continua sem tocar em ctypes (quem lê o Windows é `tela.py`).
    Ausente ou None deixa o cenário como está — ou seja, a faixa larga de sempre —,
    então nenhum chamador antigo muda de resultado por causa deste campo."""
    monitores = medida.get("monitores_ligados")
    if monitores is not None:
        cenario = replace(cenario, monitores_ligados=bool(monitores))
    return estimar(
        gpu_watts=medida.get("gpu_watts"),
        cpu_watts=medida.get("cpu_watts"),
        cenario=cenario,
    )
