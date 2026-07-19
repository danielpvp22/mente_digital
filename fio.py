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

import textutils


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


def casar_conversa(conversas: List[dict], tema: str) -> Optional[dict]:
    """Navegação por voz (#14): acha a conversa cujo TÍTULO melhor casa o `tema` falado
    (maior sobreposição de keywords; empate -> a mais recente, pois `conversas` já vem
    ordenada). None se nenhuma casa. Puro."""
    chaves = set(textutils.palavras_chave(tema or ""))
    if not chaves:
        return None
    melhor, melhor_score = None, 0
    for conv in conversas:
        titulo_chaves = set(textutils.palavras_chave(str(conv.get("titulo", ""))))
        score = len(chaves & titulo_chaves)
        if score > melhor_score:               # '>' estrito: mantém a 1ª (mais recente)
            melhor, melhor_score = conv, score
    return melhor
