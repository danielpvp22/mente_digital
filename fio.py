"""Fio da Conversa (#35) — retomar um assunto anterior. Puro/testável.

POR QUE existe: conversas ficam pela metade. "Fio da conversa" resgata, EM MOMENTO
OPORTUNO (aqui: quando o usuário pede — 'onde paramos?'), o assunto de uma conversa
anterior para continuar de onde parou. Depende do #34 (a Malha): entre vários fios
possíveis, prefere o que é um FIO DE VERDADE (uma troca com substância), não um
"oi" solto.

`escolher_fio` é puro: recebe a lista de conversas (mais recentes primeiro) e o id
da conversa ATUAL, e devolve o melhor fio anterior — ou None se não há um.
"""
from __future__ import annotations

from typing import List, Optional


def escolher_fio(
    conversas: List[dict], cid_atual: Optional[str], min_turnos: int
) -> Optional[dict]:
    """O fio anterior a retomar: a conversa MAIS RECENTE que (a) não é a atual e
    (b) teve pelo menos `min_turnos` trocas (um assunto real, não um 'oi' solto).
    `conversas` vem ordenada do mais recente para o mais antigo. Puro."""
    for conv in conversas:
        if cid_atual is not None and conv.get("id") == cid_atual:
            continue                              # não "retoma" a conversa em curso
        if int(conv.get("n", 0) or 0) < max(1, min_turnos):
            continue                              # curto demais para ser um fio
        return conv
    return None
