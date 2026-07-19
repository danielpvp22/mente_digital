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

# EXPLIQUE-COMO-PARA-CRIANÇA (#45): pedido de SIMPLICIDADE, não de tamanho. É um eixo
# ORTOGONAL ao curto/detalhado (o usuário quer ANALOGIA e zero jargão), então tem
# Nível próprio e vence os outros — inclusive um "explica X" que pediria detalhado, e
# um "o que é X, simples" curtinho que seria cortado a 1 frase (simplificar precisa de
# espaço p/ a analogia). Já normalizado (sem acento): "criança"->"crianca".
_CRIANCA = (
    "para uma crianca", "pra uma crianca", "para crianca", "pra crianca",
    "como uma crianca", "como para uma crianca", "como se eu fosse crianca",
    "como se eu fosse uma crianca", "como se eu tivesse 5 anos",
    "como se eu tivesse cinco anos", "modo crianca", "explica como crianca",
    "para leigos", "para um leigo", "como se eu fosse leigo", "para leigo",
    "linguagem simples", "de forma bem simples", "de um jeito simples",
    "de um jeito bem simples", "bem simples", "de forma simples", "explica simples",
    "facil de entender", "eli5",
)


# TUTOR SOCRÁTICO (#44): modo de SESSÃO (ativado por "mestre, modo tutor"). Em vez de dar
# a resposta pronta, o assistente faz perguntas guiadas para o usuário raciocinar. Precisa
# de diálogo sustentado (por isso é modo, não por-pergunta), e de ESPAÇO (tokens normais),
# sobrepondo o corte de 1 frase do #7. Reusa a mesma fiação instrucao->LLM (local E web).
_TUTOR_INSTRUCAO = (
    "IMPORTANTE: você está em MODO TUTOR SOCRÁTICO. Em vez de entregar a resposta pronta, "
    "faça UMA pergunta guiada que leve o usuário a raciocinar e chegar à resposta sozinho. "
    "Se ele já respondeu algo, reaja (elogie o acerto, corrija com gentileza) e avance com a "
    "próxima pergunta. Seja breve e encorajador; só dê a resposta direta se ele pedir ou travar."
)


def aplicar_tutor(nivel: Nivel, ativo: bool) -> Nivel:
    """Se o modo tutor está ativo, sobrepõe o nível com a instrução socrática e tokens
    normais (espaço para a pergunta guiada). Ortogonal ao resto; puro/testável."""
    if not ativo:
        return nivel
    return Nivel("tutor", settings.max_tokens_resposta, _TUTOR_INSTRUCAO)


def classificar(pergunta: str) -> Nivel:
    """Decide a verbosidade da resposta a partir da pergunta. Puro/testável."""
    n = textutils.normaliza(pergunta)
    # ELI5 (#45) primeiro: é sobre ESTILO (simples, com analogia) e vence tamanho/detalhe.
    if any(p in n for p in _CRIANCA):
        return Nivel(
            "crianca",
            settings.max_tokens_resposta,
            "IMPORTANTE: explique de forma MUITO simples, como para uma criança de 5 anos. "
            "Use uma analogia do dia a dia, evite jargão e termos técnicos, e prefira "
            "frases curtas. Não use listas nem fórmulas.",
        )
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
