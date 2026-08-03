"""
WATTÍMETRO — quanta ENERGIA já foi gasta, e não quantos watts estão sendo gastos.

`potencia.py` responde "quanto está puxando AGORA". Isto responde "quanto já
consumiu hoje, e nos últimos 30 dias" — que é uma pergunta diferente e não se
resolve guardando watts: watt é taxa, e taxa não se soma. O que se soma é energia
(watt × tempo), e é isso que este módulo acumula.

⚠ A GPU tem um caminho MUITO melhor que amostrar watts, e é o que usamos: a NVML
expõe `nvmlDeviceGetTotalEnergyConsumption`, um contador MONOTÔNICO de milijoules
desde que o driver subiu. Com ele, a energia entre duas leituras é uma subtração
EXATA — não perde nada entre as amostras. Amostrar watts a cada 20 s numa placa
que salta de 96 W para 320 W erraria por quanto tempo cada pico durou; o contador
não erra, porque ele integrou por dentro do driver.

A CPU não tem esse luxo (o ajudante elevado publica watts INSTANTÂNEOS), então lá
a integração é por trapézio entre duas amostras. É uma aproximação, e ela está
marcada como tal na estrutura de retorno — quem lê o gráfico precisa saber que as
duas metades da barra têm qualidades diferentes.

⚠ AS TRÊS COISAS QUE NÃO PODEM VIRAR ENERGIA (e que um acumulador ingênuo soma):

1. **Contador reiniciado.** Driver recarregado, `nvidia-smi -r`, reboot: o
   acumulador volta a zero e a subtração daria um negativo. Rebaseia, não soma.
2. **Buraco.** O app fica fechado das 2h às 9h; na volta, a subtração do contador
   traz SETE HORAS de energia do dispositivo — que existiu de verdade, mas não foi
   medida por nós e não pode ser creditada a um intervalo de amostragem. Rebaseia.
3. **Amostra absurda.** Ruído de leitura vira um pico de MWh no gráfico do mês.

Por isso cada dia guarda também os SEGUNDOS efetivamente medidos: um dia com 2 h
de cobertura e um dia com 24 h não são comparáveis, e uma barra baixa por falta de
medição é indistinguível de uma barra baixa por economia — a não ser que a
cobertura viaje junto. É a mesma regra do `cpu_watts` que nunca vira `0 W`.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Iterator, Optional

from mente_digital.telemetry import telemetry

# Intervalo alvo entre amostras. Não é a régua do scheduler (que tica bem mais
# rápido): é o passo que o acumulador aceita, para não gravar no SQLite a cada
# passada do loop de alarmes.
INTERVALO_PADRAO_S = 20.0

# Acima disto, o vão entre duas amostras não é "amostragem", é BURACO — o app
# estava fechado, a máquina suspensa, ou o processo travado. Creditar a energia de
# um buraco ao intervalo seria inventar medição. 3× o intervalo dá folga para um
# tique atrasado por GPU ocupada sem confundir com ausência.
GAP_MAXIMO_S = 90.0

# Joule por hora-watt. 1 Wh = 3600 J.
_J_POR_WH = 3600.0

# Nenhuma parcela real passa disto num intervalo de amostragem: 5 kW por 90 s.
# É a mesma régua de sanidade do `_WATTS_ABSURDO` do potencia.py, em energia.
_JOULES_ABSURDO = 5000.0 * GAP_MAXIMO_S


@dataclass(frozen=True)
class Acumulo:
    """O que uma amostra acrescenta. Puro — nada aqui tocou disco ou relógio."""

    joules: Optional[float]                 # None = nada a somar (ver `motivo`)
    segundos: float                         # janela efetivamente medida
    baseline: Optional[tuple[float, float]]  # (valor, instante) para a próxima
    motivo: Optional[str] = None            # por que não somou, quando não somou


# --------------------------------------------------------------------------- #
# O núcleo PURO                                                                #
# --------------------------------------------------------------------------- #
def acumular_contador(
    baseline: Optional[tuple[float, float]],
    milijoules: float,
    agora: float,
    gap_maximo: float = GAP_MAXIMO_S,
) -> Acumulo:
    """Energia entre esta leitura do contador monotônico e a anterior.

    Espelha a estrutura de `potencia.media_integral` de propósito — é o mesmo
    contador, lido para outro fim —, mas com uma diferença que importa: aqui NÃO
    existe "janela curta demais". Duas leituras coladas somam pouca energia, e
    pouca energia é a resposta certa; descartá-las perderia o pedaço.
    """
    if baseline is None:
        return Acumulo(None, 0.0, (milijoules, agora), "primeira_amostra")

    valor_antes, t_antes = baseline
    if milijoules < valor_antes:
        # O contador zerou (driver recarregado / reboot). A energia do meio existiu,
        # mas é impossível saber quanta — e inventar seria pior que perder.
        return Acumulo(None, 0.0, (milijoules, agora), "contador_reiniciou")

    janela = agora - t_antes
    if janela <= 0:
        return Acumulo(None, 0.0, (milijoules, agora), "relogio_andou_para_tras")
    if janela > gap_maximo:
        return Acumulo(None, 0.0, (milijoules, agora), "buraco")

    joules = (milijoules - valor_antes) / 1000.0
    if joules > _JOULES_ABSURDO:
        return Acumulo(None, 0.0, (milijoules, agora), "absurdo")
    return Acumulo(joules, janela, (milijoules, agora))


def acumular_watts(
    baseline: Optional[tuple[float, float]],
    watts: Optional[float],
    agora: float,
    gap_maximo: float = GAP_MAXIMO_S,
) -> Acumulo:
    """Energia por TRAPÉZIO entre duas amostras de watt instantâneo (a CPU).

    Trapézio e não retângulo porque a carga varia continuamente entre as amostras:
    assumir o valor da ponta esquerda (ou da direita) enviesa o total inteiro na
    direção de quem começou, e num dia de 4.300 amostras o viés não se cancela.
    A média das duas pontas é o erro menor que dá para cometer sem amostrar mais.

    ⚠ Isto É uma aproximação, ao contrário da GPU. Quem exibe precisa dizer.
    """
    if watts is None:
        # Ausência não é zero: o ajudante caiu. Perde-se o baseline de propósito,
        # senão a próxima amostra fecharia um trapézio por cima do buraco.
        return Acumulo(None, 0.0, None, "sem_leitura")
    if watts < 0:
        return Acumulo(None, 0.0, None, "leitura_negativa")
    if baseline is None:
        return Acumulo(None, 0.0, (watts, agora), "primeira_amostra")

    w_antes, t_antes = baseline
    janela = agora - t_antes
    if janela <= 0:
        return Acumulo(None, 0.0, (watts, agora), "relogio_andou_para_tras")
    if janela > gap_maximo:
        return Acumulo(None, 0.0, (watts, agora), "buraco")

    joules = (w_antes + watts) / 2.0 * janela
    if joules > _JOULES_ABSURDO:
        return Acumulo(None, 0.0, (watts, agora), "absurdo")
    return Acumulo(joules, janela, (watts, agora))


def wh(joules: Optional[float]) -> Optional[float]:
    """Joule para watt-hora, a unidade da conta de luz. None propaga."""
    return None if joules is None else joules / _J_POR_WH


def cobertura(segundos_medidos: float, segundos_do_periodo: float) -> Optional[float]:
    """Fração do período que foi efetivamente medida (0..1), ou None se o período
    não faz sentido. É o número que impede ler "dia de pouco consumo" onde o certo
    é "dia mal medido"."""
    if segundos_do_periodo <= 0:
        return None
    return min(segundos_medidos / segundos_do_periodo, 1.0)


def segundos_no_dia(dia: date, agora: datetime) -> float:
    """Quanto tempo o dia JÁ teve. Hoje não tem 24 h ainda — usar 86400 para o dia
    corrente faria a cobertura de hoje parecer sempre ruim, e o dono concluiria que
    o medidor está furado quando ele só está no meio da tarde."""
    if dia > agora.date():
        return 0.0
    if dia < agora.date():
        return 86400.0
    return (agora - datetime.combine(dia, datetime.min.time())).total_seconds()


# --------------------------------------------------------------------------- #
# A parte suja: SQLite                                                         #
# --------------------------------------------------------------------------- #
class RegistroConsumo:
    """Acumula em RAM e descarrega no SQLite por DIA.

    Conexão própria (não `telemetry.Database`) pelo mesmo motivo do
    `registro_aparelhos`: é assunto fechado, e o WAL do arquivo já torna o acesso
    concorrente seguro sem coordenação extra.
    """

    def __init__(self, path: str, relogio: Optional[Callable[[], datetime]] = None,
                 monotonico: Optional[Callable[[], float]] = None) -> None:
        self.path = path
        self._agora = relogio or datetime.now
        # Relógio MONOTÔNICO para as janelas: `datetime.now` anda para trás no
        # horário de verão e na sincronização de NTP, e uma janela negativa viraria
        # energia negativa. O relógio de parede só decide a QUAL DIA creditar.
        self._mono = monotonico or __import__("time").monotonic
        self._trava = threading.Lock()
        self._base_gpu: Optional[tuple[float, float]] = None
        self._base_cpu: Optional[tuple[float, float]] = None
        self._base_p_min: Optional[tuple[float, float]] = None
        self._base_p_max: Optional[tuple[float, float]] = None
        self._ultimo_registro: float = 0.0
        self._descartes: dict[str, int] = {}

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        """Idempotente, como todas as migrações do projeto.

        ⚠ As colunas da PAREDE ficam separadas das de GPU/CPU de propósito, e não
        fundidas num total só: as duas metades têm QUALIDADES diferentes. GPU e CPU
        são medidas (a GPU, exatamente); a parede é um MODELO (fonte, monitores,
        placa-mãe, bombas — ver `tomada.py`), e vem como FAIXA porque a incerteza é
        real. Somar tudo numa coluna transformaria o fato em estimativa e apagaria
        a diferença justamente no lugar onde ela importa: a conta de luz.
        """
        try:
            with self._conn() as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS consumo_diario
                       (dia TEXT PRIMARY KEY, gpu_j REAL DEFAULT 0, cpu_j REAL DEFAULT 0,
                        segundos REAL DEFAULT 0, amostras INTEGER DEFAULT 0,
                        parede_min_j REAL DEFAULT 0, parede_max_j REAL DEFAULT 0)"""
                )
                # Banco criado antes da parede existir: acrescenta as colunas sem
                # perder o histórico já acumulado.
                existentes = {r[1] for r in conn.execute("PRAGMA table_info(consumo_diario)")}
                for coluna in ("parede_min_j", "parede_max_j"):
                    if coluna not in existentes:
                        conn.execute(
                            f"ALTER TABLE consumo_diario ADD COLUMN {coluna} REAL DEFAULT 0")
        except Exception as exc:
            telemetry.error("CONSUMO", "Erro ao criar a tabela de consumo", exc)

    # --- Escrita ------------------------------------------------------------
    def devido(self, intervalo: float = INTERVALO_PADRAO_S) -> bool:
        """Já passou tempo suficiente desde a última amostra? O scheduler tica bem
        mais rápido que isto, e gravar a cada tique seria uma escrita por segundo
        num banco que também serve o histórico de chat."""
        return (self._mono() - self._ultimo_registro) >= intervalo

    def registrar(self, milijoules_gpu: Optional[float], watts_cpu: Optional[float],
                  parede_min_w: Optional[float] = None,
                  parede_max_w: Optional[float] = None) -> dict:
        """Uma amostra. Devolve o que foi somado (para log/teste), nunca levanta.

        As parcelas são independentes de propósito: o ajudante da CPU pode cair e
        voltar sem afetar a contabilidade da GPU, que é a exata. A da parede é
        MODELO (o chamador a calcula com `tomada.py`) e vem como faixa — este
        módulo integra o que recebe e não opina sobre o modelo.
        """
        agora_mono = self._mono()
        dia = self._agora().date().isoformat()
        with self._trava:
            gpu = (acumular_contador(self._base_gpu, milijoules_gpu, agora_mono)
                   if milijoules_gpu is not None
                   else Acumulo(None, 0.0, None, "sem_leitura"))
            self._base_gpu = gpu.baseline
            cpu = acumular_watts(self._base_cpu, watts_cpu, agora_mono)
            self._base_cpu = cpu.baseline
            p_min = acumular_watts(self._base_p_min, parede_min_w, agora_mono)
            self._base_p_min = p_min.baseline
            p_max = acumular_watts(self._base_p_max, parede_max_w, agora_mono)
            self._base_p_max = p_max.baseline
            self._ultimo_registro = agora_mono
            for m in (gpu.motivo, cpu.motivo):
                if m:
                    self._descartes[m] = self._descartes.get(m, 0) + 1

        gpu_j, cpu_j = gpu.joules or 0.0, cpu.joules or 0.0
        pmin_j, pmax_j = p_min.joules or 0.0, p_max.joules or 0.0
        # Os segundos medidos são os da GPU quando ela contou, senão os da CPU: é a
        # cobertura do MEDIDOR, e ela existe se qualquer uma das parcelas mediu. A
        # parede NÃO conta cobertura — ela é derivada, não medição.
        segundos = gpu.segundos or cpu.segundos
        if gpu_j or cpu_j or pmin_j:
            self._somar(dia, gpu_j, cpu_j, segundos, pmin_j, pmax_j)
        return {"dia": dia, "gpu_j": gpu_j, "cpu_j": cpu_j, "segundos": segundos,
                "parede_min_j": pmin_j, "parede_max_j": pmax_j,
                "gpu_motivo": gpu.motivo, "cpu_motivo": cpu.motivo}

    def _somar(self, dia: str, gpu_j: float, cpu_j: float, segundos: float,
               pmin_j: float = 0.0, pmax_j: float = 0.0) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO consumo_diario
                         (dia, gpu_j, cpu_j, segundos, amostras, parede_min_j, parede_max_j)
                       VALUES (?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(dia) DO UPDATE SET
                         gpu_j = gpu_j + excluded.gpu_j,
                         cpu_j = cpu_j + excluded.cpu_j,
                         segundos = segundos + excluded.segundos,
                         parede_min_j = parede_min_j + excluded.parede_min_j,
                         parede_max_j = parede_max_j + excluded.parede_max_j,
                         amostras = amostras + 1""",
                    (dia, gpu_j, cpu_j, segundos, pmin_j, pmax_j),
                )
        except Exception as exc:
            telemetry.error("CONSUMO", f"Erro ao somar o consumo de {dia}", exc)

    # --- Leitura ------------------------------------------------------------
    def diario(self, dias: int = 30) -> list[dict]:
        """Os últimos N dias, do mais antigo para o mais novo, **sem buracos**: dia
        sem medição entra com zero e cobertura zero, senão o gráfico encosta duas
        barras distantes e inventa uma continuidade que não houve."""
        agora = self._agora()
        hoje = agora.date()
        primeiro = date.fromordinal(hoje.toordinal() - dias + 1).isoformat()
        try:
            with self._conn() as conn:
                linhas = {
                    r[0]: r for r in conn.execute(
                        "SELECT dia, gpu_j, cpu_j, segundos, amostras, parede_min_j, "
                        "parede_max_j FROM consumo_diario WHERE dia >= ? ORDER BY dia",
                        (primeiro,),
                    ).fetchall()
                }
        except Exception as exc:
            telemetry.error("CONSUMO", "Erro ao ler o consumo diário", exc)
            return []

        saida = []
        for n in range(dias - 1, -1, -1):
            d = date.fromordinal(hoje.toordinal() - n)
            chave = d.isoformat()
            r = linhas.get(chave)
            gpu_j, cpu_j, seg, amostras, pmin_j, pmax_j = (
                (r[1], r[2], r[3], r[4], r[5] or 0.0, r[6] or 0.0) if r
                else (0.0, 0.0, 0.0, 0, 0.0, 0.0))
            saida.append({
                "dia": chave,
                "gpu_wh": round(wh(gpu_j) or 0.0, 2),
                "cpu_wh": round(wh(cpu_j) or 0.0, 2),
                # `medido_wh` e não `total_wh`: o nome antigo dava a entender que
                # era tudo que o PC gastou, e não é — é só o que dá para MEDIR.
                "medido_wh": round((wh(gpu_j) or 0.0) + (wh(cpu_j) or 0.0), 2),
                "parede_min_wh": round(wh(pmin_j) or 0.0, 2),
                "parede_max_wh": round(wh(pmax_j) or 0.0, 2),
                "segundos": round(seg),
                "amostras": amostras,
                # 4 casas, não 3: com 3, uma cobertura de 0,046% arredonda para
                # 0.0 e vira indistinguível de "não mediu nada" — exatamente a
                # confusão que este campo existe para desfazer.
                "cobertura": round(cobertura(seg, segundos_no_dia(d, agora)) or 0.0, 4),
            })
        return saida

    def mensal(self, meses: int = 12) -> list[dict]:
        """Agrega por mês. Sai do SQL (não somando o diário em Python) porque o
        histórico pode ser maior que a janela do gráfico de 30 dias."""
        try:
            with self._conn() as conn:
                linhas = conn.execute(
                    """SELECT substr(dia, 1, 7) AS mes, SUM(gpu_j), SUM(cpu_j),
                              SUM(segundos), SUM(amostras), COUNT(*),
                              SUM(parede_min_j), SUM(parede_max_j)
                       FROM consumo_diario GROUP BY mes ORDER BY mes DESC LIMIT ?""",
                    (meses,),
                ).fetchall()
        except Exception as exc:
            telemetry.error("CONSUMO", "Erro ao ler o consumo mensal", exc)
            return []
        return [{
            "mes": m,
            "gpu_wh": round(wh(gj) or 0.0, 2),
            "cpu_wh": round(wh(cj) or 0.0, 2),
            "medido_wh": round((wh(gj) or 0.0) + (wh(cj) or 0.0), 2),
            "parede_min_wh": round(wh(pmin or 0.0) or 0.0, 2),
            "parede_max_wh": round(wh(pmax or 0.0) or 0.0, 2),
            "segundos": round(seg),
            "amostras": am,
            "dias_com_medicao": ndias,
        } for m, gj, cj, seg, am, ndias, pmin, pmax in reversed(linhas)]

    def descartes(self) -> dict[str, int]:
        """Quantas amostras foram jogadas fora e por quê. É o que responde "por que
        a cobertura de ontem foi 40%?" sem reencenar a noite."""
        with self._trava:
            return dict(self._descartes)
