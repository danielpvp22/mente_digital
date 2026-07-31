"""Gera conceitos que CONECTAM na Malha Neural das notas do vault (LLM local, offline).

O NÚMERO QUE MOTIVA A PASSADA (medido no vault inteiro, 2026-07-31): 24.487 notas,
96,9% com a linha `**Malha Neural:**`, 66.227 wikilinks, 24.471 conceitos distintos
— e **70,1% desses conceitos (17.153) aparecem em UMA nota só**. Um link que não é
compartilhado não liga nada: a Malha hoje é quase inerte, tanto para navegar no
Obsidian quanto para a expansão por vizinhança do `rag.MalhaIndex` (que só pontua
conceito COMPARTILHADO, por IDF). Só 893 conceitos aparecem em 10+ notas — e são
justamente os úteis e genéricos: Cannabis 2.029, YOLO 473, Cultivo 330, Python 294,
VRAM 266, pH 204.

A MESMA CONTA, PELA RÉGUA DESTE SCRIPT (2026-07-31, `--so-metricas`): 22.355 notas,
60.688 links, 21.924 conceitos distintos, 69,1% solteiros (15.152) e 2.157 notas
INERTES (9,6% — nenhum conceito compartilhado, ou seja, a malha delas não leva a
lugar nenhum). A diferença para os números acima não é discordância: aqui ficam de
fora as 2.121 notas de FIGURA e conta-se só a LINHA da Malha, enquanto
`rag.extrair_conceitos` varre a nota inteira e por isso conta o embed de figura
('![[fig.webp]]') como se fosse conceito. O diagnóstico é o mesmo, com ou sem eles.

Logo, o alvo NÃO é "gerar mais links" — é gerar links que CONECTEM. O critério de
sucesso é a queda da fração de conceitos-solteiros e a subida da conectividade
média, que este próprio script mede ANTES e DEPOIS (`--so-metricas` mede sem gastar
GPU nenhuma).

COMO SE EVITA PIORAR O PROBLEMA (a decisão de desenho que sustenta tudo): o modelo
recebe um VOCABULÁRIO CONTROLADO — a lista dos conceitos que o vault JÁ usa,
filtrada por relevância à nota — e a ordem de REUSAR um deles sempre que couber,
inventando termo novo só quando nenhum servir. Sem isso, pedir conceitos a um LLM
para 24 mil notas produz 24 mil conceitos novos e o vault sai com o MESMO defeito,
só que maior. A relevância é medida sem GPU: casamento de token (índice invertido
sobre os conceitos frequentes), co-ocorrência na malha atual e os hubs globais.

E o Python valida, nunca o modelo: cada item volta a passar por `atomos.normalizar_malha`
(a sintaxe é imposta aqui desde que 58% dos átomos saíram com colchete simples) e
pelas guardas de forma. Nota cujo modelo não devolveu NENHUM conceito válido fica
INTACTA — mesma régua do reparo: melhor malha pobre que malha inventada.

Conservador na escrita: os links atuais COMPARTILHADOS (frequência >= 2) são
preservados e os novos entram ao lado; só o link solteiro (frequência 1, que não liga
nada) é descartado. Reescreve SÓ a linha da Malha — corpo, frontmatter e tags saem
byte-idênticos.

NOTAS DE FIGURA (`--incluir-figuras` / `--so-figuras`, default = fora): a Malha das
1.818 figuras é METADADO, não conceito — `**Malha Neural:** [[Soil]] [[Cannabis]]`,
capítulo + livro, em inglês; `cannabis` está em 1.817 delas e 432 dos 486 conceitos
de figura não existem em nenhuma nota de texto. Pela régua "2+ conceitos
compartilhados com notas de TEXTO", só 14,0% das figuras passam hoje. Testado em 30:
conceituar pela LEGENDA que a nota já tem rende 3,70 compartilhados por nota (80% com
>=2) contra 1,50 (37%) de um modelo multimodal olhando a imagem — 2,5x melhor e sem
GPU. Por isso a figura entra pelo caminho de TEXTO, com a legenda PT como corpo; a
`legenda_original` (inglês) NÃO vai ao prompt: ela é a fonte do conceito em inglês
que o vault já sofre.

    python scripts/melhorar_links.py --so-metricas              # só mede (sem GPU)
    python scripts/melhorar_links.py --limite 20                # DRY-RUN (default)
    python scripts/melhorar_links.py --limite 200 --aplicar     # grava (com backup)
    python scripts/melhorar_links.py --so-figuras --limite 20   # DRY-RUN nas figuras
    python scripts/melhorar_links.py --limite 20 --ancorar-dominio   # candidatos da MESMA obra
    python scripts/melhorar_links.py --so-obra "Cannabis" --aplicar

ANCORAGEM POR DOMÍNIO (`--ancorar-dominio`, default OFF — a régua atual é a validada):
o vault mistura dois mundos que não se falam (10.534 notas de cultivo contra 148
baldes de desenvolvimento, o maior com 1.944), e casamento por TOKEN atravessa os
dois: uma foto de aero-cloner recebeu [[Ambiente Conda]], uma de aranha recebeu
[[Detecção de Objetos]] e "esta VISÃO de perto" recebeu [[Visão Computacional]]. Com
a flag, só entram como candidatos os conceitos já usados por notas da MESMA obra ou
balde. A/B medido (2026-07-31, 20 figuras + 20 notas de texto, CPU, mesmas notas nos
dois braços): notas com conceito de outro domínio caem de 7/20 para 1/20 nas figuras
e de 9/20 para 0/20 no texto; o custo temido (vocabulário mais pobre) NÃO apareceu —
candidatos por nota 22,9 -> 22,4 e o reuso SUBIU (59 -> 62 conceitos que já conectam).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import socket
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)   # o pydantic lê o .env relativo ao CWD (ver ocr_agora.py)

from mente_digital import atomos, livro, prompts, rag, reparo, textutils  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.llm import LlamaManager, preparar_offline  # noqa: E402
from mente_digital.telemetry import telemetry  # noqa: E402

# Rótulo canônico da Malha. Cópia local pelo mesmo motivo de `reparo._MALHA_MARCA`:
# é CONTRATO de texto com `atomos._MALHA`, e um script não importa privado de módulo.
_MALHA = "**Malha Neural:**"
_TAG_LINHA_RE = re.compile(r"^\s*#[\w/\-]+(?:\s+#[\w/\-]+)*\s*$")

MAX_CHARS_CORPO = 700        # o que da nota vai ao prompt (1 ideia cabe folgado)
MAX_TOKENS_SAIDA = 64        # 2-4 conceitos numa linha; teto curto é anti-derivação
MAX_PALAVRAS_CONCEITO = 4    # conceito é um NOME, não uma frase
MAX_CHARS_CONCEITO = 45
MIN_FREQ_CANDIDATO = 3       # conceito abaixo disso ainda não conecta: não se oferece
MAX_CANDIDATOS = 24          # cabe no prompt sem empurrar a nota para fora do foco
MAX_CONCEITOS_NOTA = 6       # teto por nota: malha inchada não é malha, é ruído
TOP_GLOBAL = 10              # hubs oferecidos a toda nota (Cannabis, Cultivo, ...)
TOP_COOCORRENCIA = 6         # vizinhos de malha oferecidos por conceito atual
SALVA_ESTADO_A_CADA = 25

ESTADO = RAIZ / "scripts" / "melhorar_links.estado.json"

SYS = (
    "Você indexa notas de um Zettelkasten: dada uma nota, devolve os CONCEITOS sob os "
    "quais ela deve ser arquivada. Só a linha de conceitos, sem explicação e sem preâmbulo."
)

# Extensões que denunciam caminho de arquivo virado "conceito". É defeito REAL da base:
# `rag.extrair_conceitos` roda sobre a nota inteira, então o embed de figura
# ('![[fig_12.webp]]') já entra no índice de conceitos como se fosse um tema.
_EXTENSOES = (".md", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".json", ".txt", ".pdf",
              ".csv", ".html", ".py", ".gguf", ".yaml", ".yml")
# Rótulo que o modelo às vezes prefixa ("Conceitos: a; b"). Curto e terminando em ':'.
_ROTULO_RE = re.compile(r"^[A-Za-zÀ-ÿ ]{0,20}:\s*")
# Numeração/marcador de lista. O prompt pede UMA linha, mas o modelo às vezes
# responde em lista — e sem tirar o '1.' o conceito entraria no vault como
# "1. Cannabis", que é um nó novo e inútil no grafo.
_MARCADOR_RE = re.compile(r"^\s*(?:\d+\s*[.)\-]|[-*•‣])\s*")
MAX_LINHAS_SAIDA = 6         # linhas da resposta que ainda contam como conceitos
# HUB: conceito presente em mais que esta fração das notas de TEXTO não conta como
# conexão. Medido: `Cannabis` está em 2.029 notas (9,1%) e ligar por ele é ligar com
# metade do vault — o estudo das figuras achou 99,7% delas "conectadas" quando o
# único conceito em comum era esse. Contar hub como conexão faz a métrica MENTIR.
HUB_FRACAO_MAX = 0.05

# Linhas que uma nota de FIGURA tem e que NÃO podem ir ao prompt:
# - o embed (`![[...webp]]`) é caminho de arquivo, e caminho vira "conceito" ruim;
# - a linha de proveniência carrega o capítulo EM INGLÊS ('Soil — Chapter 18') e o
#   título do livro — é exatamente a fonte do conceito-metadado em inglês que a
#   Malha das figuras tem hoje ([[Soil]] [[Cannabis]] em 1.817 delas).
# A `legenda_original` (inglês) mora no FRONTMATTER, que `reparo.separar` já deixa
# de fora do corpo — então ela nunca chega ao prompt por construção.
_EMBED_RE = re.compile(r"!\[\[")
_PROVENIENCIA_FIGURA_RE = re.compile(
    r"^\s*(?:figura\s+d[ao]\s+p[áa]gina|imagem\s+obtida\s+na\s+web)\b", re.IGNORECASE)
# Legenda de UMA palavra não é material — é rótulo. MEDIDO no dry-run de 20 figuras:
# das 5 que ganharam conceito infundado, 3 tinham legenda de uma palavra só ("Julho"
# -> [[Estresse]], "Junho" -> [[Secagem]], "Greensand" -> [[Secagem]]): sem texto, o
# modelo preenche com o que é frequente no vault. São 98 das 1.840 figuras (5,3%), e
# ficam INTACTAS — a mesma régua do reparo, melhor malha pobre que malha inventada.
MIN_PALAVRAS_LEGENDA = 2


def _norm(c: str) -> str:
    return textutils.normaliza(c)


# A obra dentro da `origem` de página de livro (`livro.origem_do_job` monta a string).
_OBRA_RE = re.compile(r"Livro\s+'([^']+)'")


def dominio_da_origem(origem: str) -> str:
    """O MUNDO a que a nota pertence: a OBRA (livro) ou o BALDE (auto-colhido). Puro.

    Existe porque este vault mistura dois mundos que não se falam — cultivo (10.534
    notas de 'The Cannabis Encyclopedia') e desenvolvimento (148 baldes, o maior com
    1.944 notas: 'Orquestração-de-IA…json', 'Otimização-do-Módulo-de-Visão-YOLO.json',
    'Sintese'). Qualquer casamento por TOKEN sobre o vault inteiro atravessa os dois:
    é o que fez uma foto de aranha ('Green lynx spider') receber [[Detecção de
    Objetos]] e a de glândulas resiníferas ('esta VISÃO de perto') receber [[Visão
    Computacional]] — e é o mesmo mecanismo do bug que abriu esta sessão, quando "tem
    imagem de um?" sobre tricomas puxou os baldes de YOLO/Ollama.

    `livro.e_origem_de_pagina` já é o juiz de "página de verdade x balaio" no repo;
    aqui ele só escolhe QUAL chave usar.
    """
    origem = (origem or "").strip()
    if not origem:
        return ""
    if livro.e_origem_de_pagina(origem):
        m = _OBRA_RE.search(origem)
        if m:
            # A OBRA, não a página: o vocabulário de um capítulo sozinho seria magro
            # demais, e o livro inteiro já é um mundo coeso.
            return "obra:" + textutils.normaliza(m.group(1))
    return "balde:" + textutils.normaliza(origem)


# --- leitura do vault --------------------------------------------------------

@dataclass
class Nota:
    caminho: Path
    rel: str
    origem: str
    titulo: str
    corpo: str
    malha: str                 # linha inteira '**Malha Neural:** ...' (ou "")
    conceitos: List[str]       # forma de superfície, na ordem do arquivo
    e_figura: bool = False
    dominio: str = ""          # ver dominio_da_origem


def corpo_de_figura(corpo: str) -> str:
    """O que da nota de FIGURA vale como material do prompt: a LEGENDA. Puro/testável.

    Medido pelo estudo das figuras (30 notas): gerar conceitos a partir da legenda
    que já está na nota rende 3,70 conceitos compartilhados por nota (80% com >=2),
    contra 1,50 (37%) de um modelo multimodal OLHANDO a imagem — a legenda ganha por
    2,5x e sem GPU nenhuma. Então o caminho de figura é o caminho de TEXTO, e o que
    esta função faz é só tirar do corpo o que não é legenda.
    """
    linhas = [ln for ln in corpo.splitlines()
              if not _EMBED_RE.search(ln) and not _PROVENIENCIA_FIGURA_RE.match(ln)]
    return "\n".join(linhas).strip()


def _conceitos_da_malha(linha_malha: str) -> List[str]:
    """Conceitos da LINHA da Malha — não da nota inteira. De propósito.

    `rag.metadados_da_nota` extrai de todo o texto e por isso conta o embed de figura
    ('![[fig.webp]]') como conceito; para o vocabulário que vai ao prompt isso é
    veneno (o modelo passa a "reusar" nome de arquivo). Aqui só a Malha conta.
    """
    return rag.extrair_conceitos(linha_malha)


def varrer(filtro_obra: str) -> Tuple[List[Nota], Counter]:
    """Todas as notas-átomo do vault (figuras MARCADAS, não descartadas) + as de fora.

    A varredura é sempre COMPLETA mesmo quando as figuras não são alvo: o vocabulário
    controlado e a régua do que é "conceito compartilhado" saem das notas de TEXTO, e
    quem decide o alvo é `_selecionar`. Separar as duas coisas é o que permite a
    figura REUSAR a palavra do vault sem que a palavra dela contamine o vault.
    """
    vault = Path(settings.caminho_obsidian)
    subpasta_figuras = (settings.subpasta_figuras or "").strip("/\\")
    notas: List[Nota] = []
    motivos: Counter = Counter()
    for md in sorted(vault.rglob("*.md")):
        if ".obsidian" in md.parts:
            motivos[".obsidian (fora do escopo)"] += 1
            continue
        try:
            texto = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            telemetry.error("LINKS", f"não deu para ler {md.name}", exc)
            motivos["ilegível"] += 1
            continue
        origem = (rag.parse_frontmatter(texto).get("origem") or "").strip()
        if filtro_obra and filtro_obra.lower() not in origem.lower():
            motivos["fora do --so-obra"] += 1
            continue
        atomo = reparo.separar(texto)
        if not atomo.titulo:
            motivos["sem título (não é átomo)"] += 1
            continue
        e_figura = (bool(subpasta_figuras) and subpasta_figuras in md.parts) or \
            reparo.classificar(atomo) == reparo.FIGURA
        corpo = corpo_de_figura(atomo.corpo) if e_figura else atomo.corpo
        if not corpo.strip():
            # Numa figura isto quer dizer "nota sem legenda": sobrou só o embed e a
            # proveniência, e aí não há material nenhum para conceituar.
            motivos["sem corpo/legenda" if e_figura else "sem corpo (não é átomo)"] += 1
            continue
        if prompts.TAG_ATOMO not in texto and not atomo.malha:
            # Lista, inbox, agenda e nota escrita à mão pelo dono não são átomos —
            # e o vault NÃO está no git: passar o rolo nelas não teria desfazer.
            motivos["não é átomo (sem tag nem malha)"] += 1
            continue
        notas.append(Nota(md, str(md.relative_to(vault)), origem, atomo.titulo,
                          corpo, atomo.malha, _conceitos_da_malha(atomo.malha), e_figura,
                          dominio_da_origem(origem)))
    return notas, motivos


# --- vocabulário controlado (o que evita 24 mil conceitos novos) -------------

@dataclass
class Indice:
    """O vocabulário que o vault JÁ usa, com o que é preciso para oferecê-lo."""
    freq: Counter = field(default_factory=Counter)              # conceito norm -> nº de notas
    canonica: Dict[str, str] = field(default_factory=dict)      # norm -> forma de superfície
    por_token: Dict[str, Set[str]] = field(default_factory=dict)   # token -> conceitos norm
    coocorrencia: Dict[str, Counter] = field(default_factory=dict)  # norm -> vizinhos na malha
    top_global: List[str] = field(default_factory=list)
    por_dominio: Dict[str, Counter] = field(default_factory=dict)   # domínio -> conceito -> nº notas
    top_dominio: Dict[str, List[str]] = field(default_factory=dict)  # domínio -> conceitos mais usados


def _tokens_uteis(conceito_norm: str) -> List[str]:
    """Tokens do conceito que servem para casar com a nota (sem stopword, >=3)."""
    return [t for t in textutils.tokens(conceito_norm)
            if t not in textutils.STOP and len(t) >= 3]


def indexar(notas: Sequence[Nota]) -> Indice:
    """Monta o vocabulário. Sem GPU, sem embedding: é contagem e casamento de token.

    O índice invertido token->conceitos existe por CUSTO: escolher candidatos varrendo
    os 24 mil conceitos por nota seria ~600M comparações na passada inteira. Com o
    invertido, cada nota olha só os conceitos que compartilham uma palavra com ela.
    """
    idx = Indice()
    superficie: Dict[str, Counter] = defaultdict(Counter)
    for n in notas:
        chaves = []
        for c in n.conceitos:
            k = _norm(c)
            if not k:
                continue
            chaves.append(k)
            superficie[k][c] += 1
        for k in set(chaves):
            idx.freq[k] += 1
            if n.dominio:
                idx.por_dominio.setdefault(n.dominio, Counter())[k] += 1
        # Co-ocorrência: quem anda junto na malha de hoje é candidato natural para a
        # nota vizinha — é o que faz o conceito novo cair no MESMO bolso do antigo.
        for a, b in combinations(sorted(set(chaves)), 2):
            idx.coocorrencia.setdefault(a, Counter())[b] += 1
            idx.coocorrencia.setdefault(b, Counter())[a] += 1
    # A forma de superfície MAIS USADA vira a canônica: é ela que impede o vault de
    # ganhar 'cannabis' ao lado de 'Cannabis' (dois nós no grafo, um tema só).
    for k, formas in superficie.items():
        idx.canonica[k] = formas.most_common(1)[0][0]
    for k, n in idx.freq.items():
        if n < MIN_FREQ_CANDIDATO:
            continue
        for t in set(_tokens_uteis(k)):
            idx.por_token.setdefault(t, set()).add(k)
    idx.top_global = [k for k, _ in idx.freq.most_common(TOP_GLOBAL)]
    # Pré-calculado: com a âncora ligada isto é lido uma vez por nota, e ordenar um
    # Counter de milhares de conceitos a cada nota custaria a passada inteira.
    for dom, freq in idx.por_dominio.items():
        idx.top_dominio[dom] = [k for k, _ in freq.most_common(TOP_GLOBAL)]
    return idx


def escolher_candidatos(titulo: str, corpo: str, atuais: Sequence[str], idx: Indice,
                        maximo: int = MAX_CANDIDATOS, dominio: str = "") -> List[str]:
    """Os conceitos JÁ existentes mais plausíveis para esta nota. Puro/testável.

    Três fontes, somadas: (1) casamento léxico com o texto da nota, ponderado pela
    fração do conceito que casou (assim 'Solução Nutritiva' ganha de 'Nutrição
    Animal' numa nota sobre solução); (2) vizinhos de malha dos conceitos que a nota
    já tem; (3) os hubs globais, que toda nota pode legitimamente vestir.

    `dominio` != "" ANCORA as três fontes no mundo da nota (ver `dominio_da_origem`):
    só entra conceito que já é usado por uma nota de texto da MESMA obra/balde. É a
    correção do casamento de token que atravessa cultivo e desenvolvimento. Tem um
    custo — vocabulário menor, mais conceito inventado — que o relatório mede nos
    dois braços em vez de supor.
    """
    palavras = textutils.palavras_chave(f"{titulo}\n{corpo}")
    permitido = idx.por_dominio.get(dominio) if dominio else None
    pontos: Dict[str, float] = defaultdict(float)

    vistos: Set[str] = set()
    for w in palavras:
        vistos |= idx.por_token.get(w, set())
    for k in vistos:
        if permitido is not None and k not in permitido:
            continue
        toks = _tokens_uteis(k)
        if not toks:
            continue
        casados = sum(1 for t in toks if t in palavras)
        if not casados:
            continue
        # log da frequência entra como DESEMPATE, não como critério: senão o hub
        # engoliria o conceito específico, que é justamente o que conecta melhor.
        pontos[k] += 3.0 * (casados / len(toks)) + math.log1p(idx.freq[k]) / 10

    for c in atuais:
        k = _norm(c)
        if idx.freq.get(k, 0) < 2:
            continue                     # solteiro não tem vizinhança que valha
        for vizinho, n in idx.coocorrencia.get(k, Counter()).most_common(TOP_COOCORRENCIA):
            if permitido is not None and vizinho not in permitido:
                continue
            if idx.freq.get(vizinho, 0) >= MIN_FREQ_CANDIDATO:
                pontos[vizinho] += 1.0 + math.log1p(n) / 10

    topo = idx.top_dominio.get(dominio, []) if permitido is not None else idx.top_global
    for pos, k in enumerate(topo):
        pontos[k] += 0.5 - pos * 0.01    # piso: entram, mas atrás do que casou

    ordem = sorted(pontos.items(), key=lambda kv: (-kv[1], kv[0]))
    return [idx.canonica.get(k, k) for k, _ in ordem[:maximo]]


# --- prompt + validação ------------------------------------------------------

def prompt_conceitos(titulo: str, corpo: str, candidatos: Sequence[str]) -> str:
    """Curto e duro. Saída MÍNIMA (uma linha) — quanto menor a tarefa, menos deriva."""
    lista = "; ".join(candidatos) or "(o vault ainda não tem conceitos frequentes)"
    return (
        f"NOTA:\n## {titulo}\n{corpo[:MAX_CHARS_CORPO]}\n\n"
        f"CONCEITOS JÁ USADOS NO MEU ARQUIVO:\n{lista}\n\n"
        "Responda com 2 a 4 conceitos sob os quais esta nota deve ser arquivada, "
        "separados por ponto e vírgula.\n"
        "Regras:\n"
        "1. REUSE um conceito da lista acima sempre que ele couber; escreva um termo "
        "novo só se nenhum da lista servir.\n"
        "2. Cada conceito é um substantivo ou locução nominal em português, de 1 a 4 "
        "palavras — nunca uma frase, nunca um verbo, nunca uma definição.\n"
        "3. Não repita o título da nota. Nada de nome de arquivo, número solto, "
        "colchete, numeração ou comentário.\n"
        "4. Responda apenas a linha dos conceitos."
    )


def _forma_invalida(c: str, titulo_norm: str) -> Optional[str]:
    """Por que este item NÃO é conceito, ou None se passa. Puro/testável."""
    k = _norm(c)
    if not k:
        return "vazio"
    if "/" in c or "\\" in c:
        return "tem barra (caminho)"
    if k.endswith(_EXTENSOES):
        return "nome de arquivo"
    if len(c) > MAX_CHARS_CONCEITO:
        return f"mais de {MAX_CHARS_CONCEITO} chars"
    if len(c.split()) > MAX_PALAVRAS_CONCEITO:
        return f"mais de {MAX_PALAVRAS_CONCEITO} palavras"
    if not re.search(r"[a-zà-ÿ]", k):
        return "número/símbolo solto"
    if k == titulo_norm:
        return "igual ao título"
    return None


def _itens_crus(bruto: str) -> List[str]:
    """Linhas úteis da saída, sem rótulo nem marcador de lista. Puro/testável."""
    out: List[str] = []
    for ln in (bruto or "").splitlines():
        ln = _MARCADOR_RE.sub("", _ROTULO_RE.sub("", ln.strip())).strip()
        if ln:
            out.append(ln)
        if len(out) >= MAX_LINHAS_SAIDA:
            break
    return out


def validar(bruto: str, titulo: str, idx: Indice) -> Tuple[List[str], Counter]:
    """Saída crua do LLM -> conceitos aceitos (na capitalização do vault). Puro/testável.

    O fatiamento é o do repo (`atomos.normalizar_malha`), não um novo: ele já resolve
    vírgula/ponto-e-vírgula/'ou', parênteses e colchete torto — a mesma bagunça que o
    modelo produz aqui. Depois vêm as guardas de forma, porque confiar no modelo para
    "sem verbo, sem frase, sem arquivo" é o erro que este projeto já pagou.
    """
    recusas: Counter = Counter()
    linha = ";".join(_itens_crus(bruto))
    titulo_norm = _norm(titulo)
    aceitos: List[str] = []
    vistos: Set[str] = set()
    achados = rag.extrair_conceitos(atomos.normalizar_malha(linha))
    if linha and not achados:
        # Sem isto a perda seria SILENCIOSA: `normalizar_malha` descarta o que passa
        # de 60 chars, então o modelo respondendo com uma FRASE some sem deixar rastro.
        recusas["saída sem conceito utilizável (frase?)"] += 1
    for c in achados:
        motivo = _forma_invalida(c, titulo_norm)
        if motivo:
            recusas[motivo] += 1
            continue
        k = _norm(c)
        if k in vistos:
            continue
        vistos.add(k)
        # Capitalização do VAULT quando o conceito já existe: 'cannabis' e 'Cannabis'
        # são dois nós no grafo do Obsidian, e criar o segundo desfaz o trabalho.
        aceitos.append(idx.canonica.get(k, c))
    return aceitos, recusas


def mesclar(atuais: Sequence[str], novos: Sequence[str], idx: Indice) -> Tuple[List[str], int]:
    """Links finais da nota + quantos solteiros foram descartados. Puro/testável.

    Conservador por ordem do desenho: link atual COMPARTILHADO (freq >= 2) é
    preservado — ele já liga alguma coisa, e reescrevê-lo apagaria uma aresta boa.
    Só o solteiro (freq 1) sai, porque ele não leva a lugar nenhum.
    """
    finais: List[str] = []
    vistos: Set[str] = set()
    descartados = 0
    for c in atuais:
        k = _norm(c)
        if idx.freq.get(k, 0) >= 2:
            if k not in vistos:
                vistos.add(k)
                finais.append(c)
        else:
            descartados += 1
    for c in novos:
        k = _norm(c)
        if k not in vistos:
            vistos.add(k)
            finais.append(c)
    return finais[:MAX_CONCEITOS_NOTA], descartados


def reescrever_malha(texto: str, links: str) -> str:
    """Troca SÓ a linha da Malha (ou a insere antes das tags). Puro/testável.

    Cirurgia de linha em vez de remontar a nota por `reparo.montar`: corpo,
    frontmatter e tags saem byte-idênticos, e o que não é o alvo desta passada não
    corre risco nenhum.
    """
    nova = f"{_MALHA} {links}"
    linhas = texto.splitlines()
    for i, ln in enumerate(linhas):
        if _MALHA in ln:
            antes = ln.partition(_MALHA)[0]
            # Malha GRUDADA no fim de uma linha de corpo (58 casos reais na
            # Encyclopedia — ver `reparo.separar`): trocar a linha inteira apagaria
            # a frase. Preserva o que vem antes do rótulo, em linha própria.
            linhas[i] = f"{antes.rstrip()}\n{nova}" if antes.strip() else nova
            return "\n".join(linhas) + "\n"
    pos = next((k for k, ln in enumerate(linhas) if _TAG_LINHA_RE.match(ln)), len(linhas))
    linhas.insert(pos, nova)
    return "\n".join(linhas) + "\n"


# --- métricas de conectividade (o critério de sucesso) -----------------------

def conceitos_conectores(freq_texto: Counter, freq_escopo: Counter, teto_hub: int) -> Set[str]:
    """Os conceitos que de fato LIGAM: em 2+ notas de TEXTO e que não sejam HUB.

    Duas exclusões, e as duas medidas, não supostas:
    - df 1 (solteiro): não liga nada — 69% do vocabulário do vault está aqui.
    - HUB: ligar por `[[Cannabis]]` é ligar com um oitavo do vault. O estudo das
      figuras contou 99,7% delas "conectadas" quando o único conceito em comum era
      esse; contar hub faz a métrica MENTIR.

    As DUAS frequências existem porque são perguntas diferentes, e medi as duas
    (2026-07-31): `cannabis` está em 246 notas de TEXTO e em 1.808 de FIGURA (2.054
    no total, o número do estudo). Então:
    - o PISO (df >= 2) é sobre o TEXTO: para uma figura, conectar é alcançar o vault
      escrito, não outra figura do mesmo capítulo — as 1.817 figuras compartilham
      `[[Soil]]`/`[[Cannabis]]` entre si sem que isso ligue nada de novo;
    - o TETO de hub é sobre o ESCOPO, porque hub é o que o dono sente ao navegar. Só
      com as figuras em jogo `cannabis` cruza o teto (8,5% das notas) — no vault de
      texto sozinho ele é o 7º colocado, com 246, atrás de YOLO 475 e Cultivo 344,
      que são conceitos legítimos e não podem ser descartados junto.
    """
    return {c for c, n in freq_texto.items() if n >= 2 and freq_escopo.get(c, n) <= teto_hub}


def metricas(mapa: Dict[str, List[str]], conectores: Set[str]) -> Dict[str, float]:
    """Conectividade do vault a partir de {nota -> conceitos normalizados}."""
    freq: Counter = Counter()
    for cs in mapa.values():
        freq.update(set(cs))
    distintos = len(freq)
    solteiros = sum(1 for n in freq.values() if n == 1)
    com_malha = sum(1 for cs in mapa.values() if cs)
    # Nota INERTE: não tem NENHUM conceito conector, ou seja, a malha dela não leva a
    # lugar nenhum. É a métrica que o dono sente ao navegar.
    inertes = sum(1 for cs in mapa.values() if not (set(cs) & conectores))
    links = sum(len(set(cs)) for cs in mapa.values())
    n = max(1, len(mapa))
    return {
        "notas": len(mapa),
        "com_malha": com_malha,
        "links": links,
        "links_por_nota": links / n,
        "distintos": distintos,
        "solteiros": solteiros,
        "pct_solteiros": 100.0 * solteiros / max(1, distintos),
        "inertes": inertes,
        "pct_inertes": 100.0 * inertes / n,
    }


def _conectividade_do_subconjunto(mapa: Dict[str, List[str]], rels: Sequence[str],
                                  conectores: Set[str]) -> Tuple[int, float, float]:
    """(inertes, conectores por nota, % com >=2 conectores) dentro de `rels`.

    O recorte é só das notas tocadas: com `--limite` pequeno o número global não se
    move e esconderia o efeito real da passada. O "% com >=2" é a régua do estudo das
    figuras (14,0% hoje, 72,2% projetado), então é ela que sai no relatório.
    """
    sub = [set(mapa[r]) for r in rels if r in mapa]
    if not sub:
        return 0, 0.0, 0.0
    inertes = sum(1 for cs in sub if not (cs & conectores))
    com_dois = sum(1 for cs in sub if len(cs & conectores) >= 2)
    return (inertes,
            sum(len(cs & conectores) for cs in sub) / len(sub),
            100.0 * com_dois / len(sub))


def _imprimir_metricas(antes: Dict[str, float], depois: Optional[Dict[str, float]]) -> None:
    def col(m: Optional[Dict[str, float]], chave: str, fmt: str) -> str:
        return "" if m is None else format(m[chave], fmt).rjust(12)
    print(f"\n{'CONECTIVIDADE':38}{'ANTES':>12}{'DEPOIS' if depois else '':>12}")
    linhas = [
        ("notas analisadas", "notas", ",.0f"),
        ("notas com Malha", "com_malha", ",.0f"),
        ("links (conceitos por nota, únicos)", "links", ",.0f"),
        ("links por nota", "links_por_nota", ".2f"),
        ("conceitos DISTINTOS", "distintos", ",.0f"),
        ("conceitos SOLTEIROS (df=1)", "solteiros", ",.0f"),
        ("  % de solteiros", "pct_solteiros", ".1f"),
        ("notas INERTES (0 conceito conector)", "inertes", ",.0f"),
        ("  % de inertes", "pct_inertes", ".1f"),
    ]
    for rotulo, chave, fmt in linhas:
        print(f"{rotulo:38}{col(antes, chave, fmt)}{col(depois, chave, fmt)}")


# --- estado (retomável) ------------------------------------------------------

def carregar_estado() -> dict:
    if ESTADO.exists():
        try:
            return json.loads(ESTADO.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            telemetry.error("LINKS", "estado ilegível — recomeçando do zero", exc)
    return {"feitos": [], "mudados": 0}


def salvar_estado(estado: dict) -> None:
    estado["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    try:
        ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        telemetry.error("LINKS", "não deu para salvar o estado", exc)


def _servidor_no_ar() -> bool:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, settings.port)) == 0


# --- passada -----------------------------------------------------------------

@dataclass
class Mudanca:
    nota: Nota
    finais: List[str]
    links: str
    candidatos: List[str]
    bruto: str


def _mostrar(m: Mudanca) -> None:
    print(f"\n  ## {m.nota.titulo[:88]}   [{m.nota.rel[-52:]}]")
    print(f"     ANTES : {m.nota.malha[len(_MALHA):].strip() or '(sem Malha)'}")
    print(f"     DEPOIS: {m.links}")
    print(f"     modelo devolveu: {' '.join(m.bruto.split())[:110]}")


def _de_outro_dominio(conceito: str, dominio: str, idx: Indice) -> bool:
    """O conceito EXISTE no vault, mas em nenhuma nota do domínio desta nota.

    É a régua AUTOMÁTICA do defeito que eu tinha contado a olho: `[[Detecção de
    Objetos]]` existe (304 notas), mas nenhuma delas é da Cannabis Encyclopedia —
    então numa figura do livro ele é conceito de outro mundo. Conceito INVENTADO
    (freq 0) fica de fora desta conta de propósito: ele já é contado à parte, e
    marcá-lo aqui misturaria dois defeitos diferentes num número só.
    """
    k = _norm(conceito)
    return idx.freq.get(k, 0) >= 1 and k not in idx.por_dominio.get(dominio, {})


async def _uma_nota(llama: LlamaManager, nota: Nota, idx: Indice, recusas: Counter,
                    stats: Counter, ancorar: bool = False) -> Tuple[Optional[Mudanca], int]:
    """Uma nota: prompt -> LLM -> validação -> mescla. (mudança ou None, chars gerados).

    Todo caminho que NÃO muda a nota é contado com o motivo — recusa silenciosa é
    como se descobre tarde demais que a passada inteira não fez nada.
    """
    candidatos = escolher_candidatos(nota.titulo, nota.corpo, nota.conceitos, idx,
                                     dominio=nota.dominio if ancorar else "")
    stats["candidatos oferecidos (soma)"] += len(candidatos)
    try:
        bruto = await llama.collect(
            prompt_conceitos(nota.titulo, nota.corpo, candidatos),
            system_prompt=SYS,
            max_tokens=MAX_TOKENS_SAIDA,
            # temperatura 0: sem isto, comparar duas versões de prompt vira ruído de
            # amostragem — lição já paga caro neste repo.
            temperature=0.0,
        )
    except Exception as exc:                 # nunca engolir em silêncio
        telemetry.error("LINKS", f"LLM falhou em {nota.rel}", exc)
        recusas["erro no LLM"] += 1
        return None, 0
    novos, motivos = validar(bruto, nota.titulo, idx)
    recusas.update(motivos)
    # A separação importa: reusar um conceito SOLTEIRO não conecta nada — só o
    # reuso de conceito compartilhado (df >= 2) é o que a passada persegue.
    stats["conceitos reusados que JÁ CONECTAM (df>=2)"] += sum(
        1 for c in novos if idx.freq.get(_norm(c), 0) >= 2)
    stats["conceitos reusados mas SOLTEIROS (df=1)"] += sum(
        1 for c in novos if idx.freq.get(_norm(c), 0) == 1)
    stats["conceitos INVENTADOS (novos no vault)"] += sum(
        1 for c in novos if idx.freq.get(_norm(c), 0) == 0)
    fora = sum(1 for c in novos if _de_outro_dominio(c, nota.dominio, idx))
    stats["conceitos de OUTRO DOMÍNIO"] += fora
    stats["notas com >=1 conceito de outro domínio"] += 1 if fora else 0
    if not novos:
        # Guarda do desenho: sem NENHUM conceito válido, a nota fica intacta.
        recusas["nenhum conceito válido (nota intacta)"] += 1
        return None, len(bruto)
    finais, descartados = mesclar(nota.conceitos, novos, idx)
    links = atomos.normalizar_malha(", ".join(finais))
    if not links:
        recusas["malha vazia após normalizar"] += 1
        return None, len(bruto)
    if [_norm(c) for c in finais] == [_norm(c) for c in nota.conceitos]:
        recusas["já estava assim (idempotente)"] += 1
        return None, len(bruto)
    stats["links solteiros descartados"] += descartados
    return Mudanca(nota, finais, links, candidatos, bruto), len(bruto)


async def _rodar(alvos: List[Nota], idx: Indice, args,
                 estado: dict) -> Tuple[List[Mudanca], Counter, Counter, float, int]:
    """Percorre as notas com o LLM carregado. (mudanças, recusas, stats, s, chars)."""
    llama = LlamaManager(preparar_offline(settings.caminho_modelo_atomizacao))
    telemetry.track("LINKS", "Carregando o LLM…")
    await llama.load()

    mudancas: List[Mudanca] = []
    recusas: Counter = Counter()
    stats: Counter = Counter()
    chars = 0
    mostrados = 0
    t0 = time.perf_counter()
    try:
        for i, nota in enumerate(alvos, 1):
            m, n_chars = await _uma_nota(llama, nota, idx, recusas, stats,
                                         ancorar=args.ancorar_dominio)
            chars += n_chars
            if args.aplicar:
                # Marcada como FEITA mesmo sem mudança: com temperatura 0 a segunda
                # visita daria a mesma saída, e repeti-la seria pagar GPU por nada.
                estado["feitos"].append(nota.rel)
                if i % SALVA_ESTADO_A_CADA == 0:
                    salvar_estado(estado)
            if m is None:
                continue
            mudancas.append(m)
            if mostrados < args.amostra:
                mostrados += 1
                _mostrar(m)
            if args.aplicar:
                _escrever(m)
                estado["mudados"] = estado.get("mudados", 0) + 1
            if i % 25 == 0:
                print(f"    {i}/{len(alvos)} notas — {len(mudancas)} mudada(s)…", flush=True)
    finally:
        await llama.unload()
        if args.aplicar:
            salvar_estado(estado)
    return mudancas, recusas, stats, time.perf_counter() - t0, chars


_BACKUP: Optional[Path] = None


def _escrever(m: Mudanca) -> None:
    """Grava a linha nova, guardando o original. O vault NÃO está no git: sem cópia,
    uma passada ruim não tem desfazer (mesma decisão do `reparar_atomos_quebrados`)."""
    try:
        texto = m.nota.caminho.read_text(encoding="utf-8")
        if _BACKUP is not None:
            # Caminho RELATIVO achatado, não só o nome: o vault tem notas homônimas em
            # obras diferentes, e `md.name` faria uma sobrescrever o backup da outra.
            (_BACKUP / m.nota.rel.replace(os.sep, "__")).write_text(texto, encoding="utf-8")
        m.nota.caminho.write_text(reescrever_malha(texto, m.links), encoding="utf-8")
    except OSError as exc:
        telemetry.error("LINKS", f"não deu para gravar {m.nota.rel}", exc)


def _legenda_pobre(nota: Nota) -> bool:
    """Figura sem material para conceituar (ver MIN_PALAVRAS_LEGENDA)."""
    return reparo.palavras_de_conteudo(nota.corpo) < MIN_PALAVRAS_LEGENDA


def _compartilhados(nota: Nota, conectores: Set[str]) -> int:
    """Quantos links da nota LIGAM alguma coisa (conector: nem solteiro, nem hub)."""
    return sum(1 for c in nota.conceitos if _norm(c) in conectores)


def _selecionar(notas: List[Nota], conectores: Set[str], args, feitos: Set[str]) -> List[Nota]:
    """Aplica os filtros de escopo e o limite. Ordem embaralhada com semente FIXA:
    determinística (retomada não repete nem pula) e representativa — em ordem
    alfabética um `--limite 20` cairia todo na mesma obra e não diria nada do vault."""
    alvos = [n for n in notas if n.rel not in feitos]
    # Alvo default = SÓ TEXTO, o comportamento já validado. As figuras só entram
    # quando pedidas: o corpo delas é a legenda (ver `corpo_de_figura`).
    if args.so_figuras:
        alvos = [n for n in alvos if n.e_figura]
    elif not args.incluir_figuras:
        alvos = [n for n in alvos if not n.e_figura]
    # Figura sem legenda de verdade não vai ao modelo (mede-se, não se adivinha).
    alvos = [n for n in alvos if not (n.e_figura and _legenda_pobre(n))]
    if args.so_sem_malha:
        alvos = [n for n in alvos if not n.conceitos]
    if args.so_desconectadas:
        # O modo BARATO: nota com 2+ conceitos compartilhados já é navegável, e gastar
        # GPU nela é gastar no que não está quebrado.
        alvos = [n for n in alvos if _compartilhados(n, conectores) < 2]
    random.Random(0).shuffle(alvos)
    return alvos[: args.limite] if args.limite else alvos


def _relatar(mapa: Dict[str, List[str]], antes: Dict[str, float], alvos: List[Nota],
             mudancas: List[Mudanca], recusas: Counter, stats: Counter,
             dt: float, chars: int, conectores: Set[str]) -> None:
    """O relatório final: conectividade ANTES x DEPOIS + custo + contadores."""
    depois_mapa = dict(mapa)                 # projeção do DEPOIS sobre o vault inteiro
    for m in mudancas:
        depois_mapa[m.nota.rel] = [_norm(c) for c in m.finais]
    _imprimir_metricas(antes, metricas(depois_mapa, conectores))
    print("  (o DEPOIS projeta APENAS as notas desta passada sobre o vault inteiro —")
    print("   com --limite pequeno o movimento global é, corretamente, quase nulo)")

    rels = [n.rel for n in alvos]
    i_antes, l_antes, p_antes = _conectividade_do_subconjunto(mapa, rels, conectores)
    i_depois, l_depois, p_depois = _conectividade_do_subconjunto(depois_mapa, rels, conectores)
    print(f"\nrestrito às {len(rels)} nota(s) desta passada (onde o efeito é visível):")
    print(f"  notas inertes                    {i_antes:6,d}  ->{i_depois:6,d}")
    print(f"  conceitos CONECTORES por nota    {l_antes:6.2f}  ->{l_depois:6.2f}")
    print(f"  % com >=2 conceitos conectores   {p_antes:6.1f}  ->{p_depois:6.1f}")

    print(f"\nnotas processadas   {len(alvos):,}")
    print(f"notas mudadas       {len(mudancas):,}")
    print(f"tempo               {dt:.0f}s  ({dt / max(1, len(alvos)):.2f}s por nota)")
    print(f"candidatos/nota     {stats['candidatos oferecidos (soma)'] / max(1, len(alvos)):.1f}"
          "  (o tamanho do vocabulário que o modelo viu)")
    # tok/s APROXIMADO: o `collect` devolve texto, não a contagem de tokens do
    # llama-cpp — 1 token ~ 4 chars. Ordem de grandeza, não benchmark.
    print(f"geração             ~{chars / 4 / max(dt, 1e-9):.1f} tok/s (aprox., 1 tok ≈ 4 chars)")
    for titulo, contador in (("como o modelo se comportou:", stats),
                             ("recusado pela validação em Python (não pelo modelo):", recusas)):
        if contador:
            print(f"\n{titulo}")
            for motivo, n in contador.most_common():
                print(f"  {n:7,d}  {motivo}")


def _argumentos() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aplicar", action="store_true", help="grava no vault (sem isto é DRY-RUN)")
    ap.add_argument("--limite", type=int, default=0, help="máx. de notas nesta passada (0 = todas)")
    ap.add_argument("--so-obra", default="", help="só notas cuja `origem` contenha isto")
    ap.add_argument("--so-sem-malha", action="store_true", help="só notas sem conceito nenhum")
    ap.add_argument("--so-desconectadas", action="store_true",
                    help="só notas com menos de 2 conceitos COMPARTILHADOS (o modo barato)")
    ap.add_argument("--so-metricas", action="store_true", help="só mede a conectividade (sem GPU)")
    ap.add_argument("--incluir-figuras", action="store_true",
                    help="notas de FIGURA também entram (o corpo delas é a legenda em PT)")
    ap.add_argument("--so-figuras", action="store_true", help="SÓ notas de figura")
    ap.add_argument("--ancorar-dominio", action="store_true",
                    help="só oferece conceitos já usados na MESMA obra/balde da nota "
                         "(corta o casamento de token que atravessa cultivo x dev)")
    ap.add_argument("--amostra", type=int, default=15, help="quantos antes/depois imprimir")
    ap.add_argument("--sem-backup", action="store_true", help="não copia os originais (NÃO recomendado)")
    ap.add_argument("--reiniciar", action="store_true", help="ignora o estado e recomeça do zero")
    ap.add_argument("--cpu", action="store_true",
                    help="roda o LLM na CPU e libera o guard do servidor — MUITO mais lento; "
                         "existe para tirar uma amostra com o servidor no ar, não para a passada real")
    return ap.parse_args()


def main() -> int:
    global _BACKUP
    args = _argumentos()

    print(f"vault = {settings.caminho_obsidian}")
    notas, motivos = varrer(args.so_obra)
    texto = [n for n in notas if not n.e_figura]
    figuras = [n for n in notas if n.e_figura]
    print(f"{len(texto)} nota(s) de TEXTO + {len(figuras)} de FIGURA")
    for m, n in motivos.most_common():
        print(f"  {n:7,d}  fora: {m}")

    # O vocabulário e a régua de "conector" saem SÓ do texto: para uma figura,
    # conectar é alcançar o vault escrito. Se as figuras entrassem no vocabulário, o
    # [[Soil]] delas (432 dos 486 conceitos de figura não existem em nota de texto)
    # viraria candidato a ser oferecido — espalhando o metadado em inglês pelo vault.
    idx = indexar(texto)
    em_escopo = notas if (args.incluir_figuras or args.so_figuras) else texto
    mapa = {n.rel: [_norm(c) for c in n.conceitos] for n in em_escopo}
    freq_escopo: Counter = Counter()
    for cs in mapa.values():
        freq_escopo.update(set(cs))
    teto_hub = max(2, int(len(em_escopo) * HUB_FRACAO_MAX))
    conectores = conceitos_conectores(idx.freq, freq_escopo, teto_hub)
    hubs = sorted((n, c) for c, n in freq_escopo.items() if n > teto_hub and idx.freq.get(c, 0) >= 2)
    print(f"\nvocabulário: {len(idx.freq):,} conceitos distintos, "
          f"{sum(1 for v in idx.freq.values() if v >= MIN_FREQ_CANDIDATO):,} com "
          f"frequência >= {MIN_FREQ_CANDIDATO} (os que podem ser oferecidos ao modelo)")
    print(f"HUBS (df > {teto_hub:,} = {HUB_FRACAO_MAX:.0%} das notas em escopo): NÃO contam como conexão — "
          + (", ".join(f"{idx.canonica.get(c, c)} {n:,}" for n, c in reversed(hubs[-6:])) or "nenhum"))

    antes = metricas(mapa, conectores)
    _imprimir_metricas(antes, None)
    desconectadas = sum(1 for n in em_escopo if _compartilhados(n, conectores) < 2)
    print(f"\ntriagem: {desconectadas:,} nota(s) com menos de 2 conceitos CONECTORES "
          f"(as que a Malha não conecta hoje)\n         `--so-desconectadas` gasta GPU só nelas")
    if figuras:
        _i, _l, pct = _conectividade_do_subconjunto(
            {n.rel: [_norm(c) for c in n.conceitos] for n in figuras},
            [n.rel for n in figuras], conectores)
        print(f"         das {len(figuras):,} FIGURAS, {pct:.1f}% têm >=2 conceitos conectores hoje")
        pobres = sum(1 for n in figuras if _legenda_pobre(n))
        print(f"         {pobres:,} delas ficam de fora: legenda com < {MIN_PALAVRAS_LEGENDA} "
              "palavras de conteúdo não é material para conceituar")
    if args.so_metricas:
        print("\n(só-métricas: nada foi chamado nem escrito)")
        return 0

    estado = {"feitos": [], "mudados": 0} if args.reiniciar else carregar_estado()
    feitos = set(estado.get("feitos", []))
    if feitos:
        print(f"\nestado: {len(feitos):,} nota(s) já processada(s) — serão puladas")
    alvos = _selecionar(notas, conectores, args, feitos)
    print(f"{len(alvos)} nota(s) nesta passada")
    if not alvos:
        return 0
    if args.cpu:
        # A 3080 tem 10GB e o servidor já ocupa boa parte; com n_gpu_layers=0 esta
        # passada não disputa VRAM nenhuma. O preço é o relógio — o tempo por nota
        # medido assim NÃO serve para estimar o custo da passada real.
        settings.n_gpu_layers = 0
        print("\nMODO CPU: sem disputa de VRAM, mas o tempo por nota aqui NÃO é o da GPU.")
    elif _servidor_no_ar():
        print("\nO servidor está NO AR — os dois disputariam a mesma GPU. Feche-o (ou use --cpu).")
        return 3
    if args.aplicar and not args.sem_backup:
        _BACKUP = Path("dados/backups") / f"links_{datetime.now():%Y%m%d_%H%M%S}"
        _BACKUP.mkdir(parents=True, exist_ok=True)
        print(f"backup dos originais em: {_BACKUP}")

    mudancas, recusas, stats, dt, chars = asyncio.run(_rodar(alvos, idx, args, estado))
    _relatar(mapa, antes, alvos, mudancas, recusas, stats, dt, chars, conectores)
    if not args.aplicar:
        print("\n(DRY-RUN: NADA foi escrito. Repita com --aplicar.)")
    elif mudancas:
        print("\nRode `python scripts/reindexar.py` — a malha do índice só muda depois.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
