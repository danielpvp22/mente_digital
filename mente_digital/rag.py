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
import collections
import glob
import math
import os
import re
import shutil
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from mente_digital import antiinjecao
from mente_digital import disjuntor as _disjuntor
from mente_digital import egressao
from mente_digital import grafo
from mente_digital import textutils
from mente_digital.config import settings
from mente_digital.state import LruCache
from mente_digital.telemetry import db, telemetry

NENHUM = "NENHUM DADO"

# Bloco de frontmatter YAML no topo de uma nota Obsidian: ---\n ... \n---\n
# (﻿ opcional cobre o BOM que às vezes abre arquivos salvos no Windows)
_FRONTMATTER_RE = re.compile(r"^﻿?---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)

# Versão do ESQUEMA de metadados. O reindex é por mtime, então uma nota já indexada
# não seria revisitada só porque o código passou a extrair metadado novo — a base
# ficaria meio velha/meio nova para sempre. Bumpar isto força UMA re-passada e a
# migração se resolve sozinha. v2: origem/colhido_em/colhido_ts do frontmatter.
# v3: conceitos da Malha Neural (ver MalhaIndex).
_META_VERSAO = 3

# Wikilink do Obsidian: [[Conceito]] ou [[Conceito|texto exibido]] (fica o alvo).
_LINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|[^\[\]]*)?\]\]")
# Separador do campo `conceitos` no metadado. O Chroma só guarda ESCALAR (str/num/bool),
# então a lista vira string delimitada. O '|' não aparece em conceito (o _LINK_RE o
# trata como separador de alias), então não há ambiguidade ao refatiar.
_SEP_CONCEITO = "|"


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


def parse_frontmatter(texto: str) -> dict:
    """Lê o frontmatter como pares chave->valor (sem dependência de YAML).

    Contraparte de `strip_frontmatter`: o bloco sai do texto indexado (não polui o
    embedding), mas o que ele DIZ vira metadado no Chroma. Sem isto o `colhido_em`
    que o `agent.normalizar_atomo` grava morre no disco — a "poda por idade a custo
    zero de busca" que ele promete não tinha quem a lesse.

    Parser deliberadamente burro (só `chave: valor` de 1ª linha): é o formato que
    `normalizar_atomo` emite. YAML real (listas, aninhamento) não aparece aqui, e
    fingir suportá-lo convidaria a gravá-lo.
    """
    m = _FRONTMATTER_RE.match(texto)
    if not m:
        return {}
    out: dict = {}
    for ln in m.group(0).splitlines():
        ln = ln.strip().lstrip("﻿")
        if not ln or ln.startswith("---"):
            continue
        chave, sep, valor = ln.partition(":")
        if sep and chave.strip():
            out[chave.strip()] = valor.strip()
    return out


def extrair_conceitos(texto: str) -> List[str]:
    """Conceitos da Malha Neural de um átomo (os [[wikilinks]]). Puro/testável.

    MEDIDO antes de desenhar, sobre os 3.004 átomos reais: 8.403 links, mediana 3 por
    átomo, mas só 3,3% resolvem para o TÍTULO de um átomo existente. A malha NÃO é um
    grafo nota->nota: os links são CONCEITOS ([[Python]] 101x, [[YOLO]] 89x, [[VRAM]]
    81x) e os títulos são auto-contidos com o assunto prefixado ("Clima no Paraguai:
    Efeito de Forno no Verão") — um nunca casa com o outro, por construção.

    Então não se "segue o link até a nota vizinha" (não há para onde ir em 96,7% dos
    casos). O que existe é um índice invertido conceito->átomos, e é ele que o
    MalhaIndex explora.

    Dedup preservando a ordem: um conceito repetido no mesmo átomo não é mais forte,
    e a ordem é a que o LLM escreveu (a mais importante primeiro, na prática).
    """
    vistos: set = set()
    out: List[str] = []
    for bruto in _LINK_RE.findall(texto or ""):
        c = " ".join(bruto.split()).strip()
        if not c:
            continue
        chave = textutils.normaliza(c)
        if chave in vistos:
            continue
        vistos.add(chave)
        out.append(c)
    return out


def _data_para_ts(data: str) -> Optional[float]:
    """'YYYY-MM-DD' -> epoch. None se o formato não casar (nunca levanta)."""
    try:
        return datetime.strptime(data.strip(), "%Y-%m-%d").timestamp()
    except (ValueError, AttributeError):
        return None


def metadados_da_nota(path: str, conteudo: str, mtime: float) -> dict:
    """Metadados de indexação de uma nota. Puro/testável (sem IO, sem Chroma).

    A CONFIANÇA vinha só da PASTA (`is_auto = subpasta_conhecimento_novo in path`).
    Isso classificava os 1.7k+ átomos de `Importado_Gemini` como 1.0/"Local" — o
    mesmo patamar de uma nota escrita à mão pelo usuário — quando são síntese de LLM
    sobre conversas antigas (o `e_fiel` do importador é rede, não prova). O rótulo
    `[Local - Confiança: X]` vai no prompt, então o LLM estava sendo informado de que
    um átomo derivado é fonte primária. O frontmatter sabe a origem real; o caminho
    do arquivo não sabe.

    Três níveis, do mais para o menos confiável:
    - "Local"   (1.0): sem frontmatter e fora da pasta auto -> escrita pelo usuário.
    - "Conversa" (0.8): tem `origem:` -> destilado de conversa/histórico próprio.
    - "Web"     (0.6): pasta de conhecimento auto-colhido -> origem externa.

    `colhido_ts` existe porque o Chroma filtra range em NÚMERO, não em string de data:
    é ele que torna a poda/janela por idade uma cláusula `where`, não um scan.
    """
    fm = parse_frontmatter(conteudo)
    meta: dict = {"source": path, "mtime": mtime, "meta_v": _META_VERSAO}
    origem = fm.get("origem", "")
    if settings.subpasta_conhecimento_novo in path:
        meta["origin"], meta["confidence"] = "Web", 0.6
    elif origem:
        meta["origin"], meta["confidence"] = "Conversa", 0.8
    else:
        meta["origin"], meta["confidence"] = "Local", 1.0
    # Chaves ausentes são OMITIDAS: o Chroma rejeita valor None no metadado.
    if origem:
        meta["origem"] = origem
    colhido = fm.get("colhido_em", "")
    if colhido:
        meta["colhido_em"] = colhido
        ts = _data_para_ts(colhido)
        if ts is not None:
            meta["colhido_ts"] = ts
    conceitos = extrair_conceitos(conteudo)
    if conceitos:
        # Delimitado nas DUAS pontas ('|a|b|') para permitir casar '|x|' exato depois
        # sem pegar prefixo de outro conceito ('|xyz|').
        meta["conceitos"] = _SEP_CONCEITO + _SEP_CONCEITO.join(conceitos) + _SEP_CONCEITO
    return meta


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
class _DocVizinho:
    """Átomo trazido pela malha (não veio da busca vetorial, então não tem distância).

    Duck-type do Document do langchain: o resto do pipeline só lê `.page_content` e
    `.metadata`, e não vale arrastar a dependência até aqui por dois campos.
    """

    page_content: str
    metadata: dict


class MalhaIndex:
    """Índice invertido conceito -> átomos, montado da Malha Neural (ver extrair_conceitos).

    POR QUE existe: a base é Zettelkasten atômica — 1 ideia por nota. A busca vetorial
    devolve os átomos que PARECEM com a pergunta, mas a resposta boa mora na VIZINHANÇA
    deles, que é o pressuposto do Zettelkasten inteiro. Como a malha liga conceitos (e
    não notas), a vizinhança se atravessa pelo conceito COMPARTILHADO: os átomos que o
    LLM marcou com [[TensorRT]] na ingestão são a vizinhança de TensorRT, escrita à mão.

    O peso é IDF, e isso NÃO é enfeite: [[Python]] está em 101 átomos e [[DuckDB]] em 34.
    Expandir por um hub arrastaria meia base para o contexto. Compartilhar um conceito
    RARO é evidência de vizinhança; compartilhar [[IA]] não é evidência de nada. (É a
    mesma falha que o aterramento léxico tem hoje — OR booleano sem IDF — e que aqui,
    de propósito, não repetimos.)

    Vive em RAM: ~3k átomos custam poucos MB e a expansão precisa ser barata (roda no
    caminho da resposta). Se o vault crescer ordens de grandeza, isto vira o primeiro
    lugar a revisitar.
    """

    def __init__(self) -> None:
        self._por_conceito: dict = {}       # conceito normalizado -> set de sources
        self._conceitos_de: dict = {}       # source -> lista de conceitos normalizados
        self._texto_de: dict = {}           # source -> texto do átomo (concat dos chunks)
        self._meta_de: dict = {}            # source -> metadado (1º chunk basta)
        self._df_palavra: dict = {}         # palavra normalizada -> nº de átomos que a contêm (G3)

    @property
    def n_atomos(self) -> int:
        return len(self._conceitos_de)

    @property
    def n_conceitos(self) -> int:
        return len(self._por_conceito)

    def construir(self, documentos: List[str], metadatas: List[dict]) -> None:
        """(Re)constrói o índice a partir do dump do Chroma. Idempotente."""
        self._por_conceito, self._conceitos_de = {}, {}
        self._texto_de, self._meta_de = {}, {}
        self._df_palavra = {}
        for texto, md in zip(documentos, metadatas):
            src = str((md or {}).get("source") or "")
            if not src:
                continue
            # Um átomo pode ter virado 2+ chunks; o texto do vizinho é a nota inteira.
            self._texto_de[src] = (self._texto_de.get(src, "") + "\n" + (texto or "")).strip()
            self._meta_de.setdefault(src, dict(md or {}))
            if src in self._conceitos_de:
                continue
            bruto = str((md or {}).get("conceitos") or "")
            conceitos = [
                textutils.normaliza(c) for c in bruto.split(_SEP_CONCEITO) if c.strip()
            ]
            self._conceitos_de[src] = conceitos
            for c in conceitos:
                self._por_conceito.setdefault(c, set()).add(src)
        # DOCUMENT-FREQUENCY DE PALAVRAS (G3): conta em quantos ÁTOMOS cada palavra
        # aparece (não ocorrências), para o IDF do aterramento léxico. Feito num 2º passe
        # sobre o texto JÁ concatenado por átomo (`_texto_de`), então um átomo multi-chunk
        # conta uma vez só — o mesmo cuidado do índice de conceitos acima.
        for texto in self._texto_de.values():
            for w in set(textutils.tokens(texto)):
                self._df_palavra[w] = self._df_palavra.get(w, 0) + 1

    def idf(self, conceito: str) -> float:
        """log(N/df). Conceito ausente devolve 0.0 — não pontua, em vez de explodir."""
        df = len(self._por_conceito.get(textutils.normaliza(conceito), ()))
        if df <= 0 or self.n_atomos <= 0:
            return 0.0
        return math.log(self.n_atomos / df)

    def idf_palavra(self, palavra: str) -> float:
        """log(N/df) de uma PALAVRA sobre o corpus de átomos (G3, aterramento léxico).

        Gêmeo de `idf` mas sobre `_df_palavra` (palavra→nº de átomos) em vez de conceitos.
        Palavra ausente do corpus devolve 0.0 (não pontua): uma keyword que nenhum átomo
        contém não aterra nada de qualquer forma, então descartá-la é inócuo.
        """
        df = self._df_palavra.get(textutils.normaliza(palavra), 0)
        if df <= 0 or self.n_atomos <= 0:
            return 0.0
        return math.log(self.n_atomos / df)

    def pontes(self, df_min: int, coocorrencia_max: int, limite: int) -> list:
        """Descobridor de Conexões (G8): delega ao `grafo.descobrir_pontes` os mapas da
        malha (conceito->átomos, átomo->conceitos). Mantém a lógica de grafo pura e fora
        da camada de dados. Vazio se a malha não foi construída."""
        return grafo.descobrir_pontes(
            self._por_conceito, self._conceitos_de, df_min, coocorrencia_max, limite
        )

    def centralidade(self, sources: List[str]) -> dict:
        """Score de centralidade de cada `source` DENTRO do conjunto dado (G7).

        Para cada átomo, soma sobre seus conceitos `idf(conceito) * (nº de OUTROS átomos
        DO CONJUNTO que compartilham esse conceito)`. É a "hub-ness" do átomo em relação ao
        próprio tema recuperado: um átomo cujos conceitos RAROS reaparecem nos vizinhos do
        conjunto é o backbone do tema. Conceito-hub (idf baixo) quase não pontua — a mesma
        régua da `vizinhos`. Source fora da malha (sem conceitos) recebe 0.0."""
        conjunto = [s for s in sources if s in self._conceitos_de]
        no_conjunto = set(conjunto)
        scores: dict = {}
        for s in conjunto:
            total = 0.0
            for c in self._conceitos_de.get(s, ()):  # já normalizados
                peso = self.idf(c)
                if peso <= 0:
                    continue
                portadores = self._por_conceito.get(c, ())
                n_outros = sum(1 for o in portadores if o != s and o in no_conjunto)
                total += peso * n_outros
            scores[s] = total
        return scores

    def vizinhos(
        self, sementes: List[str], limite: int, idf_min: float
    ) -> List[Tuple[float, "_DocVizinho"]]:
        """Átomos que compartilham conceitos com as `sementes`, ranqueados por IDF somado.

        `sementes` são os `source` dos átomos que a busca vetorial já escolheu — eles
        são EXCLUÍDOS do resultado (já estão no contexto; repeti-los gasta orçamento).
        `idf_min` corta os hubs: um conceito genérico demais não é evidência.
        """
        if not sementes or limite <= 0:
            return []
        semente_set = set(sementes)
        scores: dict = {}
        for src in sementes:
            for c in self._conceitos_de.get(src, ()):  # já normalizados
                peso = self.idf(c)
                if peso < idf_min:
                    continue
                for vizinho in self._por_conceito.get(c, ()):
                    if vizinho in semente_set:
                        continue
                    scores[vizinho] = scores.get(vizinho, 0.0) + peso
        ordenado = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        out: List[Tuple[float, _DocVizinho]] = []
        for src, score in ordenado[:limite]:
            texto = self._texto_de.get(src, "")
            if not texto.strip():
                continue
            out.append((score, _DocVizinho(texto, dict(self._meta_de.get(src, {})))))
        return out


@dataclass
class LocalResult:
    """Resultado da busca local, já com o veredito de relevância para o gate."""

    texto: str                      # NENHUM se nada aproveitável
    melhor_dist: Optional[float]    # menor distância encontrada (para calibrar)
    relevante: bool                 # há match aterrado (léxico) OU confiante (distância)?
    # Arquivos-fonte dos chunks que ENTRARAM no contexto (não só recuperados). Alimenta
    # a PROMOÇÃO: se a resposta local usar estes átomos, o Agent tira o #conhecimento_novo.
    fontes: List[str] = field(default_factory=list)


# ==========================================================================
# Embeddings (carregado uma vez)
# ==========================================================================
def _com_prefixos(inner, q_prefix: str, p_prefix: str):
    """Envelopa um Embeddings para prefixar query/passagem (família e5). TODO caminho
    de produção (Chroma index/busca, malha G5', RAG efêmero web) passa por embed_query/
    embed_documents — então prefixar aqui, num ponto só, cobre tudo. Só é usado quando
    há prefixo configurado (o chamador nem envelopa se ambos forem vazios)."""
    from langchain_core.embeddings import Embeddings

    class _Prefixado(Embeddings):
        def embed_documents(self, texts):
            return inner.embed_documents([p_prefix + t for t in texts])

        def embed_query(self, text):
            return inner.embed_query(q_prefix + text)

    return _Prefixado()


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

            hf = HuggingFaceEmbeddings(
                model_name=settings.embedding_model,
                model_kwargs={"device": device},
                encode_kwargs={"normalize_embeddings": False},
            )
            # Prefixos e5 (query:/passage:) se configurados — no-op para o MiniLM atual.
            qp = settings.embedding_query_prefix
            pp = settings.embedding_passage_prefix
            self._embeddings = _com_prefixos(hf, qp, pp) if (qp or pp) else hf
            extra = f", prefixos q='{qp}' p='{pp}'" if (qp or pp) else ""
            telemetry.track("EMBED", f"Embeddings multilingues carregados (singleton, {device}{extra}).")
        except Exception as exc:
            telemetry.error("EMBED", "Falha ao carregar embeddings", exc)

    @property
    def instance(self):
        return self._embeddings


# ==========================================================================
# VectorStore (ChromaDB)
# ==========================================================================
class _FingerprintMudou(RuntimeError):
    """Índice construído com OUTRO embedding/prefixos que os da config atual —
    seguir usando degradaria a recuperação em silêncio (mesma dimensão não estoura
    erro nenhum no Chroma). Levantada no open() para reusar o caminho do #33."""


class VectorStore:
    def __init__(self, embeddings: EmbeddingProvider) -> None:
        self._embeddings = embeddings
        self._store = None
        self._write_lock = asyncio.Lock()  # era chroma_write_lock
        self.malha = MalhaIndex()
        self._recuperado_ja = False  # #33: recupera índice no máximo 1x por processo

    @property
    def ready(self) -> bool:
        return self._store is not None

    def load_embeddings(self) -> None:
        """Síncrono — chamar via asyncio.to_thread no startup."""
        self._embeddings.load()

    def _construir_store(self):
        """Constrói o cliente Chroma (síncrono — chamar via to_thread). Isolado do
        open() para o caminho de auto-recuperação poder reabrir sem duplicar código."""
        from langchain_chroma import Chroma

        return Chroma(
            embedding_function=self._embeddings.instance,
            persist_directory=settings.diretorio_banco_vetorial,
            # Distância de COSSENO (não o L2 padrão). Os embeddings não são
            # normalizados (norma ~4-5), então o L2 dá distâncias ~15 e os
            # thresholds do gate (rag_score_confident=0.8, rag_score_max=1.5),
            # que são de escala cosseno, rejeitariam TUDO -> local nunca casa.
            # Com cosseno, um bom match fica ~0.3 e o gate funciona.
            # Fingerprint (painel 2026-07): carimba COM O QUÊ o índice foi
            # construído. Só é aplicado na CRIAÇÃO da coleção — abrir uma coleção
            # existente preserva o carimbo antigo, que é exatamente o que o
            # _fingerprint_ok compara no open().
            collection_metadata={
                "hnsw:space": "cosine",
                "emb_model": settings.embedding_model,
                "emb_query_prefix": settings.embedding_query_prefix,
                "emb_passage_prefix": settings.embedding_passage_prefix,
            },
        )

    async def _abrir_e_provar(self):
        """Abre o Chroma E o PROVA com uma leitura mínima. A prova é o que
        distingue 'abriu' de 'abriu íntegro': um HNSW/sqlite corrompido às vezes
        constrói o objeto e só estoura na 1ª leitura. Levanta se algo falhar."""
        store = await asyncio.to_thread(self._construir_store)
        await asyncio.to_thread(lambda: store.get(limit=1))  # probe
        return store

    def _fingerprint_ok(self, store) -> bool:
        """Fingerprint do índice (painel 2026-07): True se a coleção foi construída
        com o MESMO embedding E prefixos da config atual.

        Trocar MENTE_EMBEDDING_MODEL para outro de MESMA dimensão (768→768) não
        estoura erro NENHUM no Chroma — a recuperação só degrada, silenciosamente.
        Este é o único detector desse caso; dimensão diferente ao menos quebra alto.
        A leitura usa a API semi-privada `_collection`, então é cercada: se a lib
        mudar num upgrade, o open() NUNCA cai por causa do carimbo — loga e segue.
        """
        try:
            meta = dict(getattr(store, "_collection").metadata or {})
        except Exception as exc:
            telemetry.warn("DB", f"Fingerprint ilegível (lib mudou?): {exc} — seguindo sem checar.")
            return True
        atual = {
            "emb_model": settings.embedding_model,
            "emb_query_prefix": settings.embedding_query_prefix,
            "emb_passage_prefix": settings.embedding_passage_prefix,
        }
        if "emb_model" not in meta:
            # Coleção legada (pré-fingerprint): carimba a config ATUAL — correto
            # porque o índice vigente foi construído com ela (o reindex do e5-base
            # acabou de acontecer). ATENÇÃO: o Chroma REJEITA modify() com QUALQUER
            # chave hnsw:* — mesmo com o valor idêntico — porque a distância é IMUTÁVEL
            # pós-criação ("Changing the distance function of a collection once it is
            # created is not supported currently"). A distância vive na CONFIGURAÇÃO da
            # coleção, não no metadata mutável, então um modify de metadata não a perde:
            # basta carimbar os campos REGULARES (sem hnsw:*). Reenviar hnsw:space era o
            # que gerava o warning "Não consegui carimbar a coleção legada".
            regulares = {k: v for k, v in meta.items() if not k.startswith("hnsw:")}
            try:
                store._collection.modify(metadata={**regulares, **atual})
                telemetry.track("DB", "Fingerprint: coleção legada carimbada com a config atual.")
            except Exception as exc:
                telemetry.warn("DB", f"Não consegui carimbar a coleção legada: {exc}")
            return True
        return all(meta.get(k) == v for k, v in atual.items())

    async def open(self) -> None:
        if self._embeddings.instance is None:
            telemetry.warn("DB", "VectorStore sem embeddings — indexação desativada.")
            return
        async with self._write_lock:
            try:
                store = await self._abrir_e_provar()
                # Fingerprint ANTES de aceitar o índice (painel 2026-07): embedding
                # ou prefixos mudaram sem reindex = toda busca degrada em silêncio.
                # Mismatch reusa o MESMO caminho do #33 logo abaixo.
                if not await asyncio.to_thread(self._fingerprint_ok, store):
                    raise _FingerprintMudou(
                        f"índice não foi construído com '{settings.embedding_model}'"
                        " (+prefixos atuais) — reindex necessário"
                    )
                self._store = store
                telemetry.track("DB", "ChromaDB aberto (distância: cosseno).")
            except Exception as exc:
                # #33: índice corrompido (ou fingerprint divergente). Como o vault é
                # a fonte de verdade, move o banco para o lado e reabre vazio — o
                # sync() seguinte reconstrói do vault (sem tocar mtime dos .md).
                # Uma vez por processo.
                if not (settings.indice_auto_recuperar and not self._recuperado_ja):
                    telemetry.error("DB", "Falha ao abrir ChromaDB", exc)
                    return
                self._recuperado_ja = True
                if isinstance(exc, _FingerprintMudou):
                    telemetry.error(
                        "DB",
                        "EMBEDDING MUDOU — reconstruindo o índice do vault. O boot vai "
                        "demorar alguns minutos re-embedando tudo; NÃO é travamento.",
                        exc,
                    )
                else:
                    telemetry.error("DB", "ChromaDB corrompido — recuperando do vault", exc)
                await asyncio.to_thread(
                    mover_indice_corrompido, settings.diretorio_banco_vetorial
                )
                try:
                    self._store = await self._abrir_e_provar()
                    telemetry.track("DB", "Índice recuperado: banco vazio reaberto, sync reconstrói do vault.")
                except Exception as exc2:
                    telemetry.error("DB", "Recuperação do índice falhou", exc2)
                    return
        if self._store is not None:
            await self._reconstruir_malha()

    async def _reconstruir_malha(self) -> None:
        """Recarrega o índice de conceitos do que está no Chroma.

        Chamado no open (o índice vive em RAM, some com o processo) e no fim do sync
        (nota nova = conceito novo; sem isto a expansão ficaria olhando um retrato
        velho da base). Nunca derruba a busca: sem malha, a expansão só não acontece.
        """
        if self._store is None:
            return
        try:
            dump = await asyncio.to_thread(
                lambda: self._store.get(include=["documents", "metadatas"])
            )
            await asyncio.to_thread(
                self.malha.construir,
                dump.get("documents") or [],
                dump.get("metadatas") or [],
            )
            telemetry.track(
                "MALHA",
                f"Índice de conceitos: {self.malha.n_conceitos} conceitos "
                f"em {self.malha.n_atomos} átomos.",
            )
        except Exception as exc:
            telemetry.error("MALHA", "Falha ao montar índice de conceitos", exc)

    async def sync(self) -> None:
        """Reindex incremental por mtime (novos + modificados) e por `meta_v`
        (notas indexadas com esquema de metadado antigo — ver `_META_VERSAO`)."""
        if self._store is None:
            await self.open()
        if self._store is None:
            return
        try:
            async with self._write_lock:
                existing = await asyncio.to_thread(
                    lambda: self._store.get(include=["metadatas"])
                )
                # PURGA DE ÓRFÃOS: chunks cujo `source` não vive mais sob o vault atual
                # (ou sumiu do disco). Conserta o lixo deixado quando o CAMINHO do vault
                # muda — ex.: a migração de `.../Desktop/projetos/memoria_vetorial/...`
                # para a pasta do projeto duplicava TODA nota no Chroma (source velho +
                # novo), pois o delete-by-source do reindex só casa strings idênticas.
                base = os.path.normcase(os.path.abspath(settings.caminho_obsidian))
                indexado: dict[str, float] = {}
                versao: dict[str, int] = {}
                orfaos: set[str] = set()
                for md in existing.get("metadatas", []) or []:
                    src = md.get("source")
                    if src is None:
                        continue
                    ap = os.path.normcase(os.path.abspath(str(src)))
                    dentro = ap == base or ap.startswith(base + os.sep)
                    if not dentro or not os.path.exists(str(src)):
                        orfaos.add(str(src))
                        continue
                    indexado[src] = float(md.get("mtime", 0) or 0)
                    # MIN entre os chunks da nota: se qualquer pedaço ficou para trás
                    # (lote interrompido no meio), a nota inteira é reprocessada.
                    v = int(md.get("meta_v", 1) or 1)
                    versao[src] = min(versao.get(src, v), v)

                for src in orfaos:
                    await asyncio.to_thread(
                        lambda s=src: self._store.delete(where={"source": s})
                    )
                if orfaos:
                    telemetry.track(
                        "DB", f"Purga: {len(orfaos)} fontes órfãs removidas (vault movido/nota apagada)."
                    )
                    # Métricas do ciclo (painel): purga persistida como evento.
                    await asyncio.to_thread(
                        db.log_etl, "PURGA_ORFAOS", f"{len(orfaos)} fonte(s)", "removidas"
                    )

                arquivos = glob.glob(
                    os.path.join(settings.caminho_obsidian, "**/*.md"), recursive=True
                )
                pendentes: List[Tuple[str, float]] = []
                for path in arquivos:
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    if (
                        indexado.get(path) is None
                        or mtime > indexado.get(path, 0)
                        or versao.get(path, 1) < _META_VERSAO
                    ):
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
                    base_meta = metadados_da_nota(path, conteudo, mtime)
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
            # Fora do write_lock: _reconstruir_malha só LÊ, e o lock não é reentrante.
            await self._reconstruir_malha()
        except Exception as exc:
            telemetry.error("DB", "Erro na sincronização do VectorDB", exc)

    async def buscar_conteudos(self, query: str, k: int) -> List[str]:
        """Recuperação CRUA para a Síntese sob Demanda (#23): conteúdo (sem frontmatter)
        dos top-k átomos, deduplicado — SEM gate nem orçamento. O chamador fatia em lotes
        que cabem no n_ctx (map-reduce), então aqui a largura é livre. Vazio sem loja
        (testes) ou query vazia — fail-open, como o resto do RAG."""
        if self._store is None or not query.strip():
            return []
        try:
            res = await asyncio.to_thread(
                self._store.similarity_search_with_score, query, k=k
            )
        except Exception as exc:
            telemetry.error("LOCAL", "Falha na busca para síntese", exc)
            return []
        vistos: set[str] = set()
        itens: List[Tuple[str, str]] = []   # (conteúdo, source)
        for doc, _score in res:
            c = strip_frontmatter(doc.page_content).strip()
            if c and c not in vistos:
                vistos.add(c)
                itens.append((c, str(doc.metadata.get("source") or "")))
        # HUBS PRIMEIRO (G7): reordena por centralidade na malha para o backbone do tema
        # cair nos primeiros lotes do map-reduce. Ordenação ESTÁVEL: empate preserva a
        # relevância vetorial. Sem malha construída (n_atomos==0, testes), mantém a ordem.
        if settings.sintese_hubs_primeiro and self.malha.n_atomos > 0:
            cent = self.malha.centralidade([s for _, s in itens])
            itens.sort(key=lambda it: -cent.get(it[1], 0.0))
        return [c for c, _ in itens]

    async def recuperar(self, consulta: str, k: Optional[int] = None) -> Optional[list]:
        """Só a RECUPERAÇÃO vetorial (embedding + HNSW) — a parte cara do search(),
        sem gate/aterramento. Existe para a fase (b) da consultoria TTFT (#9): o Agent
        a dispara em PARALELO com o LLM do extrator (o embedding usa a pergunta CRUA,
        que não depende dos termos) e entrega o resultado ao search() via `recuperados`.
        Devolve o mesmo formato do similarity_search_with_score. None em erro/sem
        loja/consulta vazia — o chamador cai na busca normal (fail-open, como todo o RAG)."""
        if self._store is None or not consulta.strip():
            return None
        try:
            return await asyncio.to_thread(
                self._store.similarity_search_with_score,
                consulta.strip(), k=k or settings.rag_top_k,
            )
        except Exception as exc:
            telemetry.error("LOCAL", "Falha na recuperação especulativa", exc)
            return None

    async def search(
        self, termos: str, texto_busca: Optional[str] = None, economico: bool = False,
        recuperados: Optional[list] = None,
    ) -> LocalResult:
        """
        Busca híbrida local COM aterramento léxico.

        `economico=True` (Modo Econômico #30) BYPASSA o gate: aceita qualquer átomo
        VÁLIDO (< rag_score_max) como contexto, mesmo sem aterramento nem confiança —
        reabre o "Cache Hit falso" DE PROPÓSITO (opt-in), para responder local em vez
        de escalar pra web. Trade-off do usuário: menos web, menos precisão.

        Um chunk só conta como contexto relevante se (a) menciona alguma keyword da
        pergunta OU (b) é semanticamente muito próximo (distância < rag_score_confident).
        Isso mata o "Cache Hit falso": um trecho genérico semanticamente-parecido mas
        que NÃO fala da entidade perguntada deixa de valer como contexto → vai pra web.

        `termos` é a query enxuta (5 palavras) usada para o ATERRAMENTO léxico. Já o
        EMBEDDING usa `texto_busca` quando fornecido (pergunta inteira ou passagem HyDE
        — ver Agent._texto_busca): o modelo de embedding é simétrico, então uma consulta
        no formato de passagem casa muito melhor com os parágrafos do banco do que a
        query de keywords. Sem `texto_busca`, cai no `termos` (compatível com os testes).
        """
        if self._store is None or not termos:
            return LocalResult(NENHUM, None, False)
        consulta = (texto_busca or termos).strip() or termos
        try:
            # Fase (b) (consultoria #9): `recuperados` chega pronto quando o Agent
            # especulou a recuperação em paralelo com o LLM do extrator. A consulta da
            # especulação é a MESMA pergunta crua que seria embeddada aqui, então o
            # resultado é idêntico ao da linha de baixo — só o instante muda. Lista
            # vazia é resultado legítimo ("especulei e não veio nada"), não fallback.
            res = recuperados if recuperados is not None else await asyncio.to_thread(
                self._store.similarity_search_with_score, consulta, k=settings.rag_top_k
            )
            if not res:
                return LocalResult(NENHUM, None, False)

            melhor = min(score for _, score in res)
            validos = [(score, doc) for doc, score in res if score < settings.rag_score_max]
            chaves = textutils.palavras_chave(termos)
            # ATERRAMENTO PONDERADO POR IDF (G3): o aterramento antigo era um OR sem peso —
            # uma keyword comum (que escapou do STOP) casava a nota errada. Agora só a
            # keyword RARA (idf sobre o corpus de átomos >= mínimo) vale como evidência.
            # Se TODAS forem hub, `chaves_aterr` fica vazio → nada aterra léxico (a nota
            # ainda pode entrar por confiança semântica abaixo). Guardas: desligado com
            # idf_min<=0, e sem malha construída (n_atomos==0, ex.: testes) mantém o OR
            # original — nunca fica MAIS rígido do que dá para calibrar.
            chaves_aterr = chaves
            if settings.aterramento_idf_min > 0 and self.malha.n_atomos > 0:
                chaves_aterr = {
                    k for k in chaves
                    if self.malha.idf_palavra(k) >= settings.aterramento_idf_min
                }
            aterrados = [(s, d) for s, d in validos if textutils.contem_alguma(d.page_content, chaves_aterr)]

            if settings.rag_debug:
                telemetry.track(
                    "LOCAL_DBG",
                    f"termos='{termos}' recuperados={len(res)} "
                    f"validos={len(validos)} aterrados={len(aterrados)}",
                )
                for s, d in sorted(validos, key=lambda x: x[0]):
                    fonte = os.path.basename(str(d.metadata.get("source", "")))
                    telemetry.track("LOCAL_DBG", f"  dist={s:.3f} [{fonte}] :: {d.page_content[:80]!r}")

            # Base ZETTELKASTEN: cada nota é 1 ideia, então uma resposta de verdade
            # precisa de MUITOS átomos. Um chunk entra se (a) menciona a entidade
            # (aterrado) OU (b) é semanticamente confiante (< rag_score_confident).
            # Prioriza aterrados, completa com confiáveis, sem repetir. O corte real é
            # o orçamento de caracteres (protege o n_ctx), não só a contagem.
            confiaveis = [(s, d) for s, d in validos if s < settings.rag_score_confident]
            vistos: set[str] = set()
            # DEDUP NEAR-DUPLICATE (G6): além do texto EXATO (`vistos`), descarta o átomo
            # cujo conjunto de tokens é quase igual ao de um já escolhido (Jaccard >=
            # limiar) — o ETL às vezes atomiza o mesmo fato de web+conversa com palavras
            # levemente diferentes, e os dois no contexto só custam prefill. Sem embedding
            # (velocidade pura). Desligado com limiar<=0 ou >=1.
            limiar_dedup = settings.rag_dedup_near_jaccard
            checa_near = 0.0 < limiar_dedup < 1.0
            tokens_vistos: List[set] = []
            candidatos: List[Tuple[float, object]] = []
            # Modo Econômico (#30): ignora aterramento/confiança e considera TODOS os
            # válidos — assim uma pergunta sem match forte ainda responde do vault.
            fonte_candidatos = validos if economico else (aterrados + confiaveis)
            if economico and validos:
                telemetry.track("LOCAL", "Modo econômico: gate bypassado, ingerindo todos os válidos.")
            for s, d in fonte_candidatos:
                if d.page_content in vistos:
                    continue
                if checa_near:
                    toks = set(textutils.tokens(d.page_content))
                    if any(textutils.jaccard(toks, t) >= limiar_dedup for t in tokens_vistos):
                        continue                # near-duplicate de um átomo já escolhido
                    tokens_vistos.append(toks)
                vistos.add(d.page_content)
                candidatos.append((s, d))
            candidatos.sort(key=lambda x: x[0])
            relevante = bool(candidatos)

            # EXPANSÃO PELA MALHA — só DEPOIS de `relevante` estar decidido, e isso é
            # o ponto: a vizinhança ENRIQUECE uma resposta que já tem âncora, mas nunca
            # pode transformar pergunta-sem-match em Cache Hit. Deixá-la votar no gate
            # ressuscitaria o "Cache Hit falso" por outra porta — agora por conceito.
            vizinhos: List[Tuple[Optional[float], object]] = []
            if settings.malha_expandir and candidatos:
                sementes: List[str] = []
                for _, d in candidatos[: settings.rag_max_chunks]:
                    src = str(d.metadata.get("source") or "")
                    if src and src not in sementes:
                        sementes.append(src)
                brutos: List[object] = []
                for _score, dv in self.malha.vizinhos(
                    sementes, settings.malha_max_vizinhos, settings.malha_idf_min
                ):
                    if dv.page_content in vistos:
                        continue
                    vistos.add(dv.page_content)
                    brutos.append(dv)
                # G5′: FILTRO DE PROXIMIDADE À PERGUNTA. A malha traz o vizinho pelo conceito
                # raro COMPARTILHADO, mas medido: vem "do assunto certo, da pergunta errada".
                # Rankeia os vizinhos pela similaridade de cosseno com a PERGUNTA (embedding já
                # carregado) e corta os abaixo de malha_sim_min — exige conceito raro E
                # proximidade. Fail-open: sem embeddings (testes), botão 0, ou erro, mantém
                # todos (comportamento anterior). Já sai ordenado por proximidade (melhor 1º).
                emb = self._embeddings.instance if self._embeddings else None
                if emb is not None and brutos and settings.malha_sim_min > 0:
                    try:
                        qv = await asyncio.to_thread(emb.embed_query, consulta)
                        textos = [d.page_content for d in brutos]
                        vv = await asyncio.to_thread(emb.embed_documents, textos)
                        por_texto = {d.page_content: d for d in brutos}
                        rankeados = rankear_por_similaridade(qv, list(zip(textos, vv)))
                        brutos = [
                            por_texto[t] for sim, t in rankeados
                            if sim >= settings.malha_sim_min
                        ]
                    except Exception as exc:
                        telemetry.error("MALHA", "Falha no filtro de proximidade do vizinho", exc)
                vizinhos = [(None, dv) for dv in brutos]

            # Os matches reais vêm primeiro; a vizinhança disputa o que SOBRAR do
            # orçamento. Ordem = prioridade, o corte é o char budget (protege o n_ctx).
            usar: List[Tuple[Optional[float], object]] = []
            orcamento = settings.rag_context_char_budget
            for s, d in list(candidatos[: settings.rag_max_chunks]) + vizinhos:
                if usar and orcamento - len(d.page_content) < 0:
                    break                                    # respeita o teto (n_ctx)
                usar.append((s, d))
                orcamento -= len(d.page_content)

            if settings.rag_debug:
                n_viz = sum(1 for s, _ in usar if s is None)
                telemetry.track(
                    "LOCAL_DBG",
                    f"selecionados={len(usar)}/{len(candidatos)} átomos "
                    f"(aterrados={len(aterrados)}, vizinhos_malha={n_viz}/{len(vizinhos)})",
                )

            # O vizinho é ROTULADO como relacionado, não como match. Sem isso ele chega
            # ao LLM indistinguível de um átomo que responde a pergunta — e um átomo
            # tangencial apresentado como resposta é exatamente a alucinação que o
            # pipeline inteiro combate. O rótulo deixa o modelo usá-lo como apoio.
            texto = NENHUM if not usar else "\n".join(
                (
                    f"[Malha - relacionado] {d.page_content}"
                    if s is None
                    else f"[Local - Confiança: {d.metadata.get('confidence', 1.0)}] {d.page_content}"
                )
                for s, d in usar
            )
            # Fontes (dedup, ordem preservada) dos átomos que ENTRARAM no contexto —
            # a promoção só toca no que foi de fato usado, não em tudo que foi recuperado.
            # VIZINHO DA MALHA NÃO PROMOVE (s is None): ele entrou por conceito
            # compartilhado, não por responder à pergunta. Promovê-lo tiraria o
            # #conhecimento_novo de átomo que só passou perto — o ciclo de maturidade
            # mede REUSO real, e inflá-lo o esvazia de sentido.
            fontes: List[str] = []
            for s, d in usar:
                if s is None:
                    continue
                src = d.metadata.get("source")
                if src and src not in fontes:
                    fontes.append(str(src))
            return LocalResult(texto, melhor, relevante, fontes)
        except Exception as exc:
            telemetry.error("DB", "Erro na busca local", exc)
            return LocalResult(NENHUM, None, False)


# ==========================================================================
# Busca Web (DuckDuckGo) + Pre-fetch
# ==========================================================================
def buscar_com_fallback(fetch_backend, backends: List[str]) -> list:
    """Tenta cada backend em ordem; devolve o 1º resultado não-vazio. Puro/testável.

    Corrige o ponto único de falha do DDG: se um backend cai (rate-limit, mudança
    de HTML), passa para o próximo em vez de simplesmente não ter web.
    """
    ultimo_erro = None
    algum_ok = False
    for backend in backends:
        try:
            res = fetch_backend(backend)
        except Exception as exc:  # tenta o próximo backend
            ultimo_erro = exc
            continue
        algum_ok = True          # respondeu (mesmo que vazio): é "sem resultados"
        if res:
            return res
    # Só propaga erro se NENHUM backend respondeu. Se algum voltou vazio-com-sucesso,
    # isso é "nada encontrado" ([]), não uma falha — não mascara vazio legítimo de erro.
    if not algum_ok and ultimo_erro is not None:
        raise ultimo_erro
    return []


def mover_indice_corrompido(diretorio: str) -> Optional[str]:
    """Move um índice Chroma corrompido para `<dir>.corrompido` (best-effort). Puro
    o bastante para teste: só toca o filesystem do próprio banco vetorial, nunca o
    vault. Devolve o destino, ou None se não havia dir (ou o move falhou — ex.: lock
    de sqlite no Windows, caso comum em corrupção pós-abertura).

    Guardamos a cópia corrompida (em vez de apagar) para inspeção; só mantemos a
    MAIS RECENTE (a anterior é descartada) para não vazar disco a cada recuperação.
    """
    if not os.path.isdir(diretorio):
        return None
    destino = diretorio.rstrip("/\\") + ".corrompido"
    try:
        if os.path.exists(destino):
            shutil.rmtree(destino, ignore_errors=True)
        os.rename(diretorio, destino)
        return destino
    except OSError as exc:
        # No Windows, um sqlite ainda aberto trava o rename. Não conseguimos mover;
        # o chamador degrada graciosamente (deixa claro o que fazer na mão).
        telemetry.warn(
            "DB", f"Não consegui mover o índice corrompido ({exc}). Apague '{diretorio}' na mão."
        )
        return None


def _chunk_texto(texto: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    """Quebra um texto corrido em pedaços para o ranking efêmero. Puro/testável."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )
    return [p.strip() for p in splitter.split_text(texto) if p.strip()]


def rankear_por_similaridade(
    consulta_vec: List[float], docs: List[Tuple[str, List[float]]]
) -> List[Tuple[float, str]]:
    """Ordena (texto, vetor) por similaridade de cosseno com a consulta. Puro/testável.

    Os embeddings NÃO são normalizados (mesma decisão do VectorStore), então
    normalizamos aqui na mão. Devolve [(score_cosseno, texto)] em ordem decrescente.
    """
    import numpy as np

    q = np.asarray(consulta_vec, dtype="float32")
    qn = float(np.linalg.norm(q)) or 1.0
    out: List[Tuple[float, str]] = []
    for texto, vec in docs:
        v = np.asarray(vec, dtype="float32")
        vn = float(np.linalg.norm(v)) or 1.0
        out.append((float(np.dot(q, v)) / (qn * vn), texto))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


# User-Agent de navegador reusado nos DOIS clients do deep-fetch (o normal e o de
# fallback sem verificação de cert) — muitos sites 403-am requisições sem um UA real.
_HEADERS_FETCH = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _erro_de_certificado(exc: BaseException) -> bool:
    """True se a exceção é falha de VERIFICAÇÃO de certificado TLS (cert vencido,
    cadeia inválida, ou hostname mismatch) — NÃO uma falha de rede/HTTP qualquer.

    Puro/testável. Só esse caso autoriza o re-fetch sem verificar o cert: um 500/403/
    timeout não é problema de certificado, e desativar SSL neles não ajudaria (só
    baixaria a segurança à toa). Desce a cadeia de causas porque o httpx embrulha o
    erro de SSL num ConnectError; cai para a string se o tipo não for reconhecível."""
    e: BaseException | None = exc
    for _ in range(6):  # trava contra ciclo de __cause__/__context__
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        if e is None:
            break
        e = e.__cause__ or e.__context__
    texto = str(exc).lower()
    return "certificate_verify_failed" in texto or "certificate verify failed" in texto


class WebSearcher:
    """Busca web em DOIS estágios:

    1) DDG devolve URLs + snippets (rápido, mas raso).
    2) DEEP-FETCH: abre o corpo das top-N páginas (httpx), extrai o texto principal
       (trafilatura), atomiza e RANKEIA os trechos contra a pergunta com o embedding
       já carregado — RAG efêmero, nada é indexado. Passa ao LLM só os melhores
       trechos (dentro de um orçamento de chars). Cai de volta pros snippets se o
       fetch falhar, se estiver desligado, ou se não houver embeddings (ex.: testes).
    """

    def __init__(self, embeddings: Optional["EmbeddingProvider"] = None) -> None:
        self._cache = LruCache(settings.max_web_cache)
        self._embeddings = embeddings  # p/ o ranking do RAG efêmero (opcional)
        # #31: disjuntor anti-shadowban + fila offline (em RAM).
        self._disjuntor = _disjuntor.Disjuntor(
            settings.web_disjuntor_limite_falhas, settings.web_disjuntor_cooldown_seg
        )
        self._pendentes: "collections.deque[Tuple[str, Optional[str]]]" = collections.deque(
            maxlen=settings.web_pendentes_max
        )
        self._retry_task = None  # ref forte: sem ela o GC come o drain (war story #2)
        # "FONTE?" (painel 2026-07): domínios das páginas que a ÚLTIMA busca abriu no
        # deep-fetch. Lido pelo Agent logo após o search() (single-user, GPU
        # serializada — a janela de corrida é desprezível e o dado é só informativo).
        self.ultimos_dominios: List[str] = []

    async def _ddg(self, termo: str, max_results: int) -> list:
        # Guarda de Egressão (#6): este é o ÚNICO ponto onde texto do usuário sai
        # para a rede. Mascara PII antes de o termo virar uma query no DDG.
        if settings.egressao_guarda:
            termo, pii = egressao.mascarar_pii(termo)
            if pii:
                telemetry.warn("EGRESSAO", f"PII mascarada na query web: {', '.join(pii)}")

        def _fetch() -> list:
            from ddgs import DDGS

            def _um_backend(backend: str) -> list:
                with DDGS() as ddgs:
                    try:
                        return list(ddgs.text(termo, max_results=max_results, backend=backend))
                    except TypeError:
                        # versão do ddgs sem o parâmetro 'backend'
                        return list(ddgs.text(termo, max_results=max_results))

            return buscar_com_fallback(_um_backend, settings.web_backends)

        return await asyncio.to_thread(_fetch)

    async def _baixar_pagina(self, client, url: str) -> Optional[str]:
        """Baixa e extrai o texto principal de UMA página. Nunca levanta — devolve
        None em qualquer falha (timeout, 404, HTML sem corpo), pois é best-effort."""
        html = await self._baixar_html(client, url)
        if html is None:
            return None

        def _extrair() -> Optional[str]:
            import trafilatura

            texto = trafilatura.extract(
                html, include_comments=False, include_tables=True, favor_recall=True
            )
            if not texto:
                return None
            return texto[: settings.web_fetch_max_chars]

        try:
            return await asyncio.to_thread(_extrair)
        except Exception as exc:
            telemetry.warn("WEB_FETCH", f"Falha ao extrair {url[:60]}: {exc}")
            return None

    async def _baixar_html(self, client, url: str) -> Optional[str]:
        """Baixa o HTML cru. Nunca levanta. Se (e só se) a falha for de VERIFICAÇÃO de
        certificado e o fallback estiver ligado, re-tenta a MESMA página SEM verificar o
        cert — salva sites com cert quebrado/hostname errado (comuns em blogs BR, ex.:
        www.orapha.dev visto ao vivo). É conteúdo público lido p/ RAG (rankeado/filtrado),
        então o risco de MITM é baixo; ainda assim, por ser downgrade de segurança, fica
        atrás de `web_fetch_ssl_fallback`."""
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            if settings.web_fetch_ssl_fallback and _erro_de_certificado(exc):
                return await self._baixar_sem_verificar_cert(url)
            telemetry.warn("WEB_FETCH", f"Falha ao baixar {url[:60]}: {exc}")
            return None

    async def _baixar_sem_verificar_cert(self, url: str) -> Optional[str]:
        """Re-fetch de UMA página com a verificação de TLS desligada (verify=False).
        Só chamado após um erro de certificado confirmado. Client próprio e efêmero
        (o de fora verifica; este não) — como é raro, o custo de abrir um é aceitável."""
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=settings.web_fetch_timeout, follow_redirects=True,
                headers=_HEADERS_FETCH, verify=False,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            telemetry.warn(
                "WEB_FETCH", f"Cert inválido em {url[:60]} — baixado SEM verificar SSL (fallback)."
            )
            return resp.text
        except Exception as exc:
            telemetry.warn(
                "WEB_FETCH", f"Falha ao baixar {url[:60]} mesmo sem verificar cert: {exc}"
            )
            return None

    async def _deep_fetch(self, res: list, consulta: str) -> Optional[str]:
        """Estágio 2: abre as top-N páginas, atomiza e rankeia contra a consulta.

        Devolve o contexto montado (só os melhores trechos) ou None se nada útil saiu
        — nesse caso o chamador cai de volta pros snippets.
        """
        emb = self._embeddings.instance if self._embeddings else None
        if emb is None:
            return None  # sem embedding não há como rankear -> usa snippets

        urls = [r.get("href") or r.get("url") or "" for r in res]
        urls = [u for u in urls if u][: settings.web_fetch_pages]
        if not urls:
            return None

        try:
            import httpx
        except ImportError:
            telemetry.warn("WEB_FETCH", "httpx ausente — usando só snippets.")
            return None

        async with httpx.AsyncClient(
            timeout=settings.web_fetch_timeout, follow_redirects=True, headers=_HEADERS_FETCH
        ) as client:
            paginas = await asyncio.gather(*(self._baixar_pagina(client, u) for u in urls))

        # Proveniência ("fonte?"): domínios das páginas que BAIXARAM — são as que
        # podem ter contribuído com trechos. Falado como domínio (URL é inaudível).
        from urllib.parse import urlparse

        self.ultimos_dominios = [
            urlparse(u).netloc for u, t in zip(urls, paginas) if t and urlparse(u).netloc
        ]

        textos = [t for t in paginas if t]
        if not textos:
            return None

        # Atomiza todas as páginas num pool de trechos e rankeia contra a pergunta.
        chunks: List[str] = []
        for t in textos:
            chunks.extend(
                _chunk_texto(t, settings.web_chunk_size, settings.web_chunk_overlap)
            )
        if not chunks:
            return None

        # #26: conteúdo web é NÃO-confiável — dropa os trechos com injeção de prompt
        # ANTES de eles virarem contexto do LLM. Se sobrou algo limpo, refaz com ele;
        # se a página inteira era payload, cai para os snippets (return None).
        if settings.antiinjecao_web:
            chunks, removidos = antiinjecao.filtrar_chunks(chunks)
            if removidos:
                telemetry.warn("ANTIINJECAO", f"{removidos} trecho(s) web suspeito(s) de injeção — dropados.")
            if not chunks:
                return None

        try:
            vecs = await asyncio.to_thread(emb.embed_documents, chunks)
            qvec = await asyncio.to_thread(emb.embed_query, consulta)
        except Exception as exc:
            telemetry.error("WEB_FETCH", "Falha ao embeddar trechos web", exc)
            return None

        rankeados = rankear_por_similaridade(qvec, list(zip(chunks, vecs)))

        usar: List[str] = []
        orcamento = settings.web_context_char_budget
        for _score, trecho in rankeados[: settings.web_rank_top_k]:
            if usar and orcamento - len(trecho) < 0:
                break
            usar.append(trecho)
            orcamento -= len(trecho)

        telemetry.track(
            "WEB_FETCH",
            f"{len(textos)}/{len(urls)} páginas, {len(chunks)} trechos -> {len(usar)} rankeados.",
        )
        return "\n\n".join(f"- FONTE WEB: {t}" for t in usar) if usar else None

    async def search(self, termo: str, consulta: Optional[str] = None) -> str:
        """Busca web com cache. `consulta` (pergunta natural) guia o ranking do
        deep-fetch; sem ela, usa o próprio `termo`."""
        # Proveniência: zera SEMPRE — resultado de cache/snippet não herda os
        # domínios da busca anterior (melhor "web" genérico que domínio errado).
        self.ultimos_dominios = []
        if len(termo.strip()) < 2:
            return NENHUM
        cached = self._cache.get(termo)
        if cached is not None:
            return cached
        # #31: disjuntor ABERTO -> não toca o DDG (anti-shadowban); enfileira e sai.
        if settings.web_disjuntor_habilitado and self._disjuntor.aberto():
            self._pendentes.append((termo, consulta))
            telemetry.warn(
                "DDG_API",
                f"Web em cooldown ({self._disjuntor.segundos_restantes():.0f}s) — "
                f"query enfileirada, sem bater no DDG.",
            )
            return NENHUM
        self._talvez_retomar_pendentes()  # cooldown passou? drena a fila em background
        telemetry.track("DDG_API", f"Disparando pesquisa web para: '{termo}'")
        try:
            res = await self._ddg(termo, settings.web_max_results)
            self._disjuntor.registrar_sucesso()  # canal respondeu (mesmo que vazio)
            if not res:
                return NENHUM

            ctx = None
            if settings.web_fetch_enabled:
                try:
                    ctx = await self._deep_fetch(res, (consulta or termo).strip() or termo)
                except Exception as exc:
                    telemetry.error("WEB_FETCH", "Falha no deep-fetch — usando snippets", exc)
                    ctx = None
            # Fallback (deep-fetch off/vazio/sem embeddings): os snippets de sempre.
            if not ctx:
                ctx = "\n".join(f"- FONTE ({r['title']}): {r['body']}" for r in res)

            self._cache.put(termo, ctx)
            return ctx
        except Exception as exc:
            self._disjuntor.registrar_falha()  # falha de CANAL -> aproxima do cooldown
            self._pendentes.append((termo, consulta))
            telemetry.error("DDG_API", "Erro na busca web", exc)
            return NENHUM

    def _talvez_retomar_pendentes(self) -> None:
        """Se o cooldown já passou e há fila offline, drena em background. Segura a
        ref da task (self._retry_task) — sem isso o GC mata o drain (war story #2)."""
        if not self._pendentes:
            return
        if self._retry_task is not None and not self._retry_task.done():
            return  # já tem um drain rodando
        try:
            self._retry_task = asyncio.get_running_loop().create_task(self._retomar_pendentes())
        except RuntimeError:
            pass  # sem loop (contexto síncrono/teste) — sem drenagem, sem crash

    async def _retomar_pendentes(self) -> None:
        """Re-executa as buscas enfileiradas durante o cooldown. Best-effort: o
        resultado cai no cache LRU, então uma re-pergunta idêntica sai na hora. Se
        uma falhar de novo, o disjuntor reabre e o resto volta para a fila."""
        while self._pendentes and not self._disjuntor.aberto():
            termo, consulta = self._pendentes.popleft()
            if self._cache.get(termo) is not None:
                continue  # já resolvido nesse meio tempo
            await self.search(termo, consulta)

    async def prefetch(self, tema: str) -> Optional[str]:
        """Speculative Pre-fetch: busca ampla para antecipar a próxima pergunta."""
        # #31: pre-fetch é ESPECULATIVO — durante o cooldown, jamais gastar uma
        # chamada ao DDG só para antecipar (é o 1º a cortar sob risco de ban).
        if settings.web_disjuntor_habilitado and self._disjuntor.aberto():
            return None
        telemetry.track("PRE_FETCH", f"Baixando contexto amplo sobre '{tema}'...")
        try:
            res = await self._ddg(f"{tema} resumo geral historico", settings.web_prefetch_results)
            self._disjuntor.registrar_sucesso()
            if not res:
                return None
            ctx = "\n".join(f"- CONTEXTO AMPLO ({r['title']}): {r['body']}" for r in res)
            telemetry.track("PRE_FETCH", "Contexto amplo pronto para a RAM.")
            return ctx
        except Exception as exc:
            self._disjuntor.registrar_falha()
            telemetry.error("PRE_FETCH", "Erro no pre-fetch", exc)
            return None
