"""
Parsing de tempo em PT-BR e recorrência — puro, testável e SEM dependência nova.

Por que um parser próprio em vez de `dateparser`: o resto do projeto é 100% local e
a suíte roda sem rede/GPU sobre FUNÇÕES PURAS (ver tests/). Um regex determinístico,
com o instante de referência INJETADO (`agora`, como `normalizar_atomo`), é testável
sem depender do relógio real nem de uma lib pesada que muda de comportamento entre
versões. O conjunto suportado é explícito e conservador: o que não casa devolve None,
e a ferramenta pede pro usuário reformular — melhor que agendar no horário errado.

Vocabulário coberto (o suficiente para lembretes/alarmes/timers de voz):
- Relativo:   "daqui a 10 minutos", "em 2 horas", "em 30 segundos", "daqui 1h30"
- Absoluto:   "amanhã às 8h", "hoje 18h30", "às 15h", "meio-dia", "meia-noite"
- Recorrente: "todo dia às 7h", "toda segunda às 9h", "toda semana", "a cada 30 minutos"
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

import textutils

# Recorrência é serializada como string simples na coluna `recorrencia`:
#   None                -> disparo único
#   "diario"            -> todo dia no mesmo horário
#   "semanal:<0-6>"     -> toda semana num dia (0=segunda ... 6=domingo, weekday())
#   "intervalo:<segs>"  -> a cada N segundos (usado por "a cada 30 min" e watchers)
Recorrencia = Optional[str]

_DIAS_SEMANA = {
    "segunda": 0, "segundas": 0, "segunda-feira": 0,
    "terca": 1, "tercas": 1, "terca-feira": 1,
    "quarta": 2, "quartas": 2, "quarta-feira": 2,
    "quinta": 3, "quintas": 3, "quinta-feira": 3,
    "sexta": 4, "sextas": 4, "sexta-feira": 4,
    "sabado": 5, "sabados": 5,
    "domingo": 6, "domingos": 6,
}

_UNIDADES = {
    "segundo": 1, "segundos": 1, "seg": 1, "segs": 1, "s": 1,
    "minuto": 60, "minutos": 60, "min": 60, "mins": 60, "m": 60,
    "hora": 3600, "horas": 3600, "h": 3600, "hr": 3600, "hrs": 3600,
    "dia": 86400, "dias": 86400,
}


def _extrai_hora(norm: str, default_hm: Optional[Tuple[int, int]] = None) -> Optional[Tuple[int, int]]:
    """Acha um horário na frase normalizada. Devolve (hora, minuto) ou o default.

    Aceita '8h', '8h30', '18:30', 'as 15', 'meio-dia', 'meia-noite'. Conservador:
    número solto SEM 'h'/':' não vira horário (senão 'em 2 horas' viraria 02:00)."""
    if "meio-dia" in norm or "meio dia" in norm:
        return (12, 0)
    if "meia-noite" in norm or "meia noite" in norm:
        return (0, 0)
    # 18:30 / 18h30 / 8h / 8 h
    m = re.search(r"\b(\d{1,2})\s*(?:h|:)\s*(\d{2})?\b", norm)
    if m:
        hora = int(m.group(1))
        minuto = int(m.group(2)) if m.group(2) else 0
        if 0 <= hora <= 23 and 0 <= minuto <= 59:
            return (hora, minuto)
    # "as 15" / "as 9" (sem o 'h') — só depois de 'as', para não pegar número qualquer.
    m = re.search(r"\bas?\s+(\d{1,2})\b", norm)
    if m:
        hora = int(m.group(1))
        if 0 <= hora <= 23:
            return (hora, 0)
    return default_hm


def _delta_relativo(norm: str) -> Optional[timedelta]:
    """Soma todas as durações do tipo 'daqui a 1 hora e 30 minutos' / 'em 90 min'.

    Só dispara quando há um gatilho relativo ('daqui', 'em', 'apos', 'depois de') para
    não confundir com horário absoluto. Aceita várias unidades na mesma frase."""
    if not re.search(r"\b(daqui|em|apos|depois de|dentro de)\b", norm):
        return None
    total = 0
    achou = False
    # "daqui 1h30" — hora colada no minuto. Consome PRIMEIRO e remove do texto, senão
    # o '1h' seria contado de novo no laço de unidades abaixo (dobrando a hora).
    m = re.search(r"\b(\d{1,2})h(\d{2})\b", norm)
    if m:
        total += int(m.group(1)) * 3600 + int(m.group(2)) * 60
        achou = True
        norm = norm[: m.start()] + " " + norm[m.end():]
    for qtd, unidade in re.findall(r"(\d+)\s*([a-z]+)", norm):
        if unidade in _UNIDADES:
            total += int(qtd) * _UNIDADES[unidade]
            achou = True
    return timedelta(seconds=total) if achou and total > 0 else None


def _intervalo_recorrente(norm: str) -> Optional[int]:
    """'a cada 30 minutos' / 'de 2 em 2 horas' -> segundos do intervalo, senão None."""
    m = re.search(r"a cada\s+(\d+)\s*([a-z]+)", norm)
    if not m:
        m = re.search(r"de\s+(\d+)\s+em\s+\d+\s*([a-z]+)", norm)
    if m and m.group(2) in _UNIDADES:
        segs = int(m.group(1)) * _UNIDADES[m.group(2)]
        return segs if segs > 0 else None
    # "a cada hora" / "a cada dia" (sem número = 1)
    m = re.search(r"a cada\s+([a-z]+)", norm)
    if m and m.group(1) in _UNIDADES:
        return _UNIDADES[m.group(1)]
    return None


def proximo_no_horario(agora: datetime, hora: int, minuto: int, adiar_se_passou: bool = True) -> datetime:
    """Hoje no horário (hora:minuto); se já passou e `adiar_se_passou`, amanhã."""
    alvo = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if adiar_se_passou and alvo <= agora:
        alvo += timedelta(days=1)
    return alvo


def proximo_dia_semana(agora: datetime, weekday: int, hora: int, minuto: int) -> datetime:
    """Próxima ocorrência de um dia da semana (0=seg..6=dom) no horário dado.

    Se for hoje e o horário ainda não passou, é hoje; senão, na próxima semana."""
    alvo = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    dias = (weekday - agora.weekday()) % 7
    if dias == 0 and alvo <= agora:
        dias = 7
    return alvo + timedelta(days=dias)


def proximo_disparo(atual: datetime, recorrencia: str) -> Optional[datetime]:
    """Dado um disparo que ACABOU de ocorrer, calcula o próximo para a recorrência.

    Puro. None se a recorrência é desconhecida (o chamador conclui o agendamento)."""
    if recorrencia == "diario":
        return atual + timedelta(days=1)
    if recorrencia.startswith("semanal:"):
        return atual + timedelta(days=7)
    if recorrencia.startswith("intervalo:"):
        try:
            segs = int(recorrencia.split(":", 1)[1])
        except ValueError:
            return None
        return atual + timedelta(seconds=segs) if segs > 0 else None
    return None


def parse_quando(texto: str, agora: datetime) -> Tuple[Optional[datetime], Recorrencia]:
    """Interpreta 'quando' em linguagem natural -> (primeiro_disparo, recorrencia).

    Puro/testável (o instante de referência é injetado). Devolve (None, None) quando
    não reconhece — a ferramenta então pede pro usuário reformular, em vez de agendar
    num horário adivinhado (falso positivo aqui é pior que pedir de novo).

    Ordem das regras importa: recorrência > relativo > absoluto. 'a cada 30 min' é
    recorrente mesmo tendo cara de relativo; 'toda segunda' é recorrente mesmo com
    horário absoluto embutido.
    """
    if not texto or not texto.strip():
        return (None, None)
    norm = textutils.normaliza(texto)

    # 1) RECORRENTE por intervalo: "a cada 30 minutos", "de 2 em 2 horas".
    segs = _intervalo_recorrente(norm)
    if segs is not None:
        return (agora + timedelta(seconds=segs), f"intervalo:{segs}")

    # 2) RECORRENTE por dia da semana: "toda segunda [às 9h]".
    if re.search(r"\btod[oa]s?\b", norm):
        hm = _extrai_hora(norm, default_hm=(8, 0))  # sem horário: assume 08:00
        for nome, wd in _DIAS_SEMANA.items():
            if re.search(rf"\b{re.escape(nome)}\b", norm):
                primeiro = proximo_dia_semana(agora, wd, hm[0], hm[1])
                return (primeiro, f"semanal:{wd}")
        # "todo dia às 7h" / "toda semana"
        if "semana" in norm:
            primeiro = proximo_no_horario(agora, hm[0], hm[1])
            return (primeiro, "intervalo:604800")  # 7 dias
        if "dia" in norm or "manha" in norm or hm != (8, 0):
            primeiro = proximo_no_horario(agora, hm[0], hm[1])
            return (primeiro, "diario")

    # 3) RELATIVO: "daqui a 10 minutos", "em 2 horas".
    delta = _delta_relativo(norm)
    if delta is not None:
        return (agora + delta, None)

    # 4) ABSOLUTO: "amanhã às 8h", "hoje 18h", "às 15h30", "meio-dia".
    hm = _extrai_hora(norm)
    if hm is not None:
        if "amanha" in norm or "amanhã" in norm:
            base = (agora + timedelta(days=1)).replace(
                hour=hm[0], minute=hm[1], second=0, microsecond=0
            )
            return (base, None)
        # "hoje" ou só o horário: hoje se ainda não passou, senão amanhã.
        return (proximo_no_horario(agora, hm[0], hm[1]), None)

    return (None, None)
