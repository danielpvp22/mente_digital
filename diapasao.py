"""Diapasão (#36) — perfil de COMO o dono prefere conversar. Puro/testável.

POR QUE existe (e o que NÃO é): não é para IMITAR o tom do usuário — é para
identificar COMO ele gosta de ser RESPONDIDO e adaptar a resposta a isso. Ex.: quem
sempre faz perguntas curtas e diretas quer resposta direta, sem preâmbulo; quem
pede exemplos quer analogias. No idle, o LLM destila esse perfil da conversa; aqui
ficam só as funções puras (parse do perfil + a instrução que ele vira), para o
teste não precisar de GPU.

O perfil é uma DIRETRIZ curta de estilo, nunca conteúdo. Se o LLM não vê preferência
clara, responde 'NADA' e nada é gravado (não inventa um perfil do nada).
"""
from __future__ import annotations

from typing import Optional

import textutils

_LIMITE_PERFIL = 400  # o perfil é uma diretriz curta; corta divagação do LLM


def parse_perfil(resposta: str) -> Optional[str]:
    """Normaliza a saída do LLM num perfil curto, ou None se não há preferência
    clara ('NADA'). Puro. Não confia no LLM para o formato: capa o tamanho."""
    if not resposta:
        return None
    texto = resposta.strip()
    if not texto or textutils.normaliza(texto).strip(".!? ") == "nada":
        return None
    return texto[:_LIMITE_PERFIL].strip()


def instrucao_do_perfil(perfil: Optional[str]) -> str:
    """Transforma o perfil guardado na instrução que entra no prompt de resposta.
    Vazio -> string vazia (não injeta nada). Puro."""
    if not perfil or not perfil.strip():
        return ""
    return (
        "PREFERÊNCIAS DE ESTILO do usuário (adapte COMO você responde a isto; "
        f"não imite o tom dele, não mude o conteúdo): {perfil.strip()}"
    )
