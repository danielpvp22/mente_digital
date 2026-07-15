"""
Camada de dados: VectorDB (ChromaDB) + Busca Web (DuckDuckGo).

Correções sobre o monólito:
- EmbeddingProvider é SINGLETON. Antes, o modelo HF era recarregado do zero a cada
  reindexação — caro e desnecessário. Agora carrega uma vez e reusa.
- VectorStore faz reindex INCREMENTAL por mtime. A heurística antiga
  `len(ids) < len(arquivos)` comparava nº de chunks com nº de arquivos e quebrava
  após o primeiro split (arquivos novos podiam nunca entrar). Agora comparamos
  mtime por arquivo, deletamos versões velhas e reinserimos só o que mudou.
- WebSearcher usa cache LRU limitado (não cresce sem fim na RAM).
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import textutils
from config import settings
from state import LruCache
from telemetry import telemetry

NENHUM = "NENHUM DADO"

# Bloco de frontmatter YAML no topo de uma nota Obsidian: ---\n ... \n---\n
# (﻿ opcional cobre o BOM que às vezes abre arquivos salvos no Windows)
_FRONTMATTER_RE = re.compile(r"^﻿?---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)


def resolve_device(requested: str, cuda_available: bool) -> str:
    """Resolve o device de embeddings. Puro (testável sem torch).

    - "auto"  -> "cuda" se disponível, senão "cpu".
    - "cuda*" -> respeitado se houver GPU; degrada para "cpu" se não houver.
    - resto   -> devolvido como veio (ex.: "cpu").
    """
    req = (requested or "auto").strip().lower()
    if req == "auto":
        return "cuda" if cuda_available else "cpu"
    if req.startswith("cuda") and not cuda_available:
        return "cpu"
    return req


def strip_frontmatter(texto: str) -> str:
    """Remove o frontmatter YAML do topo da nota (não é conteúdo pesquisável)."""
    return _FRONTMATTER_RE.sub("", texto, count=1)


def split_markdown(conteudo: str, base_metadata: dict, chunk_size: int, chunk_overlap: int) -> list:
    """Quebra uma nota respeitando a estrutura Obsidian.

    1) tira o frontmatter; 2) quebra pelos cabeçalhos (#/##/###), então cada chunk
    é uma SEÇÃO coerente (não um corte cego por nº de caracteres) e carrega o
    caminho dos títulos em `section`; 3) capa por tamanho para seções longas.
    """
    from langchain_core.documents import Document
    from langchain_text_splitters import (
        MarkdownHeaderTextSplitter,
        RecursiveCharacterTextSplitter,
    )

    texto = strip_frontmatter(conteudo)
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
        strip_headers=False,  # mantém o título no texto: dá contexto ao LLM e ao TTS
    )
    try:
        secoes = header_splitter.split_text(texto)
    except Exception:
        secoes = []
    if not secoes:  # nota sem cabeçalho nenhum -> trata o corpo inteiro como uma seção
        secoes = [Document(page_content=texto, metadata={})]

    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )
    out: list = []
    for sec in secoes:
        meta = dict(base_metadata)
        caminho = " > ".join(v for v in sec.metadata.values() if v)
        if caminho:
            meta["section"] = caminho
        for pedaco in char_splitter.split_text(sec.page_content):
            if pedaco.strip():
                out.append(Document(page_content=pedaco, metadata=dict(meta)))
    return out


@dataclass
class LocalResult:
    """Resultado da busca local, já com o veredito de relevância para o gate."""

    texto: str                      # NENHUM se nada aproveitável
    melhor_dist: Optional[float]    # menor distância encontrada (para calibrar)
    relevante: bool                 # há match aterrado (léxico) OU confiante (distância)?


# ==========================================================================
# Embeddings (carregado uma vez)
# ==========================================================================
class EmbeddingProvider:
    def __init__(self) -> None:
        self._embeddings = None

    def load(self) -> None:
        """Síncrono — chamar via asyncio.to_thread no startup."""
        if self._embeddings is not None:
            return
        try:
            from langchain_huggingface import HuggingFaceEmbeddings

            try:
                import torch

                cuda_ok = torch.cuda.is_available()
            except Exception:
                cuda_ok = False
            device = resolve_device(settings.embedding_device, cuda_ok)

            self._embeddings = HuggingFaceEmbeddings(
                model_name=settings.embedding_model,
                model_kwargs={"device": device},
                encode_kwargs={"normalize_embeddings": False},
            )
            telemetry.track("EMBED", f"Embeddings multilingues carregados (singleton, {device}).")
        except Exception as exc:
            telemetry.error("EMBED", "Falha ao carregar embeddings", exc)

    @property
    def instance(self):
        return self._embeddings


# ==========================================================================
# VectorStore (ChromaDB)
# ==========================================================================
class VectorStore:
    def __init__(self, embeddings: EmbeddingProvider) -> None:
        self._embeddings = embeddings
        self._store = None
        self._write_lock = asyncio.Lock()  # era chroma_write_lock

    @property
    def ready(self) -> bool:
        return self._store is not None

    def load_embeddings(self) -> None:
        """Síncrono — chamar via asyncio.to_thread no startup."""
        self._embeddings.load()

    async def open(self) -> None:
        if self._embeddings.instance is None:
            telemetry.warn("DB", "VectorStore sem embeddings — indexação desativada.")
            return
        try:
            from langchain_chroma import Chroma

            async with self._write_lock:
                self._store = await asyncio.to_thread(
                    lambda: Chroma(
                        embedding_function=self._embeddings.instance,
                        persist_directory=settings.diretorio_banco_vetorial,
                        # Distância de COSSENO (não o L2 padrão). Os embeddings não são
                        # normalizados (norma ~4-5), então o L2 dá distâncias ~15 e os
                        # thresholds do gate (rag_score_confident=0.8, rag_score_max=1.5),
                        # que são de escala cosseno, rejeitariam TUDO -> local nunca casa.
                        # Com cosseno, um bom match fica ~0.3 e o gate funciona.
                        collection_metadata={"hnsw:space": "cosine"},
                    )
                )
            telemetry.track("DB", "ChromaDB aberto (distância: cosseno).")
        except Exception as exc:
            telemetry.error("DB", "Falha ao abrir ChromaDB", exc)

    async def sync(self) -> None:
        """Reindex incremental por mtime (novos + modificados)."""
        if self._store is None:
            await self.open()
        if self._store is None:
            return
        try:
            async with self._write_lock:
                existing = await asyncio.to_thread(
                    lambda: self._store.get(include=["metadatas"])
                )
                indexado: dict[str, float] = {}
                for md in existing.get("metadatas", []) or []:
                    src = md.get("source")
                    if src is not None:
                        indexado[src] = float(md.get("mtime", 0) or 0)

                arquivos = glob.glob(
                    os.path.join(settings.caminho_obsidian, "**/*.md"), recursive=True
                )
                pendentes: List[Tuple[str, float]] = []
                for path in arquivos:
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    if indexado.get(path) is None or mtime > indexado.get(path, 0):
                        pendentes.append((path, mtime))

                if not pendentes:
                    telemetry.track("DB", "VectorDB já sincronizado (nada novo).")
                    return

                # remove versões velhas dos arquivos modificados (evita duplicata)
                for path, _ in pendentes:
                    if path in indexado:
                        await asyncio.to_thread(
                            lambda p=path: self._store.delete(where={"source": p})
                        )

                splits = []
                for path, mtime in pendentes:
                    try:
                        conteudo = await asyncio.to_thread(
                            lambda p=path: open(p, "r", encoding="utf-8").read()
                        )
                    except OSError as exc:
                        telemetry.warn("DB", f"Não consegui ler {path}: {exc}")
                        continue
                    is_auto = settings.subpasta_conhecimento_novo in path
                    base_meta = {
                        "source": path,
                        "mtime": mtime,
                        "confidence": 0.6 if is_auto else 1.0,
                        "origin": "Web" if is_auto else "Local",
                    }
                    # Chunking por cabeçalho Markdown (respeita a estrutura Obsidian)
                    splits.extend(
                        split_markdown(
                            conteudo, base_meta, settings.chunk_size, settings.chunk_overlap
                        )
                    )

                for i in range(0, len(splits), settings.chroma_batch):
                    await asyncio.to_thread(
                        self._store.add_documents, splits[i : i + settings.chroma_batch]
                    )
                telemetry.track(
                    "DB", f"Indexados/atualizados {len(pendentes)} arquivos ({len(splits)} chunks)."
                )
        except Exception as exc:
            telemetry.error("DB", "Erro na sincronização do VectorDB", exc)

    async def search(self, termos: str) -> LocalResult:
        """
        Busca híbrida local COM aterramento léxico.

        Um chunk só conta como contexto relevante se (a) menciona alguma keyword da
        pergunta OU (b) é semanticamente muito próximo (distância < rag_score_confident).
        Isso mata o "Cache Hit falso": um trecho genérico semanticamente-parecido mas
        que NÃO fala da entidade perguntada deixa de valer como contexto → vai pra web.
        """
        if self._store is None or not termos:
            return LocalResult(NENHUM, None, False)
        try:
            res = await asyncio.to_thread(
                self._store.similarity_search_with_score, termos, k=settings.rag_top_k
            )
            if not res:
                return LocalResult(NENHUM, None, False)

            melhor = min(score for _, score in res)
            validos = [(score, doc) for doc, score in res if score < settings.rag_score_max]
            chaves = textutils.palavras_chave(termos)
            aterrados = [(s, d) for s, d in validos if textutils.contem_alguma(d.page_content, chaves)]

            if aterrados:                                    # menciona a entidade
                usar, relevante = aterrados, True
            elif melhor < settings.rag_score_confident:      # semanticamente confiante
                usar, relevante = sorted(validos, key=lambda x: x[0])[:1], True
            else:                                            # parecido, mas fora do tema
                usar, relevante = [], False

            texto = NENHUM if not usar else "\n".join(
                f"[Local - Confiança: {d.metadata.get('confidence', 1.0)}] {d.page_content}"
                for _, d in usar
            )
            return LocalResult(texto, melhor, relevante)
        except Exception as exc:
            telemetry.error("DB", "Erro na busca local", exc)
            return LocalResult(NENHUM, None, False)


# ==========================================================================
# Busca Web (DuckDuckGo) + Pre-fetch
# ==========================================================================
class WebSearcher:
    def __init__(self) -> None:
        self._cache = LruCache(settings.max_web_cache)

    async def _ddg(self, termo: str, max_results: int) -> list:
        def _fetch() -> list:
            from ddgs import DDGS

            with DDGS() as ddgs:
                return list(ddgs.text(termo, max_results=max_results))

        return await asyncio.to_thread(_fetch)

    async def search(self, termo: str) -> str:
        """Busca exata com cache."""
        if len(termo.strip()) < 2:
            return NENHUM
        cached = self._cache.get(termo)
        if cached is not None:
            return cached
        telemetry.track("DDG_API", f"Disparando pesquisa web para: '{termo}'")
        try:
            res = await self._ddg(termo, settings.web_max_results)
            if not res:
                return NENHUM
            ctx = "\n".join(f"- FONTE ({r['title']}): {r['body']}" for r in res)
            self._cache.put(termo, ctx)
            return ctx
        except Exception as exc:
            telemetry.error("DDG_API", "Erro na busca web", exc)
            return NENHUM

    async def prefetch(self, tema: str) -> Optional[str]:
        """Speculative Pre-fetch: busca ampla para antecipar a próxima pergunta."""
        telemetry.track("PRE_FETCH", f"Baixando contexto amplo sobre '{tema}'...")
        try:
            res = await self._ddg(f"{tema} resumo geral historico", settings.web_prefetch_results)
            if not res:
                return None
            ctx = "\n".join(f"- CONTEXTO AMPLO ({r['title']}): {r['body']}" for r in res)
            telemetry.track("PRE_FETCH", "Contexto amplo pronto para a RAM.")
            return ctx
        except Exception as exc:
            telemetry.error("PRE_FETCH", "Erro no pre-fetch", exc)
            return None
