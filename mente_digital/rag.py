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
from typing import Awaitable, List, Optional, Sequence, Tuple

from mente_digital import antiinjecao
from mente_digital import disjuntor as _disjuntor
from mente_digital import vram
from mente_digital import egressao
from mente_digital import grafo
from mente_digital import identidade
from mente_digital import livro
from mente_digital import obras
from mente_digital import prompts
from mente_digital import textutils
from mente_digital import academico
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
# v4: `tipo` (figura/texto), que habilita a busca de figura em espaço próprio.
_META_VERSAO = 4

# Quantos chunks um dump do Chroma puxa por vez. O limite duro do SQLite é ~32k
# variáveis por statement; 5.000 deixa 6x de folga e o custo de paginar é
# desprezível perto do de processar o dump. Ver `dump_paginado`.
_DUMP_LOTE = 5000

# Marca de PROVENIÊNCIA posta por `_irmaos_de_pagina`. Existe porque `score is None`
# sozinho não distingue as duas coisas que entram sem distância — ver `rotular_contexto`.
IRMAO_DE_PAGINA = "_irmao_de_pagina"


def rotular_contexto(score: Optional[float], doc) -> str:
    """Prefixa o chunk com O QUE ELE É antes de mandar ao LLM. Puro/testável.

    Sem rótulo, um vizinho tangencial chega indistinguível de um átomo que responde
    — exatamente a alucinação que o pipeline combate. `score is None` marca o que
    entrou SEM distância, mas não diz por qual porta.

    DUAS PORTAS ENTRAM SEM DISTÂNCIA, e elas não valem o mesmo. O vizinho da malha
    entrou por conceito compartilhado (evidência fraca: medido em 2026-07-31, o
    ranking por IDF ganha 2,3% das vagas contra o próximo match vetorial). O IRMÃO DE
    PÁGINA entrou por co-locação POR CONSTRUÇÃO — é a mesma página do mesmo livro do
    átomo que respondeu, a evidência mais forte do pipeline depois do match.

    O bug que isto corrige (medido em 205 perguntas reais do `chat_history`, com
    `malha_expandir=false`, ou seja, ZERO vizinhos de malha existindo): o contexto
    ainda levava 3,96 blocos por pergunta rotulados "[Malha - relacionado]" — todos
    irmãos de página. A evidência mais forte chegava ao modelo anunciada como a mais
    fraca, e o rótulo existe exatamente para dizer qual é qual.

    A FIGURA avisa ao modelo que a imagem JÁ SERÁ MOSTRADA ao usuário (o servidor a
    anexa — ver `respostas._mostrar_figuras`). Antes este rótulo pedia que ele
    copiasse o wikilink; o teste real mostrou que não funciona (a resposta cabia em
    90 tokens e o embed sozinho gasta ~40) e que, se funcionasse, duplicaria a
    imagem. Agora ele é instruído a usar a figura como CONTEÚDO e a não repetir o
    link."""
    conteudo = doc.page_content
    if score is None:
        if (getattr(doc, "metadata", None) or {}).get(IRMAO_DE_PAGINA):
            return f"[Mesma página - completa o trecho que respondeu] {conteudo}"
        return f"[Malha - relacionado] {conteudo}"
    if str((doc.metadata or {}).get("tipo") or "") == "figura":
        return ("[Figura do acervo — a imagem já é mostrada ao usuário; descreva o que "
                f"ela ensina, NÃO repita o link] {conteudo}")
    confianca = (doc.metadata or {}).get("confidence", 1.0)
    return f"[Local - Confiança: {confianca}] {conteudo}"


def dump_paginado(store, include: Sequence[str], lote: Optional[int] = None) -> dict:
    """`store.get(include=...)` em LOTES, agregado no mesmo formato de um get só.

    Existe porque o get() sem limite vira um SQL com uma variável por registro e o
    SQLite recusa acima de ~32k ("too many SQL variables"). Não é hipótese: em
    2026-07-26 a base passou de 31.450 para 33.396 chunks ao indexar as notas de
    figura e TODO dump quebrou de uma vez — a malha (fail-soft, morreu calada) e o
    `sync()`, que sem ler o que já está indexado não indexa mais nada.

    Síncrona de propósito: os chamadores já rodam dentro de `asyncio.to_thread`,
    e os scripts offline a usam direto."""
    lote = lote or _DUMP_LOTE      # lido em runtime: o default no `def` congelaria
    agregado: dict = {}
    offset = 0
    while True:
        parte = store.get(include=list(include), limit=lote, offset=offset)
        # `embeddings` pode vir ndarray — nada de truthiness, só tamanho.
        listas = {k: v for k, v in (parte or {}).items()
                  if v is not None and not isinstance(v, (str, bytes, dict))
                  and hasattr(v, "__len__")}
        lidos = max((len(v) for v in listas.values()), default=0)
        if not lidos:
            return agregado
        for k, v in listas.items():
            agregado.setdefault(k, []).extend(v)
        offset += lidos
        if lidos < lote:
            return agregado


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


def preparar_embedding_offline() -> str:
    """Aponta o embedding para o device das passadas OFFLINE. Devolve o pedido.

    Irmão do `llm.preparar_offline`, e pela mesma razão: script roda com o SERVIDOR
    FECHADO, então a GPU inteira está livre. Ao vivo é o oposto — os 10GB da 3080 se
    dividem entre LLM, Whisper e XTTS, e o A/B de 2026-07-29 mediu 474 MiB de folga
    no pico com o e5 junto, contra 1.578 MiB com ele na CPU. Fino demais: o que
    passa disso não estoura, cai no WDDM, que é pior (precipício de desempenho).
    Offline não há essa disputa, e o lote é 14,7x mais rápido (40,7 -> 600 docs/s).

    HONESTIDADE SOBRE O GANHO: 14,7x é o EMBEDDING, não o relógio. Medido no
    `reindexar.py` com 400 notas tocadas, a passada foi de 98s para 77s (-21%) —
    numa passada incremental quem domina é varrer as ~24k notas do vault. O ganho
    grande está no rebuild COMPLETO, em que os ~25k chunks é que mandam.

    Chamar ANTES de `EmbeddingProvider.load`: o provider é singleton e lê o settings
    uma vez só. Com `embedding_device_offline` vazio não sobrepõe nada — é o botão
    de quem quer o comportamento do servidor também nos scripts.
    """
    alvo = (settings.embedding_device_offline or "").strip()
    if not alvo:
        return settings.embedding_device
    if alvo != settings.embedding_device:
        telemetry.track("EMBED", f"Passada offline: embedding em '{alvo}' "
                                 f"(servidor usa '{settings.embedding_device}').")
    settings.embedding_device = alvo
    return alvo


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
    # `tipo` é o que permite BUSCAR FIGURA À PARTE. Medido em 2026-07-26: no banco
    # cheio (33k chunks) as notas de figura perdiam a disputa por vaga no top-k para
    # os átomos de texto — uma figura a 0,1239 ficava fora de um top-40 cujo pior era
    # 0,1373. Com `where={"tipo": "figura"}` a busca roda no subconjunto e o recall
    # volta a ser exato (conferido 20/20 contra força bruta nos 1.735 vetores).
    # Gravado nos DOIS lados: filtro sobre chave ausente não casa nada no Chroma,
    # então "texto" precisa ser afirmativo, não a ausência de "figura".
    meta["tipo"] = "figura" if _e_figura(path) else "texto"
    # ATTACH-ONLY: figura que NÃO disputa vaga na busca (ver `_buscar_figuras`).
    # Só é gravado quando VERDADEIRO, de propósito: as ~1.735 notas de figura já
    # indexadas não têm o campo, e escrever `False` para todas exigiria bumpar o
    # `_META_VERSAO` (uma re-passada na base inteira) para nada — a ausência já
    # significa "buscável", que é o comportamento delas hoje.
    if meta["tipo"] == "figura" and prompts.TAG_FIGURA_ANEXO in conteudo:
        meta["attach_only"] = True
    # ACERVO SUSPEITO: figura colhida da web cuja IMAGEM não é o que a nota afirma ser.
    # A revalidação do idle (`EtlProcessor.revalidar_acervo_web`) marca
    # `acervo_confere: NAO` no frontmatter, mas até 2026-07-31 ninguém lia a marca —
    # exatamente o estágio em que o `attach_only` já esteve ("declaração e não
    # comportamento"). São 5 das 45 notas do acervo, e é o que faz uma pergunta sobre
    # 'polvo' trazer um calendário do EBT da Flórida e 'solo argiloso' trazer uma
    # miniatura de Rainbow Six Siege (o buscador casou o "solo" de *solo queue*).
    # Gravado só quando VERDADEIRO, pelo mesmo motivo do attach_only: chave ausente
    # não casa `where` no Chroma, e ausência aqui significa "confere ou nunca avaliada"
    # — que é o comportamento buscável de toda nota de figura de livro.
    if str(fm.get("acervo_confere", "")).strip().upper() == "NAO":
        meta["acervo_suspeito"] = True
    return meta


def _e_figura(path: str) -> bool:
    """A nota vive sob a subpasta de figuras do vault?

    Compara SEGMENTO de caminho, não substring: `subpasta_conhecimento_novo in path`
    (o padrão vizinho) marcaria uma nota chamada `Figuras_do_livro.md` como figura.
    """
    alvo = (settings.subpasta_figuras or "").strip().strip("/\\").casefold()
    if not alvo:
        return False
    partes = str(path).replace("\\", "/").split("/")
    return alvo in {p.casefold() for p in partes[:-1]}   # só PASTA, nunca o arquivo


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
        self.estender(documentos, metadatas)

    def estender(self, documentos: List[str], metadatas: List[dict]) -> None:
        """Acrescenta o dump de OUTRA coleção a este índice (o fan-in do multiusuário:
        o acervo comum mais o vault pessoal de quem está perguntando).

        ⚠ Cada chamada tem de trazer átomos COMPLETOS de uma coleção. O
        document-frequency é contado uma vez por `source` tocado NESTA chamada, então
        um mesmo átomo partido entre duas chamadas contaria duas vezes — o que não
        acontece porque cada chamada é o dump de uma coleção e nenhum `source` vive
        em duas (a fronteira é a PASTA; ver o bloco `multiusuario_ligado`).
        """
        tocados: set = set()
        for texto, md in zip(documentos, metadatas):
            src = str((md or {}).get("source") or "")
            if not src:
                continue
            tocados.add(src)
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
                portadores = self._por_conceito.get(c)
                # Set NOVO em vez de `.add`: numa malha DERIVADA este set ainda é o do
                # acervo, compartilhado com os outros donos — mutá-lo daria a cada um a
                # vizinhança dos demais. Copy-on-write: só o conceito que este dump
                # toca é copiado (ver `derivada`).
                self._por_conceito[c] = (set(portadores) | {src}) if portadores else {src}
        # DOCUMENT-FREQUENCY DE PALAVRAS (G3): conta em quantos ÁTOMOS cada palavra
        # aparece (não ocorrências), para o IDF do aterramento léxico. Feito num 2º passe
        # sobre o texto JÁ concatenado por átomo (`_texto_de`), então um átomo multi-chunk
        # conta uma vez só — o mesmo cuidado do índice de conceitos acima.
        for src in tocados:
            for w in set(textutils.tokens(self._texto_de.get(src, ""))):
                self._df_palavra[w] = self._df_palavra.get(w, 0) + 1

    def derivada(self) -> "MalhaIndex":
        """Cópia BARATA deste índice, para receber por cima o vault de UM dono.

        Rasa de propósito: os dicionários são novos (o dono nunca contamina o acervo),
        mas os `set` de portadores, as listas de conceitos e as strings dos átomos são
        COMPARTILHADOS até alguém escrever — e quem escreve é o `estender`, que troca
        o objeto inteiro em vez de mutá-lo. Com 4 donos, é a diferença entre 4 cópias
        dos ~25k átomos do acervo em RAM e uma só; em CPU, é não repetir os ~3 s de
        montagem da malha do acervo a cada usuário."""
        nova = MalhaIndex()
        nova._por_conceito = dict(self._por_conceito)
        nova._conceitos_de = dict(self._conceitos_de)
        nova._texto_de = dict(self._texto_de)
        nova._meta_de = dict(self._meta_de)
        nova._df_palavra = dict(self._df_palavra)
        return nova

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

    def vizinhanca(self, semente: str, saltos: int, max_nos: int, idf_min: float,
                   max_arestas: int = 0) -> dict:
        """Grafo LOCAL em volta de uma nota (painel avançado). Delega ao `grafo`,
        pela mesma razão que `pontes`: lógica de grafo é pura e não mora na camada
        de dados. Malha não construída devolve grafo vazio, não erro."""
        return grafo.vizinhanca_local(
            self._por_conceito, self._conceitos_de, self.idf,
            semente, saltos, max_nos, idf_min, max_arestas,
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
    # ANEXOS: notas de figura ATTACH-ONLY, que acompanham um átomo usado sem nunca
    # terem competido na busca (co-locação — ver `_anexos_colocados`). Ficam FORA
    # de `fontes` de propósito: elas não entram no contexto do LLM e não devem
    # disparar a promoção do #conhecimento_novo, que mede reuso de CONHECIMENTO.
    # O que elas fazem é uma coisa só: virar imagem na tela.
    anexos: List[str] = field(default_factory=list)


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

    def unload(self) -> None:
        """Solta o e5 da VRAM (Fase 3: GPU limpa p/ o OCR em outro processo). O
        `load` é idempotente, então restaurar é só chamá-lo de novo."""
        if self._embeddings is None:
            return
        self._embeddings = None
        vram.liberar_cache_gpu()

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
# Multiusuário: a fronteira é a COLEÇÃO, não um `where`
# ==========================================================================
# POR QUE NÃO `dono` NO METADADO + filtro — as duas medições de 2026-08-03 que
# fecharam a decisão, ambas sobre ESTE índice (25.671 chunks):
#
# (1) `_buscar_texto` é fail-open DE PROPÓSITO: se o filtro der erro OU vier vazio,
#     ele refaz a busca SEM filtro nenhum. Isso salvou a busca numa base ainda em
#     meta_v<4 (sem o campo `tipo`) e está lá comentado como sobrevivência. Com o
#     dono num `where`, o MESMO mecanismo vira o vazamento: o usuário cujo filtro
#     não casasse nada cairia numa busca sobre o vault inteiro — e a resposta viria
#     bonita, aterrada e com a memória de outra pessoa.
# (2) dos 25.671 chunks, 5 NÃO têm a chave `origem` — e são justamente
#     `Inbox_Captura.md`, `Lista_compras.md` e as notas que as ferramentas salvam à
#     mão. Chave AUSENTE não casa `where` em sentido nenhum: é a war story do
#     `acervo_suspeito` (ver `metadados_da_nota`), agora com privacidade em jogo.
#
# O único metadado presente em 100% dos chunks é `source`, o CAMINHO — e caminho
# ninguém esquece de gravar, porque escrever o arquivo obriga a escolher a pasta.
# Daí: pasta separada no vault, COLEÇÃO separada no Chroma (mesma persist_directory,
# mesmo embedding, mesmo `hnsw:space`), e o dono deixa de ser um filtro que dá para
# esquecer de aplicar. O que não está no escopo simplesmente não é consultado.
_avisou_config_incompleta = False


def multiusuario_ligado() -> bool:
    """O PONTO ÚNICO da decisão. Desligado (o default), TUDO abaixo vira no-op e o
    RAG se comporta byte a byte como antes: uma coleção só, com o nome de sempre
    ('langchain', o default do langchain-chroma), zero fan-in, zero cache por dono.

    O `getattr` não é frouxidão, é o oposto. O campo EXISTE (`Settings.
    multiusuario_habilitado`); se um dia sumir num rename, ler `settings.x` direto
    derrubaria TODA busca com AttributeError, e engolir a ausência em silêncio repetiria
    a war story deste projeto (`..._SEGUNDOS` em vez de `..._SECONDS`, engolido pelo
    `extra="ignore"` do pydantic: uma função que o dono acreditou ter ligado e passou
    meses desligada). Aqui a ausência não derruba e não cala — ela GRITA no log, uma
    vez, e o RAG segue no modo de um usuário só."""
    global _avisou_config_incompleta
    ligado = getattr(settings, "multiusuario_habilitado", None)
    if ligado is None:
        if not _avisou_config_incompleta:
            _avisou_config_incompleta = True
            telemetry.warn(
                "DB",
                "config sem `multiusuario_habilitado` — RAG seguindo em coleção ÚNICA "
                "(modo de um usuário só). Se você esperava separação por dono, ela NÃO "
                "está no ar.",
            )
        return False
    return bool(ligado)


def repartir_por_raiz(caminhos: Sequence[str],
                      raizes: Sequence[str]) -> Tuple[dict, List[str]]:
    """Distribui cada arquivo para a RAIZ que o contém. Puro/testável (sem IO).

    Devolve `(por_raiz, sobras)`. `sobras` é a metade que importa: arquivo que não
    cai em raiz nenhuma não pertence a coleção nenhuma e some da busca — e sumir em
    silêncio é a falha que este projeto já pagou duas vezes (attach_only e
    acervo_suspeito, ambas "declaração que ninguém lia").

    Raiz mais LONGA primeiro, porque as raízes se aninham: `<vault>/Pessoal/daniel`
    tem de vencer `<vault>` quando as duas casam. Com UMA raiz (o vault inteiro, que
    é o modo de um usuário só) tudo cai nela e `sobras` é vazia por construção."""
    normal = sorted(
        ((r, os.path.normcase(os.path.abspath(r))) for r in raizes),
        key=lambda rn: -len(rn[1]),
    )
    por_raiz: dict = {r: [] for r in raizes}
    sobras: List[str] = []
    for caminho in caminhos:
        ap = os.path.normcase(os.path.abspath(caminho))
        for r, base in normal:
            if ap == base or ap.startswith(base + os.sep):
                por_raiz[r].append(caminho)
                break
        else:
            sobras.append(caminho)
    return por_raiz, sobras


def lojas_do_escopo(escopo, base) -> list:
    """As coleções a consultar: as do `escopo` que o chamador já resolveu, ou só a BASE.

    Pura, e recebe tudo o que usa por argumento — o `search` resolve o escopo UMA vez e
    o repassa às etapas seguintes (irmãos de página, anexo por co-locação), em vez de
    cada uma redescobrir de quem é o turno. Sem escopo, o padrão é a base sozinha:
    degrada para MENOS informação (só o acervo comum), nunca para a de outra pessoa —
    a única direção em que errar aqui é aceitável."""
    if escopo is not None:
        return [loja for _chave, loja in escopo]
    return [base] if base is not None else []


def fundir_por_distancia(partes: Sequence[Sequence[tuple]], k: int) -> list:
    """Funde os resultados `(doc, score)` de N coleções num top-k único. Puro/testável.

    POR QUE PEDIR `k` EM CADA COLEÇÃO E CORTAR EM `k` NO FIM (e não `k/N` em cada):
    a união dos top-k de uma partição CONTÉM o top-k do conjunto todo, então este
    resultado é exatamente o que uma coleção única devolveria — nada de "o acervo
    perdeu metade das vagas porque existe uma coleção pessoal, mesmo vazia". Isso é o
    que preserva a calibração: `rag_score_confident` (0,16 no e5) é um limiar de
    DISTÂNCIA, não de posição — um top-k enviesado por cota mudaria o que entra no
    contexto sem mudar número nenhum de config. O custo é N buscas HNSW em vez de
    uma, cada uma contra um índice menor.

    Uma parte só devolve a lista INTACTA (nem ordena, nem corta): é o modo de um
    usuário só, e ali o contrato é ser indistinguível do código anterior."""
    partes = [p for p in partes if p]
    if len(partes) <= 1:
        return list(partes[0]) if partes else []
    juntos = [par for parte in partes for par in parte]
    juntos.sort(key=lambda ds: ds[1])
    return juntos[:k]


# ==========================================================================
# VectorStore (ChromaDB)
# ==========================================================================
class _FingerprintMudou(RuntimeError):
    """Índice construído com OUTRO embedding/prefixos que os da config atual —
    seguir usando degradaria a recuperação em silêncio (mesma dimensão não estoura
    erro nenhum no Chroma). Levantada no open() para reusar o caminho do #33."""


class _IndiceFiguras:
    """As figuras em RAM para busca EXATA — HNSW é maquinário errado para 1.861 itens.

    O DEFEITO MEDIDO (2026-07-31, índice de produção com 24.925 chunks e 1.861 figuras):
    a busca aproximada com `where={"tipo":"figura"}` erra o PRIMEIRO colocado em 30% das
    perguntas. "tem foto de uma capivara?" tem a nota certa a 0,1319 e o Chroma devolvia
    0,1673 — a capivara não aparecia nem no top-300. A causa é mecânica: o hnswlib usa
    `ef = max(ef_search, n_results)`, e pedir 12 resultados explora o grafo raso demais
    para alcançar um ponto ISOLADO (uma capivara num acervo de cannabis tem poucos
    vizinhos no grafo). Com `n_results=1000` ela reaparece em 1º lugar.

    ⚠ O docstring antigo de `_buscar_figuras` afirmava "recall exato dentro do
    subconjunto (20/20 contra força bruta)". Isso valia para 1.735 figuras e deixou de
    valer; medir de novo depois de crescer a base é o que pegou.

    A curva medida (recall@10 / top-1 exato / ms por query):
        n_results=12 (o que estava no ar) .... 90,5% / 70% / 18,6 ms
        n_results=500 ....................... 98,0% / 90% / 50,4 ms
        n_results=2000 (exaustivo) .......... 100%  / 100% / 140,1 ms
        ESTA CLASSE ......................... 100%  / 100% / **0,22 ms**

    Ou seja: exato E 85x mais rápido que o aproximado, por 5,7 MB de RAM. Faz sentido
    porque 1.861 vetores cabem numa multiplicação de matriz; o índice aproximado existe
    para milhões, e aqui só atrapalha.
    """
    __slots__ = ("_matriz", "_docs")

    def __init__(self, matriz, docs) -> None:
        self._matriz = matriz          # (n, dim), já normalizada por linha
        self._docs = docs

    def __len__(self) -> int:
        return len(self._docs)

    def buscar(self, vetor, k: int) -> List[Tuple[float, object]]:
        """As `k` figuras mais próximas, por cosseno. Exato, sem aproximação."""
        import numpy as np

        v = np.asarray(vetor, dtype="float32")
        norma = float(np.linalg.norm(v))
        if norma == 0.0:
            return []
        distancias = 1.0 - (self._matriz @ (v / norma))
        k = max(1, min(k, len(self._docs)))
        # argpartition e não sort: só o topo importa, e ordenar 1.861 à toa é o
        # tipo de desperdício que some no benchmark e aparece no TTFT.
        topo = np.argpartition(distancias, k - 1)[:k]
        return sorted(((float(distancias[i]), self._docs[i]) for i in topo),
                      key=lambda par: par[0])


def montar_indice_figuras(store) -> Optional[_IndiceFiguras]:
    """Lê as figuras do Chroma e monta a matriz. Fail-soft: None mantém o caminho antigo.

    Síncrona — o chamador já está em `asyncio.to_thread`. Custo medido: 76 ms para as
    1.861, uma vez por `sync()`.
    """
    try:
        import numpy as np

        col = store._collection
        got = col.get(where={"tipo": "figura"}, include=["embeddings", "metadatas", "documents"])
        embs = got.get("embeddings")
        if embs is None or len(embs) == 0:
            return None
        matriz = np.asarray(embs, dtype="float32")
        matriz /= (np.linalg.norm(matriz, axis=1, keepdims=True) + 1e-12)
        docs = [_DocSimples(t or "", m or {})
                for t, m in zip(got.get("documents") or [], got.get("metadatas") or [])]
        if len(docs) != matriz.shape[0]:
            return None          # desalinhado: melhor o caminho antigo que a figura errada
        return _IndiceFiguras(matriz, docs)
    except Exception as exc:
        telemetry.error("FIGURAS", "Índice exato de figuras indisponível (seguindo no ANN)", exc)
        return None


class VectorStore:
    def __init__(self, embeddings: EmbeddingProvider) -> None:
        self._embeddings = embeddings
        self._store = None
        # Índice EXATO das figuras, montado sob demanda e zerado a cada `sync`. Lazy
        # porque o custo (76 ms medidos) não deve entrar no boot de quem nunca pede
        # imagem — e a primeira pergunta com figura já o paga uma vez só.
        self._idx_figuras: Optional["_IndiceFiguras"] = None
        self._write_lock = asyncio.Lock()  # era chroma_write_lock
        # A malha é montada em DOIS caminhos (fim do open e fim do sync) e, desde que o
        # boot a manda para segundo plano, os dois podem se cruzar — duas threads
        # mutando o MESMO MalhaIndex. O lock as serializa; sem ele o índice de conceitos
        # ficaria pela metade em silêncio (fail-soft), que é o pior modo de falhar.
        self._malha_lock = asyncio.Lock()
        self._recuperado_ja = False  # #33: recupera índice no máximo 1x por processo
        # A malha base e os caches do multiusuário (`_pessoais`, `_malhas_dono`,
        # `_idx_figuras_dono`, `_pessoais_lock`) NÃO nascem aqui — ver `_preguicoso`.

    def _preguicoso(self, nome: str, fabrica):
        """Campo criado na 1ª necessidade, por INSTÂNCIA.

        Não é elegância: meia dúzia de testes monta o VectorStore por
        `VectorStore.__new__` justamente para não tocar disco nem embedding, e
        preenche à mão só os campos que o caminho sob teste usa. Um cache que só
        nascesse no `__init__` transformaria esse atalho legítimo — e todo fake que
        alguém escrever depois — em AttributeError num ponto que não tem nada a ver
        com o que o teste afirma. Atributo de CLASSE resolveria o AttributeError e
        criaria coisa pior: um dicionário mutável compartilhado por todas as
        instâncias, ou seja, o vault de um usuário no cache de outro."""
        valor = self.__dict__.get(nome)
        if valor is None:
            valor = self.__dict__[nome] = fabrica()
        return valor

    @property
    def _malha_base(self) -> MalhaIndex:
        """O índice de conceitos do ESCOPO BASE (o acervo, ou o vault inteiro no modo
        de um usuário só). É dele que as malhas por dono derivam."""
        return self._preguicoso("_cache_malha_base", MalhaIndex)

    @property
    def _pessoais(self) -> dict:
        """Coleções pessoais abertas, por dono. Abrir uma coleção do Chroma custa IO e
        o dono pergunta dezenas de vezes por sessão — daí o cache."""
        return self._preguicoso("_cache_pessoais", dict)

    @property
    def _pessoais_lock(self) -> asyncio.Lock:
        """Serializa a abertura: dois turnos simultâneos do mesmo dono abririam duas."""
        return self._preguicoso("_cache_pessoais_lock", asyncio.Lock)

    @property
    def _malhas_dono(self) -> dict:
        """Malha por dono (acervo + vault dele). Derivada da base — ver `derivada`."""
        return self._preguicoso("_cache_malhas_dono", dict)

    @property
    def _idx_figuras_dono(self) -> dict:
        """Matriz exata de figuras por dono. A do acervo segue em `_idx_figuras`."""
        return self._preguicoso("_cache_idx_figuras_dono", dict)

    @property
    def ready(self) -> bool:
        return self._store is not None

    @property
    def malha(self) -> MalhaIndex:
        """O índice de conceitos DO ESCOPO de quem está perguntando.

        É propriedade e não atributo por uma razão só: `search` (aterramento por IDF,
        expansão por conceito), `buscar_conteudos` (hubs primeiro) e o painel de
        conexões em `comandos_mestre` leem `vectorstore.malha` — e no multiusuário a
        resposta depende de QUEM pergunta. Concentrar isso aqui evita reescrever cada
        chamador e, mais importante, evita que um deles fique para trás lendo a malha
        errada, que é vazamento silencioso.

        Sem multiusuário devolve a de sempre. Com multiusuário e sem dono no contexto
        (trabalho de fundo, boot), devolve a do ACERVO — nunca a do último usuário.
        Dono ainda sem malha montada cai na do acervo, que é degradação honesta: perde
        a vizinhança das notas dele, não enxerga a de ninguém."""
        base = self._malha_base
        if not multiusuario_ligado():
            return base
        dono = identidade.dono_atual()
        return self._malhas_dono.get(dono or "") or base

    @malha.setter
    def malha(self, valor) -> None:
        """Escrever em `.malha` escreve na BASE (o acervo). Quem faz isso é o boot e
        os testes; as malhas por dono derivam dela e são reconstruídas pelo sync."""
        self.__dict__["_cache_malha_base"] = valor

    def load_embeddings(self) -> None:
        """Síncrono — chamar via asyncio.to_thread no startup."""
        self._embeddings.load()

    def _construir_store(self, colecao: Optional[str] = None):
        """Constrói o cliente Chroma (síncrono — chamar via to_thread). Isolado do
        open() para o caminho de auto-recuperação poder reabrir sem duplicar código.

        `colecao=None` NÃO passa o argumento: o default do langchain-chroma
        ('langchain') continua sendo o nome, então o modo de um usuário só abre
        exatamente a coleção que já está no disco — 25.671 embeddings, 468 MB, que
        nenhuma renomeação vai reindexar. Todas as coleções compartilham a MESMA
        `persist_directory` e os MESMOS `collection_metadata` (cosseno + fingerprint),
        porque distância e embedding precisam ser idênticos para as distâncias de
        coleções diferentes serem comparáveis no fan-in."""
        from langchain_chroma import Chroma

        extra = {"collection_name": colecao} if colecao else {}
        return Chroma(
            **extra,
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

    async def _abrir_e_provar(self, colecao: Optional[str] = None):
        """Abre o Chroma E o PROVA com uma leitura mínima. A prova é o que
        distingue 'abriu' de 'abriu íntegro': um HNSW/sqlite corrompido às vezes
        constrói o objeto e só estoura na 1ª leitura. Levanta se algo falhar."""
        store = await asyncio.to_thread(self._construir_store, colecao)
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

    async def open(self, com_malha: bool = True) -> None:
        """Abre o Chroma. `com_malha=False` deixa o índice de conceitos para o
        chamador montar em segundo plano (ver `reconstruir_malha`) — é o boot
        devolvendo 3,09 s ao caminho crítico."""
        if self._embeddings.instance is None:
            telemetry.warn("DB", "VectorStore sem embeddings — indexação desativada.")
            return
        # Reabrir troca o cliente Chroma: as coleções pessoais cacheadas apontam para o
        # cliente ANTIGO (no caminho de auto-recuperação, para um diretório movido para
        # o lado). Zerar aqui é o que impede uma busca pós-recuperação de ler um banco
        # que já não é o banco.
        self._pessoais.clear()
        self._malhas_dono.clear()
        self._idx_figuras_dono.clear()
        async with self._write_lock:
            try:
                store = await self._abrir_e_provar(self._colecao_base())
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
                    self._store = await self._abrir_e_provar(self._colecao_base())
                    telemetry.track("DB", "Índice recuperado: banco vazio reaberto, sync reconstrói do vault.")
                except Exception as exc2:
                    telemetry.error("DB", "Recuperação do índice falhou", exc2)
                    return
        if self._store is not None and com_malha:
            await self._reconstruir_malha()

    # ----------------------------------------------------------------------
    # Escopo: quais coleções esta leitura enxerga
    # ----------------------------------------------------------------------
    def _colecao_base(self) -> Optional[str]:
        """O nome da coleção BASE: a do acervo comum no multiusuário, e `None` (= o
        default 'langchain') no modo de um usuário só, para não renomear o que já
        está no disco."""
        return settings.colecao_acervo if multiusuario_ligado() else None

    async def _loja_pessoal(self, dono: str):
        """A coleção pessoal de `dono`, aberta sob demanda e cacheada. None se não deu.

        Sob demanda porque abrir coleção custa IO e a maioria dos processos (boot,
        scripts, trabalho de fundo) nunca lê dado pessoal de ninguém. O lock evita que
        dois turnos simultâneos do mesmo dono abram duas.

        FALHA É PEGAJOSA (cacheia o None) de propósito: sem isso, uma coleção que não
        abre viraria uma tentativa cara de IO a CADA pergunta. O erro vai alto no log,
        e o turno segue enxergando só o acervo — degrada para MENOS informação, nunca
        para a de outra pessoa."""
        if self._store is None or not dono:
            return None
        if dono in self._pessoais:
            return self._pessoais[dono]
        async with self._pessoais_lock:
            if dono in self._pessoais:      # outra corrotina abriu enquanto esperávamos
                return self._pessoais[dono]
            try:
                loja = await self._abrir_e_provar(settings.colecao_pessoal(dono))
                if not await asyncio.to_thread(self._fingerprint_ok, loja):
                    # Sem auto-recuperação aqui: ela move o DIRETÓRIO inteiro para o
                    # lado, o que levaria junto o acervo e as coleções dos outros. O
                    # caso real (config de embedding trocada entre execuções) já é
                    # pego no `open()`, que roda antes e cobre o banco todo.
                    raise _FingerprintMudou(
                        f"coleção pessoal de '{dono}' construída com outro embedding")
                self._pessoais[dono] = loja
            except Exception as exc:
                telemetry.error(
                    "DB", f"Coleção pessoal de '{dono}' indisponível — os turnos dele "
                          f"enxergam só o acervo comum até reiniciar", exc)
                self._pessoais[dono] = None
                return None
        await self._reconstruir_malha_dono(dono)
        return self._pessoais[dono]

    async def _escopo(self) -> List[Tuple[str, object]]:
        """(chave, coleção) de tudo que esta leitura enxerga: o acervo comum e, quando
        há dono no contexto, a coleção pessoal dele. É o fan-in inteiro num lugar só.

        A `chave` ("" para a base, o nome do dono para a pessoal) é o que indexa os
        caches em RAM — malha e matriz de figuras —, para trocar de usuário não
        remontar nada.

        SEM DONO DEFINIDO devolve só o acervo. Nunca "o do último usuário", nunca "o de
        todos": é a mesma escolha do `identidade.dono_atual`, em que a ausência de dono
        NEGA o dado pessoal em vez de adivinhar de quem ele é."""
        if self._store is None:
            return []
        if not multiusuario_ligado():
            return [("", self._store)]
        escopo: List[Tuple[str, object]] = [("", self._store)]
        dono = identidade.dono_atual()
        if dono:
            pessoal = await self._loja_pessoal(dono)
            if pessoal is not None:
                escopo.append((dono, pessoal))
        return escopo

    async def reconstruir_malha(self) -> None:
        """Porta pública da malha, para o BOOT montá-la em segundo plano.

        Medido em 2026-07-29: a malha custa 3,09 s dos 17,79 s de boot, e o servidor
        não precisa dela para atender — sem malha construída (`n_atomos == 0`) o
        `search` mantém o aterramento léxico original (o OR sem IDF) e a expansão por
        conceito só não acontece. Degradação graciosa que o código já tratava."""
        await self._reconstruir_malha()

    async def _reconstruir_malha(self) -> None:
        """Recarrega o índice de conceitos do que está no Chroma.

        Chamado no open (o índice vive em RAM, some com o processo) e no fim do sync
        (nota nova = conceito novo; sem isto a expansão ficaria olhando um retrato
        velho da base). Nunca derruba a busca: sem malha, a expansão só não acontece.

        LÊ EM LOTES de propósito. O `get()` sem limite vira um SQL com uma variável
        por registro, e o SQLite recusa acima de ~32k ("too many SQL variables").
        Medido em 2026-07-26: com 31.450 chunks a malha montava; as 1.736 notas de
        figura levaram a 33.396 e ela passou a estourar — falha silenciosa, porque
        aqui é fail-soft e só a expansão por conceito morria.
        """
        if self._store is None:
            return
        async with self._malha_lock:
            base = self._malha_base
            try:
                dump = await asyncio.to_thread(
                    dump_paginado, self._store, ["documents", "metadatas"]
                )
                await asyncio.to_thread(
                    base.construir,
                    dump.get("documents") or [],
                    dump.get("metadatas") or [],
                )
                telemetry.track(
                    "MALHA",
                    f"Índice de conceitos: {base.n_conceitos} conceitos "
                    f"em {base.n_atomos} átomos.",
                )
            except Exception as exc:
                telemetry.error("MALHA", "Falha ao montar índice de conceitos", exc)
                return
            # As malhas por dono DERIVAM da base — reconstruí-la invalida todas elas.
            # Refazer só as dos donos com coleção JÁ ABERTA: quem não perguntou neste
            # processo monta a sua na primeira pergunta (ver `_loja_pessoal`).
            for dono in [d for d, loja in self._pessoais.items() if loja is not None]:
                await self._montar_malha_dono(dono)

    async def _reconstruir_malha_dono(self, dono: str) -> None:
        """A malha de UM dono (o acervo + o vault pessoal dele). Serializada pelo mesmo
        lock da base: sem ele, derivar de uma base sendo reescrita daria um índice pela
        metade — e a malha é fail-soft, então isso morreria calado."""
        async with self._malha_lock:
            await self._montar_malha_dono(dono)

    async def _montar_malha_dono(self, dono: str) -> None:
        """Idem, JÁ com o `_malha_lock` na mão (o lock não é reentrante)."""
        loja = self._pessoais.get(dono)
        if loja is None:
            return
        try:
            dump = await asyncio.to_thread(
                dump_paginado, loja, ["documents", "metadatas"]
            )
            malha = self._malha_base.derivada()
            await asyncio.to_thread(
                malha.estender,
                dump.get("documents") or [],
                dump.get("metadatas") or [],
            )
            self._malhas_dono[dono] = malha
            telemetry.track(
                "MALHA",
                f"Índice de conceitos de '{dono}': {malha.n_conceitos} conceitos "
                f"em {malha.n_atomos} átomos (acervo + pessoal).",
            )
        except Exception as exc:
            # Fail-soft como a base: sem malha do dono, `malha` cai na do acervo e a
            # expansão por conceito só não alcança as notas pessoais dele.
            telemetry.error("MALHA", f"Falha ao montar a malha de '{dono}'", exc)

    async def _escopos_de_indexacao(self) -> List[Tuple[object, str]]:
        """(coleção, raiz no vault) que o `sync` varre.

        Um usuário só: a coleção de sempre e o vault INTEIRO — idêntico ao de antes.
        Multiusuário: `Acervo/` na coleção do acervo, e cada `Pessoal/<dono>/` na
        coleção daquele dono. A lista de donos sai das PASTAS que existem, não de um
        campo de config: a pasta é a fronteira (é justamente o que o `where` não sabia
        ser), então é ela também quem sabe quem existe."""
        if not multiusuario_ligado():
            return [(self._store, settings.caminho_obsidian)]
        escopos: List[Tuple[object, str]] = [(self._store, str(settings.caminho_acervo))]
        raiz_pessoal = os.path.join(settings.caminho_obsidian, settings.subpasta_pessoal)
        try:
            nomes = sorted(e.name for e in os.scandir(raiz_pessoal) if e.is_dir())
        except OSError:
            nomes = []       # pasta ainda não criada: não há vault pessoal a varrer
        for nome in nomes:
            # Nome de pasta que não é um identificador de dono (acento, espaço, "..")
            # não vira coleção — `identidade.normalizar` recusa adivinhar, e adivinhar
            # aqui seria o servidor escolhendo em que coleção a memória de alguém cai.
            if not (identidade.valido(nome) and identidade.normalizar(nome) == nome):
                telemetry.warn(
                    "DB", f"'{nome}' em {raiz_pessoal} não é um nome de dono válido — "
                          f"as notas dessa pasta NÃO são indexadas.")
                continue
            loja = await self._loja_pessoal(nome)
            if loja is not None:
                escopos.append((loja, str(settings.caminho_pessoal(nome))))
        return escopos

    async def sync(self) -> bool:
        """Reindex incremental por mtime (novos + modificados) e por `meta_v`
        (notas indexadas com esquema de metadado antigo — ver `_META_VERSAO`).

        Devolve **se reconstruiu a malha**. Serve ao chamador que já ia construí-la:
        o `_malha_e_sync` do boot fazia malha→sync, e o `sync` refazia a malha no
        fim — a primeira era jogada fora segundos depois toda vez que houvesse algo
        a indexar (5,53 s medidos num boot real em 2026-08-02). Com o retorno, ele
        constrói só quando este aqui não construiu.

        No multiusuário a varredura é POR ESCOPO (ver `_escopos_de_indexacao`), mas o
        `glob` do vault continua sendo UM só: varrer 25k arquivos por dono seria pagar
        a parte cara N vezes para depois jogar fora N-1. Ele é repartido em memória
        (`repartir_por_raiz`), que também é o que denuncia a nota fora de todo escopo."""
        if self._store is None:
            await self.open()
        if self._store is None:
            return False
        # INVALIDA O ÍNDICE EXATO DE FIGURAS logo na entrada, e não nos pontos em que o
        # sync grava: `sync` tem várias saídas antecipadas ("nada novo", erro, lock), e
        # esquecer UMA delas deixaria a figura nova invisível até o próximo restart —
        # falha silenciosa, do tipo que só aparece semanas depois. Invalidar aqui custa
        # no máximo uma remontagem de 76 ms por sync, mesmo quando nada mudou.
        self._idx_figuras = None
        self._idx_figuras_dono.clear()
        try:
            async with self._write_lock:
                escopos = await self._escopos_de_indexacao()
                arquivos = glob.glob(
                    os.path.join(settings.caminho_obsidian, "**/*.md"), recursive=True
                )
                por_raiz, sobras = repartir_por_raiz(
                    arquivos, [raiz for _loja, raiz in escopos])
                if sobras:
                    # Nota que não está em `Acervo/` nem em `Pessoal/<dono>/` não
                    # pertence a coleção nenhuma — logo, é INVISÍVEL para toda busca.
                    # Isto é o aviso que a migração do vault ainda não terminou; sem
                    # ele, a base encolheria em silêncio, que é como este projeto já
                    # perdeu figura (attach_only) e nota (acervo_suspeito) antes.
                    telemetry.warn(
                        "DB",
                        f"{len(sobras)} nota(s) fora de '{settings.subpasta_acervo}/' e de "
                        f"'{settings.subpasta_pessoal}/<dono>/' — NÃO indexadas, invisíveis "
                        f"para a busca (ex.: {os.path.basename(sobras[0])}).",
                    )
                mudou = False
                for loja, raiz in escopos:
                    if await self._sync_escopo(loja, raiz, por_raiz.get(raiz, [])):
                        mudou = True
                if not mudou:
                    return False
            # Fora do write_lock: _reconstruir_malha só LÊ, e o lock não é reentrante.
            await self._reconstruir_malha()
            return True
        except Exception as exc:
            telemetry.error("DB", "Erro na sincronização do VectorDB", exc)
            # False e não None: quem perguntou "você construiu a malha?" recebe um
            # "não" honesto e constrói por conta — o pior desfecho de um sync que
            # falhou seria o boot seguir SEM índice de conceitos achando que tem.
            return False

    async def _sync_escopo(self, loja, raiz: str, arquivos: List[str]) -> bool:
        """A varredura incremental de UMA coleção contra UMA raiz do vault.

        É o corpo histórico do `sync` — o que mudou foi passar a loja e a raiz como
        parâmetros, para o multiusuário rodá-lo uma vez por escopo. Devolve **se o
        índice mudou** (gravou ou removeu algo); o chamador usa isso para decidir se
        vale remontar a malha. Levanta em erro: quem trata é o `sync`."""
        existing = await asyncio.to_thread(dump_paginado, loja, ["metadatas"])
        # PURGA DE ÓRFÃOS: chunks cujo `source` não vive mais sob a raiz DESTE escopo
        # (ou sumiu do disco). Conserta o lixo deixado quando o CAMINHO do vault
        # muda — ex.: a migração de `.../Desktop/projetos/memoria_vetorial/...`
        # para a pasta do projeto duplicava TODA nota no Chroma (source velho +
        # novo), pois o delete-by-source do reindex só casa strings idênticas.
        # No multiusuário ela ganha um segundo ofício, de graça: a nota que MUDOU de
        # dono (a pasta de A para a de B) some da coleção de A por ser órfã lá, e
        # entra na de B como arquivo novo. Sem isso ela responderia para os dois.
        base = os.path.normcase(os.path.abspath(raiz))
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
                lambda s=src: loja.delete(where={"source": s})
            )
        if orfaos:
            telemetry.track(
                "DB", f"Purga: {len(orfaos)} fontes órfãs removidas (vault movido/nota apagada)."
            )
            # Métricas do ciclo (painel): purga persistida como evento.
            await asyncio.to_thread(
                db.log_etl, "PURGA_ORFAOS", f"{len(orfaos)} fonte(s)", "removidas"
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
            return False

        # remove versões velhas dos arquivos modificados (evita duplicata).
        # Só quem JÁ estava no índice remove algo — e contamos, porque essa
        # contagem é metade do veredito "o índice mudou?" logo abaixo.
        removidos = 0
        for path, _ in pendentes:
            if path in indexado:
                await asyncio.to_thread(
                    lambda p=path: loja.delete(where={"source": p})
                )
                removidos += 1

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

        # PENDENTE ETERNO (medido em 2026-08-02): uma nota que não produz
        # chunk nenhum — arquivo de 0 byte, só frontmatter, só espaço — nunca
        # é gravada, logo nunca ganha entrada no índice, logo volta a ser
        # `pendente` no boot seguinte. PARA SEMPRE. Com isso o atalho "nada
        # novo" acima nunca dispara e o `sync` sempre chegava até aqui e
        # reconstruía a malha à toa: 2,4 s por sync, 2 syncs por boot, todo
        # boot. O vault do dono tinha 9 desses (7 notas vazias, 1 átomo de
        # livro truncado, 1 stub de figura), e apagá-los não resolveria — o
        # importador de figuras gera mais.
        #
        # O conserto NÃO é fingir que o arquivo foi indexado (isso mentiria
        # para a purga de órfãos e para a próxima comparação de mtime). É
        # reconhecer que, sem nada gravado E sem nada removido, o índice está
        # IDÊNTICO ao de antes — e reconstruir a malha sobre um índice
        # idêntico não pode produzir malha diferente.
        if not splits and not removidos:
            telemetry.track(
                "DB",
                f"{len(pendentes)} arquivo(s) sem conteúdo indexável "
                f"(0 chunks, nada removido) — índice inalterado, malha preservada.",
            )
            return False
        for i in range(0, len(splits), settings.chroma_batch):
            await asyncio.to_thread(
                loja.add_documents, splits[i : i + settings.chroma_batch]
            )
        telemetry.track(
            "DB", f"Indexados/atualizados {len(pendentes)} arquivos ({len(splits)} chunks)."
        )
        return True

    async def corpus_com_embeddings(self) -> List[tuple]:
        """Dump (source, texto, embedding) por chunk — p/ a consolidação (Fase 2).
        Fail-open: sem store/erro devolve [] (a consolidação simplesmente não roda).
        Cuidado com o numpy: `embeddings` do Chroma pode vir ndarray — truthiness
        de array levanta, então o teste é `is None`, nunca `or []`."""
        if not self.ready:
            return []
        out: List[tuple] = []
        for _chave, loja in await self._escopo():
            try:
                dump = await asyncio.to_thread(
                    dump_paginado, loja, ["documents", "metadatas", "embeddings"]
                )
            except Exception as exc:
                telemetry.error("RAG", "Falha no dump p/ consolidação", exc)
                return []
            embs = dump.get("embeddings")
            embs = [] if embs is None else embs
            for doc, md, emb in zip(dump.get("documents") or [],
                                    dump.get("metadatas") or [], embs):
                src = str((md or {}).get("source") or "")
                if src and emb is not None:
                    out.append((src, doc, [float(x) for x in emb]))
        return out

    async def remover_fontes(self, fontes: List[str]) -> None:
        """Remove chunks por `source` (consolidação: o .md foi ARQUIVADO fora do
        vault — sem isto o índice seguiria citando arquivos-fantasma).

        Tenta em todas as coleções do escopo: um `source` vive em exatamente uma, e
        o delete das outras é um no-op barato. Se o chamador rodar sem dono no
        contexto (trabalho de fundo), a coleção pessoal fica de fora — e a purga de
        órfãos do próximo `sync` limpa o resto, porque o arquivo já não existe."""
        if not self.ready:
            return
        for _chave, loja in await self._escopo():
            for s in fontes:
                try:
                    await asyncio.to_thread(lambda s=s, lj=loja: lj.delete(where={"source": s}))
                except Exception as exc:
                    telemetry.error("RAG", f"Falha ao remover do índice: {s}", exc)

    async def buscar_conteudos(self, query: str, k: int) -> List[str]:
        """Recuperação CRUA para a Síntese sob Demanda (#23): conteúdo (sem frontmatter)
        dos top-k átomos, deduplicado — SEM gate nem orçamento. O chamador fatia em lotes
        que cabem no n_ctx (map-reduce), então aqui a largura é livre. Vazio sem loja
        (testes) ou query vazia — fail-open, como o resto do RAG."""
        if self._store is None or not query.strip():
            return []
        try:
            partes = [
                await asyncio.to_thread(
                    lambda lj=loja: lj.similarity_search_with_score(query, k=k)
                )
                for _chave, loja in await self._escopo()
            ]
        except Exception as exc:
            telemetry.error("LOCAL", "Falha na busca para síntese", exc)
            return []
        res = fundir_por_distancia(partes, k)
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
            return await self._buscar_texto(consulta.strip(), k or settings.rag_top_k)
        except Exception as exc:
            telemetry.error("LOCAL", "Falha na recuperação especulativa", exc)
            return None

    async def _buscar_texto(self, consulta: str, k: int) -> list:
        """Recupera SÓ texto no ESCOPO de quem pergunta (acervo + vault pessoal dele).

        O fan-in mora aqui, e não em cada chamador, porque este é o funil por onde
        passam `search` e `recuperar` — a fase (b) do extrator inclusive. Cada coleção
        recebe o MESMO pedido de `k`; a fusão por distância e o porquê do `k` estão em
        `fundir_por_distancia`."""
        return fundir_por_distancia(
            [await self._buscar_texto_em(loja, consulta, k)
             for _chave, loja in await self._escopo()],
            k,
        )

    async def _buscar_texto_em(self, loja, consulta: str, k: int) -> list:
        """Recupera SÓ texto de UMA coleção, pedindo o filtro ao Chroma — não
        filtrando depois.

        A diferença não é estilo, é sobrevivência da busca. Medido em 2026-07-26,
        logo após traduzir 635 legendas para PT-BR: as notas de figura ficaram tão
        competitivas que ocuparam os 40 slots INTEIROS numa pergunta sobre magnésio.
        Como o texto é o que ancora a resposta, descartar figura DEPOIS deixava a
        lista vazia e a pergunta ia parar na web — com o vault cheio de átomos bons
        sobre o assunto, a 0,0951 de distância.

        Fail-open em duas camadas, porque uma base ainda em meta_v<4 não tem o campo
        `tipo` e o filtro devolveria vazio: se o filtro der erro OU vier vazio
        enquanto a busca sem filtro acha coisa, vale a busca sem filtro (é o
        comportamento anterior, com o descarte em Python fazendo o resto).

        ⚠ E é exatamente este fail-open que proíbe o dono de ser um `where`: aqui,
        filtro que falha vira busca SEM filtro — o que para o `tipo` é sobrevivência
        e para o dono seria o vault de todo mundo. Ver o bloco `multiusuario_ligado`."""
        try:
            res = await asyncio.to_thread(
                lambda: loja.similarity_search_with_score(
                    consulta, k=k, filter={"tipo": "texto"})
            )
            if res:
                return res
        except Exception as exc:
            telemetry.error("LOCAL", "Filtro de tipo indisponível; busca sem ele", exc)
        return await asyncio.to_thread(
            loja.similarity_search_with_score, consulta, k
        )

    def _indice_figuras_cacheado(self, chave: str) -> Optional["_IndiceFiguras"]:
        """A matriz de figuras JÁ montada daquele escopo, ou None. A do acervo continua
        morando em `_idx_figuras` — é o campo que o `sync` invalida e que os testes
        montam à mão —, e cada dono ganha a sua no dicionário ao lado."""
        if not chave:
            return self._idx_figuras
        return self._idx_figuras_dono.get(chave)

    def _guardar_indice_figuras(self, chave: str, idx: Optional["_IndiceFiguras"]) -> None:
        if not chave:
            self._idx_figuras = idx
        else:
            self._idx_figuras_dono[chave] = idx

    async def _figuras_candidatas(self, consulta: str) -> List[Tuple[object, float]]:
        """Os candidatos brutos, no formato (doc, score) do Chroma.

        Prefere o índice EXATO em memória (`_IndiceFiguras`) e cai no ANN se ele não
        existir — base sem figura, numpy ausente, store fake nos testes. O fallback é o
        que estava no ar até 2026-07-31, então o pior caso é o status quo.

        Uma matriz POR COLEÇÃO (e não uma só, concatenada): as coleções crescem em
        ritmos diferentes e o `sync` invalida escopo a escopo — remontar a do acervo
        (1.861 figuras, 76 ms) porque alguém salvou uma foto no vault pessoal seria
        pagar caro por nada. A fusão é a mesma do texto, por distância.
        """
        escopo = await self._escopo()
        if settings.figuras_busca_exata:
            indices = []
            for chave, loja in escopo:
                idx = self._indice_figuras_cacheado(chave)
                if idx is None:
                    idx = await asyncio.to_thread(montar_indice_figuras, loja)
                    self._guardar_indice_figuras(chave, idx)
                    if idx is not None:
                        telemetry.track("FIGURAS",
                                        f"Índice exato de figuras montado ({len(idx)}).")
                if idx is not None:
                    indices.append(idx)
            if indices:
                vetor = await asyncio.to_thread(
                    self._embeddings.instance.embed_query, consulta)
                partes = [[(doc, dist) for dist, doc in idx.buscar(vetor, settings.figuras_top_k)]
                          for idx in indices]
                return fundir_por_distancia(partes, settings.figuras_top_k)
        partes = [
            await asyncio.to_thread(
                lambda lj=loja: lj.similarity_search_with_score(
                    consulta, k=settings.figuras_top_k, filter={"tipo": "figura"}
                )
            )
            for _chave, loja in escopo
        ]
        return fundir_por_distancia(partes, settings.figuras_top_k)

    async def _buscar_figuras(self, consulta: str, chaves: set) -> List[Tuple[float, object]]:
        """Figuras que passam o gate, num espaço de busca SÓ delas.

        Duas medições de 2026-07-26 justificam o espaço separado. (1) Disputando as
        mesmas vagas, a figura perdia: uma a 0,1239 ficava fora de um top-40 cujo pior
        era 0,1373, porque a busca aproximada não a alcançava no meio de 33k chunks.
        (2) Quando ganhava, ganhava demais: "como podar a planta" tomava 16 das 40
        vagas. Por isso o `where={"tipo": "figura"}`.

        ⚠ CORREÇÃO DE 2026-07-31: este docstring afirmava que, com o filtro, "o recall
        dentro do subconjunto é exato (20/20 contra força bruta nos 1.735 vetores)".
        DEIXOU DE SER VERDADE ao crescer a base — medido em 1.861 figuras, o ANN erra o
        PRIMEIRO colocado em 30% das perguntas. Hoje a busca é EXATA e em memória (ver
        `_IndiceFiguras`), com o ANN como fallback.

        QUANTAS entram é do gate, não de cota: mesma régua do texto (aterramento
        léxico OU confiança semântica). Pergunta que não pede imagem devolve zero;
        pergunta ilustrada devolve quantas merecerem, e o corte final é o orçamento
        de chars lá no `search`.

        Fail-soft em bloco: qualquer erro (inclusive store fake sem `filter`) devolve
        lista vazia — nunca derruba a busca de texto, que é o que responde."""
        if not settings.figuras_no_contexto or self._store is None:
            return []
        limiar = settings.figuras_score_confident or settings.rag_score_confident
        try:
            res = await self._figuras_candidatas(consulta)
        except Exception as exc:
            telemetry.error("FIGURAS", "Busca de figuras falhou (seguindo só com texto)", exc)
            return []
        aprovadas = [
            (score, doc) for doc, score in (res or [])
            # ATTACH-ONLY NÃO DISPUTA VAGA. Até aqui o campo era declaração e não
            # comportamento: ele viajava no job de importação e ninguém o lia. É o
            # descarte que cumpre o achado de 2026-07-27 — a figura sem legenda de
            # verdade (rótulo herdado da seção, ou nenhum) é INVISÍVEL no acervo
            # atual e é isso que a mantém inofensiva; deixá-la concorrer é repetir
            # o caso "tricomas", em que a busca entrega a "menos ruim" porque
            # sempre há alguém abaixo do limiar. Ela volta pelo anexo por
            # co-locação (`_anexos_colocados`), acompanhando o texto da sua página.
            # Filtro em PYTHON, e não no `where` do Chroma: as ~1.735 notas de
            # figura já indexadas não têm o campo, então `{"attach_only": False}`
            # não casaria NENHUMA delas (chave ausente não casa) e a busca de
            # figura zeraria de uma vez. O custo é gastar slots do top_k.
            if not (doc.metadata or {}).get("attach_only")
            # SUSPEITA NÃO DISPUTA VAGA (2026-07-31): a revalidação do idle já sabe
            # que a imagem não é o que a nota diz; deixá-la concorrer é entregar a
            # figura errada com a confiança da certa. Filtro em Python pelo mesmo
            # motivo do attach_only — a maioria das notas não tem o campo.
            and not (doc.metadata or {}).get("acervo_suspeito")
            and score < settings.rag_score_max
            and (score < limiar or textutils.contem_alguma(doc.page_content, chaves))
        ]
        aprovadas.sort(key=lambda x: x[0])
        # CORTE RELATIVO À MELHOR FIGURA. O limiar absoluto sozinho não sabe DESISTIR:
        # num acervo de 1.735 imagens sempre há alguém abaixo dele, então uma pergunta
        # sem figura boa recebia a "menos ruim" (medido: "tricomas", que o acervo não
        # cobre, trouxe duas fotos de barbeiro). Ancorar na MELHOR figura da própria
        # pergunta transforma o gate em "estas são tão boas quanto a melhor?" — e foi
        # o único denominador que separou nos dados (ver figuras_margem_melhor).
        margem = settings.figuras_margem_melhor
        if margem > 0 and aprovadas:
            teto = aprovadas[0][0] * margem
            aprovadas = [(s, d) for s, d in aprovadas if s <= teto]
        return aprovadas

    async def _irmaos_de_pagina(self, docs: Sequence[object], vistos: set,
                                escopo=None) -> List[Tuple[Optional[float], object]]:
        """Os outros átomos da MESMA PÁGINA dos que casaram (H2, 2026-07-29).

        Por que existe: a re-atomização com o 8B fatiou o livro mais fino — 8.296
        átomos onde o 4B fez 4.739 sobre o MESMO texto — e o fato passou a viver
        separado do seu contexto. Medido em 14 perguntas reais, a base nova
        entrega 503 palavras de conteúdo distintas por contexto contra 604 da
        antiga, cobrindo o mesmo número de páginas: não falta cobertura, falta
        COMPLETUDE dentro da página que já casou.

        Isto recompõe a página sem reescrever átomo nenhum: se um átomo da página
        respondeu, seus irmãos entram atrás dele na fila do orçamento. É a mesma
        âncora e a mesma mecânica de `_anexos_colocados` — co-locação por
        construção, não por proximidade estimada.

        Figura fica de FORA: ela tem porta própria (`_buscar_figuras`) e teto
        próprio; deixá-la entrar por aqui devolveria o problema que o teto
        acabou de resolver.

        Fail-soft: qualquer erro devolve lista vazia e a busca segue como antes.
        """
        teto = settings.rag_max_irmaos
        if teto <= 0 or self._store is None:
            return []
        origens: List[str] = []
        descartadas = 0
        for d in docs:
            o = str((getattr(d, "metadata", None) or {}).get("origem") or "").strip()
            if not o or o in origens:
                continue
            # SÓ PÁGINA DE VERDADE. `origem` é a âncora de co-locação, mas só o
            # `livro.origem_do_job` escreve página; o átomo auto-colhido guarda ali o
            # BALDE de onde veio ('Sintese', 'Conversa', 'Projeto-X.json'). Medido no
            # índice (2026-07-30): baldes de até 1.947 átomos, 54,2% do índice num
            # balde maior que este teto — a expansão entrava CHEIA com 12 átomos
            # escolhidos pela ordem do Chroma. Foi assim que "o que é topping" montou
            # contexto com notas de YOLO: a palavra inglesa só existe nesses baldes.
            if settings.rag_irmaos_so_pagina and not livro.e_origem_de_pagina(o):
                descartadas += 1
                continue
            origens.append(o)
        if descartadas:
            telemetry.track(
                "LOCAL", f"Expansão por página: {descartadas} origem(ns) sem página ignorada(s)."
            )
        if not origens:
            return []
        documentos: List[str] = []
        metadados: List[dict] = []
        try:
            for loja in lojas_do_escopo(escopo, self._store):
                res = await asyncio.to_thread(
                    lambda lj=loja: lj.get(
                        where={"origem": {"$in": origens}},
                        include=["metadatas", "documents"],
                    )
                )
                documentos.extend(res.get("documents") or [])
                metadados.extend(res.get("metadatas") or [])
        except Exception as exc:
            telemetry.error("LOCAL", "Expansão por página indisponível", exc)
            return []
        fora: List[Tuple[Optional[float], object]] = []
        for texto, md in zip(documentos, metadados):
            if not texto or texto in vistos:
                continue
            # Dicionário NOVO, não mutação do que veio do Chroma: a marca é nossa e
            # não deve vazar para quem mais leia esse metadado. Sem ela o rótulo cai
            # em "[Malha - relacionado]" e o irmão chega como vizinho solto — ver
            # `rotular_contexto`.
            doc = _DocSimples(texto, {**(md or {}), IRMAO_DE_PAGINA: True})
            if e_nota_de_figura(doc):
                continue
            vistos.add(texto)
            fora.append((None, doc))
            if len(fora) >= teto:
                break
        return fora

    async def _anexos_colocados(self, docs: Sequence[object],
                                consulta: str = "", escopo=None) -> List[str]:
        """As figuras da MESMA PÁGINA dos átomos que responderam.

        É a outra metade de "encontrável ≠ anexável". A figura sem legenda boa foi
        tirada da busca (`_buscar_figuras`) para não poluir; aqui ela volta pela
        única porta que não pode errar de assunto: ela ilustra o trecho que o
        usuário está lendo. Não há similaridade envolvida — se o átomo da página 3
        do capítulo Soil respondeu, a foto da página 3 do capítulo Soil acompanha.

        A âncora é o `origem` do frontmatter, que já é metadado indexado e é
        gravado IDÊNTICO nos dois lados pela importação web (`Livro 'X' — cap
        (p. N-N)`). Reusar esse campo evita inventar um id de página novo — e é
        exatamente por isso que a página SINTÉTICA existe: co-locação por
        construção, não por proximidade estimada.

        Fail-soft em bloco: qualquer erro (store fake sem `where`, base sem o
        campo) devolve lista vazia. Anexo é bônus visual; nunca derruba resposta."""
        teto = settings.figuras_anexo_max
        if teto <= 0 or self._store is None or not settings.figuras_no_contexto:
            return []
        # SÓ A PÁGINA DO(S) MELHOR(ES) ÁTOMO(S). A premissa acima — "ela ilustra o
        # trecho que o usuário está lendo" — só vale para a página que de fato
        # respondeu. `docs` traz o CONTEXTO INTEIRO (34 átomos de dezenas de páginas),
        # e usar todas fazia a figura vir de qualquer capítulo que tenha contribuído
        # com uma frase.
        #
        # MEDIDO ao vivo em 2026-07-30, 6 perguntas, 6 figuras erradas — é a "mesma
        # sequência de imagens sem relação" que o dono relatava:
        #   "como controlar o oídio?"  -> "Curing: Passo a passo", "Secagem rápida"
        #   "e o fimming?"             -> "Transplante: Etapas"
        #   "me fale sobre HPS"        -> "Salas de Cultivo com Tenda"
        #   "o que é tripes?"          -> "Floração" (o mesmo rótulo de f1 a f13)
        # No caso do oídio a culpada foi "Controle de Moldes Antes da Colheita", um
        # átomo do capítulo de COLHEITA que entrou no contexto por vizinhança e
        # arrastou as fotos da página dele. O átomo que realmente respondeu era
        # `controle_da_mildew` — e as figuras DA PÁGINA DELE seriam pertinentes.
        # ...e só PÁGINA DE VERDADE. `docs[0]` pode ser um átomo auto-colhido, cuja
        # `origem` é o BALDE ('TemaQuente', 'Conversa', 'Projeto-X.json') e não uma
        # página — e aí a âncora se perde sem que nada apareça. Foi o que aconteceu em
        # "o que são tricomas?" (2026-07-31): o melhor átomo era um TemaQuente, e as
        # 49 figuras das páginas 53-54 que responderam ficaram todas de fora.
        origens = []
        for d in docs:
            o = str((getattr(d, "metadata", None) or {}).get("origem") or "").strip()
            if not o or o in origens or not livro.e_origem_de_pagina(o):
                continue
            origens.append(o)
            if len(origens) >= max(1, settings.figuras_anexo_paginas):
                break
        if not origens:
            return []
        # A FIGURA COM LEGENDA TAMBÉM ENTRA POR AQUI (2026-07-31). Antes só a
        # attach-only vinha, e isso deixava de fora exatamente as boas: das 41 figuras
        # de tricoma do acervo, UMA diz "tricoma" em português — as outras 40 dizem
        # "glândulas resiníferas", que é como Cervantes chama a estrutura. Nenhuma
        # busca por embedding alcança essas 40 partindo da palavra que o dono digitou,
        # e nenhuma tabela de sinônimos escala para todo assunto do livro.
        #
        # A co-locação não precisa da palavra: se o átomo da página 54 respondeu, a
        # foto da página 54 ilustra o que ele diz — em qualquer assunto, sem tabela.
        # `figuras_anexo_so_attach_only=true` volta ao comportamento anterior.
        onde = [{"origem": {"$in": origens}}]
        if settings.figuras_anexo_so_attach_only:
            onde.append({"attach_only": True})
        else:
            onde.append({"tipo": "figura"})
        try:
            # ORDENADAS PELA PERGUNTA, não pelo nome do arquivo. Uma página do livro tem
            # até 13 fotos de assuntos diferentes e o teto deixa passar 2: escolher por
            # ordem alfabética entrega a f1 sempre (foi o "mesmo rótulo de f1 a f13" que
            # o dono viu). Aqui a página já garante o ASSUNTO — o ranking só decide qual
            # das fotos dela ilustra melhor, então não há gate de distância nenhum: a
            # figura certa pode não repetir uma palavra sequer da pergunta, que é o
            # defeito inteiro que este caminho existe para contornar.
            if consulta and not settings.figuras_anexo_so_attach_only:
                res = fundir_por_distancia(
                    [await asyncio.to_thread(
                        lambda lj=loja: lj.similarity_search_with_score(
                            consulta, k=max(teto, 8), filter={"$and": onde}))
                     for loja in lojas_do_escopo(escopo, self._store)],
                    max(teto, 8),
                )
                ordenadas = sorted(res or [], key=lambda ds: ds[1])
                # SUSPEITA SAI ANTES DO CORTE RELATIVO (2026-07-31). A ordem importa:
                # descartada depois, ela ainda seria `ordenadas[0]` e ANCORARIA a
                # margem abaixo — uma reprovada em 0,08 puxaria o limite para 0,09 e
                # empurraria para fora a figura BOA em 0,10. O gate não pode deixar a
                # figura que ele mesmo barrou decidir quem mais entra.
                ordenadas = [(d, s) for d, s in ordenadas
                             if not (d.metadata or {}).get("acervo_suspeito")]
                # CORTE RELATIVO À MELHOR, o mesmo remédio do `figuras_margem_melhor`:
                # o teto de 2 preenchia o segundo slot com a próxima da fila mesmo
                # quando ela era muito pior. Medido em 2026-07-31: "o que são
                # tricomas?" trazia a foto certa e, atrás dela, "uma tempestade de
                # chuva forte deitou esse jardim". Os pares BONS ficam (mudas com
                # deficiência de N; lâmpada HPS + seu corte transversal) porque neles
                # a segunda está perto da primeira.
                margem = settings.figuras_margem_melhor
                if margem > 0 and ordenadas:
                    limite = ordenadas[0][1] * margem
                    ordenadas = [(d, s) for d, s in ordenadas if s <= limite]
                saida = []
                for doc, _score in ordenadas:
                    src = str(((doc.metadata or {}).get("source")) or "")
                    if src and src not in saida:
                        saida.append(src)
                return saida[:teto]
            metadados: List[dict] = []
            for loja in lojas_do_escopo(escopo, self._store):
                res = await asyncio.to_thread(
                    lambda lj=loja: lj.get(where={"$and": onde}, include=["metadatas"])
                )
                metadados.extend((res or {}).get("metadatas", []) or [])
        except Exception as exc:
            telemetry.error("FIGURAS", "Anexo por co-locação indisponível", exc)
            return []
        saida: List[str] = []
        for md in metadados:
            if (md or {}).get("acervo_suspeito"):
                continue          # mesma marca, mesmo descarte (ver acima)
            src = str((md or {}).get("source") or "")
            if src and src not in saida:
                saida.append(src)
        # Ordem estável (o nome carrega página e índice) para a resposta não mudar
        # de figura a cada execução com o mesmo contexto.
        saida.sort()
        return saida[:teto]

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
            # UM escopo para o turno inteiro. Resolver de novo em cada etapa (irmãos de
            # página, anexo por co-locação) daria a mesma resposta, mas passaria pelo
            # cache de coleções a cada vez — e, pior, deixaria margem para as etapas
            # discordarem entre si sobre de quem é este turno.
            escopo = await self._escopo()
            # Fase (b) (consultoria #9): `recuperados` chega pronto quando o Agent
            # especulou a recuperação em paralelo com o LLM do extrator. A consulta da
            # especulação é a MESMA pergunta crua que seria embeddada aqui, então o
            # resultado é idêntico ao da linha de baixo — só o instante muda. Lista
            # vazia é resultado legítimo ("especulei e não veio nada"), não fallback.
            res = recuperados if recuperados is not None else await self._buscar_texto(
                consulta, settings.rag_top_k
            )
            if not res:
                return LocalResult(NENHUM, None, False)

            melhor = min(score for _, score in res)
            validos = [(score, doc) for doc, score in res if score < settings.rag_score_max]
            # A FIGURA SAI DO CANAL DE TEXTO — ela tem o seu (`_buscar_figuras`), com
            # gate próprio. Sem isto ela concorre duas vezes e, pior, uma figura sozinha
            # ANCORARIA a resposta (o teste pegou exatamente isso). É também a metade
            # que faltava do defeito medido: "como podar a planta" gastava 16 das 40
            # vagas do texto com imagem.
            # Filtro em PYTHON, e não `where={"tipo":"texto"}`, de propósito: numa base
            # ainda sem o `tipo` (meta_v<4, reindex em curso) o `where` devolveria VAZIO
            # e derrubaria a busca inteira; aqui, nada é removido e vale o comportamento
            # antigo até a migração chegar.
            if settings.figuras_no_contexto:
                validos = [(s, d) for s, d in validos
                           if str((d.metadata or {}).get("tipo") or "") != "figura"]
            # A melhor distância SÓ DE TEXTO, depois de a figura sair do canal. Não dá
            # para reusar `melhor` (linha acima) no gate de página do anexo: ele é o
            # mínimo ANTES deste filtro, então numa pergunta por imagem ele é a própria
            # figura — a comparação viraria a figura contra ela mesma e o gate nunca
            # dispararia. Ver o bloco 'GATE DE PÁGINA' adiante.
            melhor_texto = min((s for s, _ in validos), default=None)
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
            # PRECEDÊNCIA ENTRE OBRAS no desempate de quase-duplicata: quando a
            # mesma ideia está no vault em duas EDIÇÕES (o Cervantes antigo e a
            # Cannabis Encyclopedia nova), quem chegava primeiro no ranking calava
            # a outra — e "primeiro" é a distância, que não sabe qual edição é a
            # atual. Aqui a nova TOMA O LUGAR da velha. Só entre clones: fora do
            # dedup, nenhuma obra ganha vantagem de ranking (ver obras.py).
            preferidas = obras.marcas(settings.obras_preferidas)
            substituiveis = obras.marcas(settings.obras_substituidas)
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
                    gemeo = next(
                        (i for i, t in enumerate(tokens_vistos)
                         if textutils.jaccard(toks, t) >= limiar_dedup), None)
                    if gemeo is not None:
                        # Clone de um átomo já escolhido. Fica o da obra preferida —
                        # o antigo só sai quando o novo VENCE (empate mantém quem
                        # chegou, que é o comportamento histórico).
                        antigo = candidatos[gemeo][1]
                        if not obras.substitui(
                            str((d.metadata or {}).get("origem") or ""),
                            str((antigo.metadata or {}).get("origem") or ""),
                            preferidas, substituiveis,
                        ):
                            continue
                        vistos.discard(antigo.page_content)
                        candidatos[gemeo] = (s, d)
                        tokens_vistos[gemeo] = toks
                        vistos.add(d.page_content)
                        continue
                    tokens_vistos.append(toks)
                vistos.add(d.page_content)
                candidatos.append((s, d))
            candidatos.sort(key=lambda x: x[0])
            relevante = bool(candidatos)

            # FIGURAS — buscadas à parte e só quando o TEXTO já ancorou, pela mesma
            # razão que segura a malha abaixo: uma figura sozinha não pode transformar
            # pergunta-sem-match em Cache Hit. Ela ILUSTRA uma resposta que existe.
            # Por isso não vota em `relevante` (já decidido acima) — mas, diferente do
            # vizinho da malha, PROMOVE (entra em `fontes`): passou o mesmo gate dos
            # átomos de texto, então é reuso real, não passagem por perto.
            figuras: List[Tuple[float, object]] = []
            if candidatos:
                for s, d in await self._buscar_figuras(consulta, chaves_aterr):
                    if d.page_content in vistos:
                        continue                 # já entrou pela busca de texto
                    vistos.add(d.page_content)
                    figuras.append((s, d))

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
            # EXPANSÃO POR PÁGINA: os irmãos entram logo atrás dos matches reais,
            # antes de figura e da malha — o fato que completa a ideia vale mais
            # que uma imagem ou um vizinho por conceito.
            irmaos: List[Tuple[Optional[float], object]] = []
            if settings.rag_expandir_pagina and candidatos:
                irmaos = await self._irmaos_de_pagina(
                    [d for _s, d in candidatos[: settings.rag_max_chunks]], vistos,
                    escopo)

            usar = selecionar_por_orcamento(
                list(candidatos[: settings.rag_max_chunks]) + irmaos + figuras + vizinhos,
                settings.rag_context_char_budget,
                settings.rag_figura_char_frac,
            )

            if settings.rag_debug:
                n_viz = sum(1 for s, _ in usar if s is None)
                n_fig = sum(1 for s, d in usar
                            if s is not None
                            and str((d.metadata or {}).get("tipo") or "") == "figura")
                telemetry.track(
                    "LOCAL_DBG",
                    f"selecionados={len(usar)}/{len(candidatos)} átomos "
                    f"(aterrados={len(aterrados)}, vizinhos_malha={n_viz}/{len(vizinhos)}, "
                    f"figuras={n_fig}/{len(figuras)})",
                )

            # O vizinho é ROTULADO como relacionado, não como match. Sem isso ele chega
            # ao LLM indistinguível de um átomo que responde a pergunta — e um átomo
            # tangencial apresentado como resposta é exatamente a alucinação que o
            # pipeline inteiro combate. O rótulo deixa o modelo usá-lo como apoio.
            texto = NENHUM if not usar else "\n".join(
                rotular_contexto(s, d) for s, d in usar
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
            # A figura attach-only entra por AQUI e só por aqui: ela acompanha as
            # páginas que de fato responderam (as promovíveis, `s is not None`), e
            # não vale como fonte nem como contexto.
            # ORDENADOS POR DISTÂNCIA para a âncora do anexo: `usar` sai na ordem em
            # que o contexto foi montado (aterrados, depois confiantes, depois irmãos
            # de página), que NÃO é a ordem de quão bem cada átomo respondeu. Medido em
            # 2026-07-31: em "o que são tricomas?" a primeira página da lista era a 33
            # (plantas-mãe, ácaros) e a que respondeu era a 54 — o anexo saía da página
            # errada por causa da ordem, não do critério.
            promovidos = sorted(
                ((s, d) for s, d in usar if s is not None), key=lambda sd: sd[0])
            # GATE DE PÁGINA: a co-locação só vale se a página veio por mérito.
            # Quando a melhor FIGURA vence com folga o melhor TEXTO, a pergunta é sobre
            # uma imagem e os átomos de texto entraram por acidente léxico — anexar as
            # figuras DELES é herdar o acidente. Caso real (2026-07-31): "poderia me
            # mostrar uma tartaruga marinha?" casou "tartaruga MARINHA" com "algas
            # MARINHAS" (adubo, na Cannabis Encyclopedia) e trouxe a figura do
            # fertilizante ao lado da tartaruga.
            # O gate é de PÁGINA e não de figura porque a figura co-locada pode
            # legitimamente estar longe da pergunta — é o defeito que este caminho
            # existe para contornar. Medido em 9 sondas, sem sobreposição: co-locação
            # boa (tricomas, podar, deficiência de N, clones, hidroponia) tem razão
            # texto/figura entre 0,76 e 1,01; ruim (tartaruga, torre Eiffel, pinguim,
            # Coliseu) entre 1,16 e 1,37. n=9 é pequeno — daí o botão.
            razao = settings.figuras_anexo_texto_ratio
            texto_perdeu = (razao > 0 and figuras and melhor_texto is not None
                            and melhor_texto > figuras[0][0] * razao)
            if texto_perdeu:
                telemetry.track("FIGURAS",
                                f"Co-locação pulada: melhor texto {melhor_texto:.4f} perde "
                                f"da melhor figura {figuras[0][0]:.4f}.")
            anexos = await self._anexos_colocados(
                [d for _s, d in promovidos], consulta,
                escopo) if (usar and not texto_perdeu) else []
            return LocalResult(texto, melhor, relevante, fontes, anexos)
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


class _DocSimples:
    """Documento mínimo (page_content + metadata) para o que vem do `store.get`,
    que devolve texto e metadado crus em vez de objetos do langchain."""

    __slots__ = ("page_content", "metadata")

    def __init__(self, page_content: str, metadata: dict) -> None:
        self.page_content = page_content
        self.metadata = metadata


def e_nota_de_figura(doc) -> bool:
    """A nota é de FIGURA? Puro/testável.

    Duas provas porque a base tem as duas gerações: o metadado `tipo` (a partir
    da migração `scripts/migrar_tipo_figura.py`) e o embed de imagem no corpo,
    que é o que as notas antigas trazem.
    """
    md = getattr(doc, "metadata", None) or {}
    if str(md.get("tipo") or "") == "figura":
        return True
    return "![[" in (getattr(doc, "page_content", "") or "")


def selecionar_por_orcamento(itens, orcamento: int, frac_figura: float):
    """Preenche o orçamento em chars com TETO PRÓPRIO para nota de figura. Puro.

    Por que o teto existe (medido em 2026-07-29 sobre 14 perguntas reais): as
    notas de figura comiam **40% do orçamento** — 7,5 notas e 4.740 chars por
    pergunta, 17 delas numa pergunta sobre oídio e 15 na de poda apical. Nesta
    última o texto que respondia não coube, e o modelo devolveu o sentinela.
    A legenda é curta e casa muita pergunta; sem teto, ela desloca o átomo que
    de fato responde.

    Figura fora do teto é PULADA, não interrompe a seleção — o `break` de
    orçamento continua valendo para o resto. Assim a busca por legenda ("me
    mostra o diagrama de X") segue funcionando, só que sem monopolizar o espaço.
    """
    teto_fig = int(orcamento * frac_figura) if frac_figura > 0 else orcamento
    usar: List[Tuple[Optional[float], object]] = []
    gasto_fig = 0
    for s, d in itens:
        n = len(d.page_content)
        if e_nota_de_figura(d):
            if gasto_fig + n > teto_fig:
                continue
            gasto_fig += n
        if usar and orcamento - n < 0:
            break                                        # respeita o teto (n_ctx)
        usar.append((s, d))
        orcamento -= n
    return usar


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


async def coletar_uteis(
    coros: List[Awaitable], aceitar: int, timeout: Optional[float] = None,
    *, cancelar_perdedores: bool = True,
) -> Tuple[list, list]:
    """RACE-FIRST-K: dispara todas as `coros` como tasks, itera conforme COMPLETAM e
    coleta os resultados ÚTEIS (truthy) até `aceitar`; então CANCELA as restantes.

    `cancelar_perdedores=False` (colheita): NÃO cancela os perdedores no caminho feliz —
    devolve-os VIVOS para o chamador aproveitar o trabalho já em curso (ver a colheita do
    WebSearcher). Barge-in/erro AINDA cancela tudo (o perdedor não vira task órfã). Nesse
    modo o chamador vira DONO das tasks vivas: precisa aguardá-las e liberar o recurso
    (ex.: o httpx.AsyncClient) só depois — senão elas rodam sob um client fechado.

    É a "consulta ~15 sites ao mesmo tempo, aceita os 3 primeiros que responderem":
    não espera as fontes lentas/mortas. Puro do ponto de vista de REDE (as coros é que
    fazem o IO), então é testável com fakes que dormem tempos diferentes.

    `timeout` (s) é aplicado POR tarefa (asyncio.wait_for): um item travado vira None
    (não contribui) em vez de segurar a coleta — rede de segurança, já que só esperamos
    os `aceitar` mais rápidos. Uma coro que FALHA ou estoura o timeout conta como
    'não-útil' e é ignorada; NUNCA derruba a coleta. CancelledError do chamador
    (barge-in) propaga: as tarefas pendentes são canceladas no finally e o erro sobe.

    Devolve (uteis, n_cancelados) — os úteis em ordem de CONCLUSÃO (os mais rápidos).
    """
    async def _guardado(coro):
        try:
            if timeout is not None:
                return await asyncio.wait_for(coro, timeout)
            return await coro
        except asyncio.CancelledError:
            raise                       # cancelamento externo (barge-in) tem que subir
        except Exception:
            return None                 # falha/timeout desta fonte -> não contribui

    tarefas = [asyncio.ensure_future(_guardado(c)) for c in coros]
    uteis: list = []
    erro = False
    try:
        for fut in asyncio.as_completed(tarefas):
            r = await fut
            if r:
                uteis.append(r)
                if len(uteis) >= aceitar:
                    break               # já temos os K mais rápidos
    except BaseException:
        erro = True   # barge-in/erro: cancela TUDO no finally, mesmo em modo colheita
        raise
    finally:
        pendentes = [t for t in tarefas if not t.done()]
        # Cancela se o chamador pediu (default) OU se estamos desenrolando por erro/barge-in
        # (nesse caso o perdedor NÃO pode virar task órfã, mesmo colhendo).
        if cancelar_perdedores or erro:
            for t in pendentes:
                t.cancel()
            if pendentes:
                await asyncio.gather(*pendentes, return_exceptions=True)
    return uteis, pendentes


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
        # Colheita dos perdedores do race: tasks de fundo (ref. forte retida, mesma war
        # story do _retry_task) que terminam os fetches perdedores e os atomizam. LRU de
        # URL já colhida deduplica entre turnos (não re-baixa nem re-enfileira a mesma pág).
        self._colheita_tasks: "set[asyncio.Task]" = set()
        self._urls_colhidas = LruCache(settings.web_colheita_url_cache)
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

    async def buscar_imagens(self, termo: str, max_results: int) -> list:
        """Candidatos de IMAGEM na web (2026-07-31). Lista de dicts do ddgs.

        Mesmo caminho do `_ddg`: máscara de PII antes de o termo sair (este e o
        `_ddg` são as únicas portas de saída de texto do usuário) e fallback de
        backend, porque um backend do DDG cair é rotina.

        `safesearch="on"` é deliberado: o buscador de imagem é o único ponto do
        projeto que traz PIXEL de fora, e o vault é de cultivo — uma consulta
        ambígua não pode devolver qualquer coisa na tela do dono.

        Não baixa nada: quem baixa, valida e sanitiza é o `imagem_web`.
        """
        if not termo:
            return []
        if settings.egressao_guarda:
            termo, pii = egressao.mascarar_pii(termo)
            if pii:
                telemetry.warn("EGRESSAO", f"PII mascarada na busca de imagem: {', '.join(pii)}")

        def _fetch() -> list:
            from ddgs import DDGS

            def _um_backend(backend: str) -> list:
                with DDGS() as ddgs:
                    try:
                        return list(ddgs.images(
                            termo, max_results=max_results, safesearch="on",
                            backend=backend))
                    except TypeError:
                        return list(ddgs.images(
                            termo, max_results=max_results, safesearch="on"))

            return buscar_com_fallback(_um_backend, settings.web_backends)

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:
            telemetry.warn("IMG_WEB", f"Busca de imagem falhou: {exc}")
            return []

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

    async def buscar_pdfs(self, termos: str, max_results: int) -> List[dict]:
        """Busca PDFs (Fase 4, colheita acadêmica): a MESMA saída de rede do `_ddg`
        — logo, a Guarda de Egressão continua mascarando PII —, só com o operador
        `filetype:pdf` e um viés acadêmico na query. ISOLADA do caminho vivo: o
        `search` de sempre não passa por aqui. Devolve [{url, titulo}]."""
        brutos = await self._ddg(academico.montar_query(termos), max_results)
        return academico.filtrar_pdfs(brutos)

    async def baixar_pdf(self, url: str, max_mb: int) -> Optional[bytes]:
        """Baixa um PDF com teto de tamanho, sem nunca levantar. Streaming: aborta
        no meio se passar do teto (um livro inteiro de 300 MB não pode virar RAM).
        Não há fallback de cert aqui — PDF de fonte com TLS quebrado não entra."""
        import httpx

        limite = max_mb * 1024 * 1024
        try:
            async with httpx.AsyncClient(
                timeout=settings.web_fetch_timeout, follow_redirects=True,
                headers=_HEADERS_FETCH,
            ) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > limite:
                            telemetry.warn("ACADEMICO", f"PDF acima de {max_mb}MB, descartado: {url[:60]}")
                            return None
            if not bytes(buf[:5]).startswith(b"%PDF"):
                return None      # HTML de paywall servido como .pdf
            return bytes(buf)
        except Exception as exc:
            telemetry.warn("ACADEMICO", f"Falha ao baixar PDF {url[:60]}: {exc}")
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
                headers=_HEADERS_FETCH, verify=False,  # nosec B501
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

    def _agendar_colheita(self, client, perdedores: list, on_colheita) -> None:
        """Task de fundo que COLHE os perdedores do race: espera cada um terminar, entrega
        o corpo útil (deduplicado por URL) ao `on_colheita`, e SÓ ENTÃO fecha o client (é
        a dona dele agora). Web-only, roda durante a fala — nunca no hot-path. Best-effort:
        erro de uma fonte é ignorado; erro do sink é logado, nada derruba (é background)."""
        async def _colher() -> None:
            colhidas = 0
            try:
                for fut in asyncio.as_completed(perdedores):
                    try:
                        r = await fut
                    except Exception:
                        continue   # perdedor que falhou/estourou timeout: ignora
                    if not r:
                        continue
                    url, texto = r
                    if not texto or self._urls_colhidas.get(url) is not None:
                        continue   # sem corpo, ou já colhida (dedup anti-refetch entre turnos)
                    self._urls_colhidas.put(url, "1")
                    try:
                        on_colheita(url, texto)
                        colhidas += 1
                    except Exception as exc:
                        telemetry.error("COLHEITA", "sink da colheita falhou", exc)
            finally:
                try:
                    await client.aclose()   # dona do client: fecha só com a colheita pronta
                except Exception:
                    pass
                if colhidas:
                    telemetry.track(
                        "COLHEITA", f"{colhidas} página(s) perdedora(s) colhida(s) p/ o idle."
                    )

        task = asyncio.ensure_future(_colher())
        self._colheita_tasks.add(task)            # ref forte enquanto roda (war story #2)
        task.add_done_callback(self._colheita_tasks.discard)  # auto-libera ao terminar

    async def _deep_fetch(
        self, res: list, consulta: str, on_colheita=None
    ) -> Optional[str]:
        """Estágio 2: abre as top-N páginas, atomiza e rankeia contra a consulta.

        Devolve o contexto montado (só os melhores trechos) ou None se nada útil saiu
        — nesse caso o chamador cai de volta pros snippets.

        `on_colheita(url, texto)` (opcional): se dado E `web_colheita_habilitada`, os
        perdedores do race NÃO são cancelados — terminam em background e cada corpo útil
        (deduplicado por URL) é entregue ao callback para virar #conhecimento_novo.
        """
        emb = self._embeddings.instance if self._embeddings else None
        if emb is None:
            return None  # sem embedding não há como rankear -> usa snippets

        urls = [r.get("href") or r.get("url") or "" for r in res]
        urls = [u for u in urls if u][: settings.web_fetch_pool]
        if not urls:
            return None

        try:
            import httpx
        except ImportError:
            telemetry.warn("WEB_FETCH", "httpx ausente — usando só snippets.")
            return None

        from urllib.parse import urlparse

        # RACE-FIRST-K: dispara até web_fetch_pool fetches concorrentes e aceita os
        # web_fetch_aceitar mais rápidos com corpo útil. Cada coro devolve (url, texto)
        # só se a página teve corpo.
        # LIFECYCLE do client: sem colheita, `coletar_uteis` cancela os perdedores e o
        # client fecha AQUI (no finally) — nada sobrevive a este método. COM colheita, os
        # perdedores continuam vivos depois daqui, então o client NÃO pode fechar já:
        # `_agendar_colheita` vira dono do client e o fecha só quando a colheita termina
        # (senão os fetches rodariam sob um client fechado — o que o `async with` evitava).
        vai_colher = bool(on_colheita) and settings.web_colheita_habilitada
        client = httpx.AsyncClient(
            timeout=settings.web_fetch_timeout, follow_redirects=True, headers=_HEADERS_FETCH
        )
        client_delegado = False
        try:
            async def _fetch_um(u):
                texto = await self._baixar_pagina(client, u)
                return (u, texto) if texto else None

            aceitos, perdedores = await coletar_uteis(
                [_fetch_um(u) for u in urls],
                settings.web_fetch_aceitar,
                settings.web_fetch_timeout_s,
                cancelar_perdedores=not vai_colher,
            )
            if vai_colher and perdedores:
                self._agendar_colheita(client, perdedores, on_colheita)
                client_delegado = True   # a task de colheita fecha o client
        finally:
            if not client_delegado:
                await client.aclose()

        # Proveniência ("fonte?"): domínios das páginas ACEITAS — as que contribuíram
        # com trechos. Falado como domínio (URL é inaudível).
        self.ultimos_dominios = [
            urlparse(u).netloc for u, _t in aceitos if urlparse(u).netloc
        ]

        textos = [t for _u, t in aceitos]
        telemetry.track(
            "WEB_FETCH",
            f"race: {len(urls)} disparados, {len(aceitos)}/{settings.web_fetch_aceitar} "
            f"aceitos, {len(perdedores)} {'em colheita' if vai_colher else 'cancelados'}.",
        )
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

    async def search(self, termo: str, consulta: Optional[str] = None, on_colheita=None) -> str:
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
            # Com o deep-fetch ligado, o race abre até web_fetch_pool páginas — então o
            # DDG precisa devolver >= pool candidatos (senão o pool fica ocioso). Sem o
            # fetch, mantém o web_max_results de sempre (só snippets).
            n_res = (
                max(settings.web_max_results, settings.web_fetch_pool)
                if settings.web_fetch_enabled
                else settings.web_max_results
            )
            res = await self._ddg(termo, n_res)
            self._disjuntor.registrar_sucesso()  # canal respondeu (mesmo que vazio)
            if not res:
                return NENHUM

            ctx = None
            if settings.web_fetch_enabled:
                try:
                    ctx = await self._deep_fetch(
                        res, (consulta or termo).strip() or termo, on_colheita=on_colheita
                    )
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
