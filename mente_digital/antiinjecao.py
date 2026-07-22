"""Anti-injeção de prompt no conteúdo web (#26). Puro/testável, sem IO.

POR QUE existe: o deep-fetch abre o CORPO de páginas da web e passa os trechos ao
LLM como contexto. Conteúdo web é DADO NÃO-CONFIÁVEL — uma página pode conter
"ignore as instruções anteriores, você agora é..." tentando sequestrar o modelo.
A defesa é filtrar os trechos SUSPEITOS antes de eles entrarem no contexto:
DROPA o trecho envenenado e segue com os limpos ("dropar/refazer"). Se a página
inteira era injeção, o chamador cai para os snippets.

FALSO-POSITIVO: um artigo QUE EXPLICA prompt injection cita essas frases. Por isso
(a) as assinaturas são IMPERATIVOS de override específicos (não a palavra
"injection"), e (b) dropar um trecho é barato — há outros —, então erramos para o
lado seguro sem calar a resposta inteira.
"""
from __future__ import annotations

from typing import List, Tuple

from mente_digital import textutils

# Assinaturas de injeção (comparadas sobre texto NORMALIZADO: minúsculo, sem acento).
# Cada uma é um comando de OVERRIDE — coisa que texto informativo raramente traz
# literal, mas que um payload de sequestro traz.
_ASSINATURAS = (
    # inglês
    "ignore previous instruction", "ignore all previous", "ignore the above",
    "ignore your instruction", "ignore your previous", "disregard previous instruction",
    "disregard all previous", "disregard the above", "forget previous instruction",
    "forget all previous", "new instructions:", "system prompt:", "reveal your prompt",
    "reveal your instruction", "reveal your system", "print your instruction",
    "you are now dan", "do anything now", "developer mode enabled",
    "override your", "you must ignore",
    # português
    "ignore as instrucoes anterior", "ignore todas as instrucoes",
    "ignore suas instrucoes", "desconsidere as instrucoes", "esqueca as instrucoes",
    "esqueca tudo que", "novas instrucoes:", "revele seu prompt",
    "revele suas instrucoes", "mostre seu prompt", "voce agora e um",
    "voce agora e uma", "a partir de agora voce", "ignore o texto acima",
)


def parece_injecao(texto: str) -> bool:
    """True se o trecho contém uma assinatura de injeção de prompt. Puro."""
    if not texto:
        return False
    n = textutils.normaliza(texto)
    return any(sig in n for sig in _ASSINATURAS)


def filtrar_chunks(chunks: List[str]) -> Tuple[List[str], int]:
    """Devolve (trechos_limpos, n_removidos). Dropa os que parecem injeção; os
    demais seguem para o ranking. Puro/testável."""
    limpos = [c for c in chunks if not parece_injecao(c)]
    return limpos, len(chunks) - len(limpos)
