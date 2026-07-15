"""
Fakes e fixtures compartilhados.

Objetivo: testar a LÓGICA do pipeline sem GPU, sem modelos e sem rede. Os fakes
abaixo substituem o LLM (stream de tokens controlado), o TTS (no-op) e o vector
store (resultados fixos), então cada teste exercita só a decisão do código.
"""
from __future__ import annotations

from typing import List, Optional, Tuple


# ==========================================================================
# LLM falso — emite uma sequência de tokens pré-definida
# ==========================================================================
class FakeLlama:
    """Substitui LlamaManager: `stream` devolve os tokens dados, na ordem."""

    def __init__(self, tokens: List[str]) -> None:
        self.tokens = tokens

    async def stream(self, prompt: str, **kwargs):
        for tok in self.tokens:
            yield tok

    async def collect(self, prompt: str, **kwargs) -> str:
        return "".join(self.tokens)


# ==========================================================================
# TTS falso — não sintetiza nada (retorna None => o _falar pula o áudio)
# ==========================================================================
class FakeTts:
    def __init__(self) -> None:
        self.chamadas: List[str] = []

    async def synth_base64(self, texto: str) -> Optional[str]:
        self.chamadas.append(texto)
        return None


# ==========================================================================
# Documento / store falsos para a busca local (sem ChromaDB)
# ==========================================================================
class FakeDoc:
    def __init__(self, content: str, metadata: Optional[dict] = None) -> None:
        self.page_content = content
        self.metadata = metadata or {}


class FakeStore:
    """Devolve pares (doc, score) fixos, imitando ChromaDB."""

    def __init__(self, results: List[Tuple[FakeDoc, float]]) -> None:
        self._results = results

    def similarity_search_with_score(self, termos: str, k: int):
        return self._results[:k]


# ==========================================================================
# Coletor de mensagens enviadas (o callback `send` do pipeline)
# ==========================================================================
def make_send():
    """Devolve (send, enviados): `send` é o callback async, `enviados` a lista."""
    enviados: List[dict] = []

    async def send(data: dict) -> bool:
        enviados.append(data)
        return True

    return send, enviados


def textos_de_tokens(enviados: List[dict]) -> str:
    """Concatena o texto de todas as mensagens do tipo 'token'."""
    return "".join(m["texto"] for m in enviados if m.get("tipo") == "token")
