"""
Governador de verbosidade (#7): "Uma Frase Basta".

Num sistema de VOZ, cada palavra falada é latência e ruído — a pior UX é o assistente
que discursa quando o usuário só queria o dado. Aqui a resposta nasce no tamanho que a
PERGUNTA pede: pergunta factual curta ("que horas são", "quanto é 3x7", "qual a capital
da França") → UMA frase; pedido de explicação ("como funciona", "me explica", "por quê")
→ resposta normal.

Puro/testável: classifica só por léxico, sem LLM, sem custo no TTFA. Governa `max_tokens`
(quanto a GPU decodifica) e injeta uma instrução de brevidade no system prompt.
"""
from __future__ import annotations

from dataclasses import dataclass

import textutils
from config import settings


@dataclass(frozen=True)
class Nivel:
    nome: str
    max_tokens: int
    instrucao: str   # anexada ao system prompt da resposta (vazia = sem mudança)


# Pistas de que o usuário QUER profundidade — respostas destas nunca são cortadas a 1 frase.
_EXPLICA = (
    "explica", "explique", "explicar", "detalha", "detalhe", "detalhar", "discorra",
    "aprofunda", "aprofunde", "como funciona", "por que", "porque", "por quê", "pq ",
    "me ensina", "ensina", "fale mais", "mais detalhes", "elabore", "passo a passo",
    "descreve", "descreva", "compara", "compare", "diferenca entre", "resuma", "resumo",
)


def classificar(pergunta: str) -> Nivel:
    """Decide a verbosidade da resposta a partir da pergunta. Puro/testável."""
    n = textutils.normaliza(pergunta)
    if any(p in n for p in _EXPLICA):
        return Nivel("detalhado", settings.max_tokens_resposta, "")
    # Pergunta curta e direta = resposta curta e direta (o ganho de latência do #7).
    if len(textutils.tokens(pergunta)) <= settings.verbosidade_curto_max_palavras:
        return Nivel(
            "curto",
            settings.max_tokens_resposta_curto,
            "IMPORTANTE: responda em NO MÁXIMO UMA frase curta e direta. Só o essencial.",
        )
    return Nivel("normal", settings.max_tokens_resposta, "")
