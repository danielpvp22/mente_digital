"""
Leitor de agenda .ics 100% LOCAL (#40, Onda 3).

Parser mínimo e PURO/testável, SEM dependência nova (mesma disciplina do agenda.py): lê
VEVENTs com SUMMARY + DTSTART e devolve (início, título). Cobre o pontual — evento
RECORRENTE (RRULE) é ignorado por ora (a maioria dos compromissos avulsos não usa; expandir
RRULE certo é trabalhoso e fica pra quando houver necessidade real). Nada vai para nuvem: o
usuário exporta o .ics do Google/Outlook e larga na pasta local; o app só LÊ o arquivo.
"""
from __future__ import annotations

import glob
import os
from datetime import date, datetime
from typing import List, Optional, Tuple

Evento = Tuple[datetime, str]


def _desdobrar(texto: str) -> List[str]:
    """Junta linhas continuadas: no RFC 5545, uma linha que começa por espaço/tab é a
    continuação da anterior (o .ics quebra linhas longas assim)."""
    linhas: List[str] = []
    for linha in texto.splitlines():
        if linha[:1] in (" ", "\t") and linhas:
            linhas[-1] += linha[1:]
        else:
            linhas.append(linha)
    return linhas


def _parse_dtstart(valor: str) -> Optional[datetime]:
    """Valor do DTSTART (já sem nome/params). Formatos: YYYYMMDDThhmmss[Z] ou YYYYMMDD
    (dia inteiro). Fuso é ignorado (o 'Z'/TZID é descartado) — suficiente para 'hoje'."""
    v = valor.strip().rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def parse_eventos(texto: str) -> List[Evento]:
    """(início, título) de cada VEVENT NÃO-recorrente, ordenados por início. Puro."""
    eventos: List[Evento] = []
    dentro = False
    inicio: Optional[datetime] = None
    titulo: Optional[str] = None
    tem_rrule = False
    for linha in _desdobrar(texto):
        cima = linha.upper()
        if cima.startswith("BEGIN:VEVENT"):
            dentro, inicio, titulo, tem_rrule = True, None, None, False
        elif cima.startswith("END:VEVENT"):
            if dentro and inicio and titulo and not tem_rrule:
                eventos.append((inicio, titulo))
            dentro = False
        elif dentro and ":" in linha:
            chave = linha.split(":", 1)[0].split(";", 1)[0].upper()
            valor = linha.split(":", 1)[1]
            if chave == "SUMMARY":
                titulo = valor.strip() or None
            elif chave == "DTSTART":
                inicio = _parse_dtstart(valor)
            elif chave == "RRULE":
                tem_rrule = True
    eventos.sort(key=lambda e: e[0])
    return eventos


def eventos_do_dia(texto: str, dia: date) -> List[Evento]:
    """Eventos cujo início cai em `dia` (injetado — puro/testável)."""
    return [(dt, t) for dt, t in parse_eventos(texto) if dt.date() == dia]


def ler_pasta(pasta: str, dia: date) -> List[Evento]:
    """I/O: lê todos os .ics de `pasta` e devolve os eventos de `dia`, ordenados. Um
    arquivo ruim não derruba os outros (o chamador roda isto em to_thread)."""
    eventos: List[Evento] = []
    for caminho in sorted(glob.glob(os.path.join(pasta, "*.ics"))):
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                eventos.extend(eventos_do_dia(f.read(), dia))
        except Exception:
            continue
    eventos.sort(key=lambda e: e[0])
    return eventos
