"""
Importa o histórico do Gemini (JSONs) como átomos Zettelkasten de verdade.

POR QUE EXISTE
--------------
O vault tem 7.268 notas no padrão `_Pt<N>_`, produto de um import mecânico que cortou
as conversas em fragmentos e derivou títulos sem contexto. O resultado:

    # Economia necessária
    **Origem:** [Import_Gemini_Otimizando-Munição-no-Tarkov-A.md]
    Precisamos economizar pelo menos **166,2**.      <- 166,2 de quê?

    # Gemini                                          <- título literalmente errado
    ...preços da AWS, instância g5.xlarge, Llama 3 (8B)...

Isso não é cosmético. `split_markdown` indexa o título junto com o corpo
(strip_headers=False), então o título ENTRA no vetor: medido, a nota do Tarkov acima
foi recuperada para uma pergunta sobre "a economia da máquina de lavar louça", junto
com uma sobre aluguel de trator — três domínios casados pela palavra "economia".

Não dá para consertar o título de um fragmento que perdeu o antecedente: a informação
não está lá. Mas a FONTE existe (64 JSONs, 9,1M de chars). Então regeneramos da fonte
em vez de remendar o derivado.

COMO EVITA REPETIR O ERRO
-------------------------
1. TEMA INJETADO: o nome do arquivo é o assunto da conversa e vai no prompt, com a
   regra de auto-contenção (prompts.prompt_sintese_import).
2. JANELA POR TURNO: corta em fronteira de turno, nunca no meio de uma frase.
3. FORMATO IMPOSTO: `agent.normalizar_atomo` garante '## ' + tags no Python — o A/B
   provou que nenhum modelo entrega o formato de forma confiável ("para que ele não
   alucine", nas palavras do dono).
4. Tags `#zettelkasten_atomico #memoria_legada` — é o passado do usuário, não
   curiosidade auto-colhida: não entra no ciclo de promoção do #conhecimento_novo.

Escreve numa pasta NOVA e não toca em nada existente. Retomável: mata e reinicia à
vontade que ele pula o que já fez (o estado é salvo a CADA janela).

    python scripts/importar_gemini.py --listar                      # plano, sem rodar
    python scripts/importar_gemini.py --so "Calculando-Velocidade"  # prova de fogo
    python scripts/importar_gemini.py                               # tudo (~4h)

ONDE O TEMPO VAI (medido antes de rodar, com 2885 janelas):
    decode  1,70 M tokens -> ~258 min   <- 93% do lote
    prefill 4,73 M tokens ->   18 min
    assunto (estágio 1)   ->    4 min
    I/O     7,8 MB total  -> irrelevante (~26 s) — este lote NÃO é I/O-bound
A tela mostra o acumulado real a cada 25 janelas (ver `Relogio.resumo`), para o
operador decidir onde vale atacar. O único gargalo com folga é o decode: batching
(llama-server -np N) daria 3-4x, ao custo de um binário novo.

IMPORTANTE: o servidor (main.py) precisa estar PARADO — a VRAM é a mesma.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import re
import time
import threading
from collections import deque
from datetime import datetime
from typing import Deque, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import prompts  # noqa: E402
import textutils  # noqa: E402
from agent import _slug_titulo, dividir_atomos, normalizar_atomo  # noqa: E402
from config import settings  # noqa: E402
from llm import LlamaManager  # noqa: E402
from rag import EmbeddingProvider, strip_frontmatter  # noqa: E402
from telemetry import telemetry  # noqa: E402

DIR_JSON = os.path.join(settings.caminho_obsidian, "gemini")
DIR_SAIDA = os.path.join(settings.caminho_obsidian, "Importado_Gemini")
ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "importar_gemini.estado.json")

# Janela de conversa por passada. Cabe MUITO mais no n_ctx=8192, e a primeira versão
# usava 9000 — mas janela grande pede 10+ átomos numa passada só, e o modelo larga a
# regra de auto-contenção por volta do quinto (medido). Janela menor = menos átomos
# por passada = menos deriva. Custa mais passadas; o assunto (estágio 1) também fica
# mais preciso, porque um trecho curto não muda de tema no meio.
JANELA_CHARS = 4000
TAGS_IMPORT: Tuple[str, ...] = (prompts.TAG_ATOMO, "#memoria_legada")
# Similaridade de cosseno acima da qual dois átomos são "praticamente clones".
# Calibrado em 122k pares reais: mediana 0.350, p99 0.783 — os clones vivem em
# >=0.95. Conservador de propósito: funde cópia, não ideia parecida.
LIMIAR_CLONE = 0.95
# Vigia de vazamento. O que DEVE crescer neste lote é só a matriz da dedup (~26k átomos
# × 384 floats ≈ 40 MB) e a lista do estado (~90 KB). Qualquer coisa muito além disso é
# vazamento. Folga generosa: parar por ruído do alocador do Python seria pior que o mal.
TOL_RSS_MB = 1500.0     # crescimento de RSS tolerado desde o baseline
TOL_VRAM_MB = 600.0     # VRAM em uso NÃO deve crescer: o contexto do llama.cpp é fixo


def tema_do_arquivo(caminho: str) -> str:
    """'Otimizando-Munição-no-Tarkov-A.json' -> 'Otimizando Munição no Tarkov A'.

    O nome do arquivo é a única fonte confiável do ASSUNTO da conversa — e é
    justamente o que o import antigo jogou fora.
    """
    nome = os.path.splitext(os.path.basename(caminho))[0]
    return nome.replace("-", " ").replace("_", " ").strip()


def ler_turnos(caminho: str) -> List[Tuple[str, str]]:
    """Devolve [(papel, texto)] só do que é conhecimento. [] se não for uma conversa.

    Descarta `thinking` (o rascunho interno do Gemini, 0,7M de chars: não é o que ele
    respondeu, e atomizar raciocínio abandonado geraria contradição na base) e os
    blocos não-textuais (imagem, widget, card de compra).

    TOLERANTE A LIXO na pasta: nem todo .json ali é uma conversa exportada. Achado no
    acervo real — `danielpvp22_mente_digital_6dadd2.json` é um SBOM SPDX (gerado pelo
    dependency graph do GitHub) que caiu na pasta por acaso, e derrubava o lote inteiro
    com AttributeError. Um arquivo estranho pula; não mata 5h de import.
    """
    try:
        with open(caminho, encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        telemetry.warn("IMPORT", f"{os.path.basename(caminho)}: JSON ilegível ({exc}) — pulado.")
        return []
    if not isinstance(dados, list):
        telemetry.warn("IMPORT", f"{os.path.basename(caminho)}: não é uma conversa exportada — pulado.")
        return []

    turnos: List[Tuple[str, str]] = []
    for msg in dados:
        if not isinstance(msg, dict):
            continue
        conteudos = msg.get("contents")
        if not isinstance(conteudos, list):
            continue
        papel = "Usuário" if msg.get("role") == "user" else "Assistente"
        partes = [
            str(c.get("content", "")).strip()
            for c in conteudos
            if isinstance(c, dict) and c.get("type") == "text" and str(c.get("content", "")).strip()
        ]
        if partes:
            turnos.append((papel, "\n".join(partes)))
    return turnos


def _partir_turno(papel: str, texto: str, limite: int) -> List[str]:
    """Parte um turno gigante em pedaços <= `limite`, cortando em quebra de linha ou
    espaço (nunca no meio de uma palavra). Cada pedaço recarrega o papel.

    Existe porque a 1ª versão deixava um turno grande virar uma janela sozinho,
    apostando que o llama.cpp truncaria. Ele NÃO trunca — levanta
    `ValueError: Requested tokens (17660) exceed context window of 8192`. E o estrago
    era silencioso: o worker morria, `collect` devolvia "", a janela era marcada como
    concluída no estado e produzia ZERO átomos. Um paste de código grande apagava um
    pedaço da conversa do import sem avisar.
    """
    prefixo = f"**{papel}:** "
    # Sem piso arbitrário: o pedaço + o prefixo têm que caber no limite, sempre — é
    # essa a garantia que o janelar() promete e que o n_ctx cobra.
    util = max(limite - len(prefixo), 1)
    pedacos: List[str] = []
    resto = texto
    while resto:
        if len(resto) <= util:
            pedacos.append(prefixo + resto)
            break
        janela = resto[:util]
        corte = max(janela.rfind("\n"), janela.rfind(" "))
        if corte <= 0:
            corte = util                       # palavra única gigante: corta seco
        pedacos.append(prefixo + resto[:corte].rstrip())
        resto = resto[corte:].lstrip()
    return pedacos


_PREFIXO_USUARIO = "**Usuário:**"


def _fechar_janela(blocos: List[str]) -> Tuple[List[str], List[str]]:
    """Divide os blocos em (emitir, adiar): tira do fim os turnos de USUÁRIO pendurados.

    Por que: uma janela que TERMINA numa pergunta é um convite para o modelo respondê-la
    com o conhecimento dele. Medido — a janela 1 de "Como Funciona a Pool de Mineração
    Zcash" acaba em "qual é o tempo liquidez de cada pool?" (a resposta está na janela
    seguinte) e o modelo produziu "varia de 1 a 7 dias": número que não existe em lugar
    nenhum do texto. Ele ainda ignorou todo o conteúdo real da janela para inventar isso.
    Movendo a pergunta órfã para a próxima janela, ela chega junto da resposta dela.

    Nunca adia tudo: se só houver turnos de usuário, emite como está (senão o conteúdo
    ficaria sendo empurrado para sempre e a conversa se perderia).
    """
    corte = len(blocos)
    while corte > 0 and blocos[corte - 1].startswith(_PREFIXO_USUARIO):
        corte -= 1
    if corte == 0:
        return blocos, []                    # só perguntas: emite, não há o que adiar
    return blocos[:corte], blocos[corte:]


def janelar(turnos: List[Tuple[str, str]], limite: int = JANELA_CHARS) -> List[str]:
    """Agrupa turnos em janelas <= `limite`, cortando em fronteira de turno.

    Puro/testável. Duas garantias, cada uma paga com um bug real:
    1. nenhuma janela excede `limite` — um turno maior é PARTIDO (`_partir_turno`),
       senão estoura o n_ctx e a janela some em silêncio;
    2. nenhuma janela termina numa pergunta do usuário (`_fechar_janela`), senão o
       modelo a responde por conta própria e inventa o fato.
    """
    janelas: List[str] = []
    atual: List[str] = []
    tam = 0
    for papel, texto in turnos:
        for bloco in _partir_turno(papel, texto, limite):
            if atual and tam + len(bloco) > limite:
                emitir, adiar = _fechar_janela(atual)
                if emitir:
                    janelas.append("\n\n".join(emitir))
                atual = adiar                # a pergunta órfã abre a próxima janela
                tam = sum(len(b) for b in atual)
                if atual and tam + len(bloco) > limite:
                    # A sobra adiada + o novo bloco não cabem. A garantia de TAMANHO
                    # vence a de "não terminar em pergunta": estourar o n_ctx some com
                    # a janela inteira, terminar em pergunta só arrisca um átomo.
                    janelas.append("\n\n".join(atual))
                    atual, tam = [], 0
            atual.append(bloco)
            tam += len(bloco)
    if atual:
        janelas.append("\n\n".join(atual))   # a última vai inteira: não há "próxima"
    return janelas


class Deduplicador:
    """Funde átomos praticamente idênticos — uma ideia, uma nota.

    POR QUE É INDISPENSÁVEL AQUI: uma conversa longa discute a mesma ideia em vários
    trechos, e cada janela a re-atomiza. Medido numa parcial de 495 átomos: **37**
    tinham o título "Race Condition de Threads: Arquitetura de Interface", e 15
    arquivos eram byte-a-byte idênticos. Sem isto, o import entrega uma base redundante
    — e redundância no RAG é pior que inútil: o `rag_top_k=40` gasta o orçamento de
    contexto repetindo o mesmo fato e empurra para fora os átomos que faltavam.

    LIMIAR: medido sobre 122k pares reais, a mediana de similaridade é 0.350 e o p99
    é 0.783 — clones vivem em ≥0.95, bem separados. O default é conservador de
    propósito: funde só o que é quase cópia, e nunca duas ideias distintas que por
    acaso falam do mesmo assunto.

    Compara o texto INDEXADO (sem frontmatter): é exatamente o que vai virar vetor no
    Chroma, então dois átomos indistinguíveis aqui são indistinguíveis lá.
    """

    def __init__(self, emb, limiar: float) -> None:
        self._emb = emb
        self._limiar = limiar
        self._vecs = None          # np.ndarray (N, dim), já normalizado
        self.fundidos = 0
        self._lock = threading.Lock()

    def _add(self, vecs) -> None:
        import numpy as np
        with self._lock:
            self._vecs = vecs if self._vecs is None else np.vstack([self._vecs, vecs])

    def semear(self, textos: List[str]) -> None:
        """Carrega os átomos já em disco. Faz a dedup funcionar através do resume:
        sem isto, matar e reiniciar o lote reintroduziria as duplicatas."""
        if not textos:
            return
        import numpy as np
        vs = np.array(self._emb.embed_documents(textos), dtype="float32")
        vs /= np.linalg.norm(vs, axis=1, keepdims=True).clip(min=1e-9)
        self._add(vs)

    def filtrar(self, textos: List[str]) -> List[bool]:
        """Devolve, para cada texto, se ele deve ser MANTIDO. Compara contra tudo que
        já foi mantido — inclusive os anteriores desta mesma chamada (uma janela pode
        repetir a ideia dentro dela mesma)."""
        import numpy as np
        if not textos:
            return []
        vs = np.array(self._emb.embed_documents(textos), dtype="float32")
        vs /= np.linalg.norm(vs, axis=1, keepdims=True).clip(min=1e-9)
        manter: List[bool] = []
        for v in vs:
            with self._lock:
                clone = self._vecs is not None and len(self._vecs) and float((self._vecs @ v).max()) >= self._limiar
                manter.append(not clone)
                if clone:
                    self.fundidos += 1
            if not clone:
                self._add(v.reshape(1, -1))
        return manter


# Números com 2+ dígitos ou com decimal. Dígito solto ('1', '7') fica de FORA de
# propósito: '1' e '7' aparecem em qualquer texto (listas numeradas, datas), então
# exigi-los daria falso positivo constante. O preço é assumido abaixo, no docstring.
_NUM_RE = re.compile(r"\d+[.,]\d+|\d{2,}")


def _numeros(texto: str) -> set:
    """Números normalizados ('2,1' e '2.1' são o mesmo; '1.200' e '1200' também)."""
    achados = set()
    for n in _NUM_RE.findall(texto):
        n = n.replace(".", "").replace(",", ".") if n.count(",") == 1 and "." not in n else n
        achados.add(n.replace(".", "").lstrip("0") or "0")
    return achados


def e_fiel(atomo: str, trecho: str) -> Tuple[bool, str]:
    """Todo número do átomo aparece no texto de origem? Puro/testável.

    O PORTÃO DE QUALIDADE do import. Nasceu de uma auditoria manual: a conversa cuja
    resposta foi truncada na origem virou o átomo "a circunferência da roda de 700c é
    de ~2,1 metros" — número que não existe na fonte. E uma janela terminada numa
    pergunta virou "a liquidez varia de 1 a 7 dias", idem. Número inventado é o modo de
    falha mais perigoso (entra no vault com a autoridade do histórico do usuário) e,
    por sorte, o mais fácil de pegar mecanicamente: se o átomo cita um número, ele tem
    que estar no texto de onde saiu.

    LIMITES, honestamente: só pega número de 2+ dígitos ou com decimal. Dígito solto
    ('1 a 7 dias') passa, porque '1' e '7' aparecem em qualquer texto e exigi-los
    reprovaria tudo. E não pega alucinação SEM número ("o Stratum foi criado pela
    Nvidia"). É uma rede, não uma prova — mas pega justamente a classe que a auditoria
    manual mostrou ser a mais comum e a mais cara.
    """
    inventados = _numeros(atomo) - _numeros(trecho)
    if inventados:
        return False, "número(s) fora da fonte: " + ", ".join(sorted(inventados))
    return True, ""


def garantir_assunto(bloco: str, assunto: str) -> str:
    """Se o título do átomo não menciona o assunto, PREFIXA o assunto. Puro/testável.

    A trava determinística, na mesma filosofia do `normalizar_atomo`: o LLM entrega a
    ideia, o Python garante a propriedade. Medido, o modelo obedece a regra de
    auto-contenção nos primeiros átomos e a larga por volta do quinto — então
    "## Pressão de Oferta" vira "## Economia do jogo NFT Bomb Crypto: Pressão de
    Oferta". Feio? Um pouco. Mas o título é INDEXADO junto com o corpo
    (split_markdown, strip_headers=False), então isto é literalmente o que faz o átomo
    ser recuperável pelo assunto certo — e o que impede que ele seja recuperado pelo
    assunto ERRADO, que é o bug que originou tudo isto (a nota do Tarkov casando com
    "economia da máquina de lavar louça" pela palavra 'economia').

    Não mexe quando o modelo já fez o trabalho: se houver qualquer keyword em comum
    entre título e assunto, o título fica intacto.
    """
    chaves_assunto = textutils.palavras_chave(assunto)
    if not chaves_assunto:
        return bloco
    linhas = bloco.strip().splitlines()
    for i, ln in enumerate(linhas):
        if not ln.lstrip().startswith("## "):
            continue
        titulo = ln.lstrip("#").strip()
        if textutils.tem_sobreposicao(chaves_assunto, textutils.palavras_chave(titulo)):
            return bloco                      # o modelo já nomeou o assunto
        linhas[i] = f"## {assunto}: {titulo}"
        return "\n".join(linhas)
    return bloco


def checar_vram() -> None:
    """Avisa ALTO se não houver VRAM para o modelo caber inteiro na GPU.

    Por que existe: o llama.cpp NÃO reclama quando falta VRAM — no Windows (WDDM) a
    alocação transborda para a RAM do sistema e os pesos passam a ser lidos por PCIe
    (~25 GB/s) em vez da VRAM (760 GB/s). Ele só fica lento, em silêncio.

    Medido neste projeto: um processo ZUMBI de um import anterior segurava 8,4 GB, e o
    lote seguinte rodava a **40 tok/s em vez de 116** — 2,9x mais lento, por 4 horas,
    sem nenhum sinal na tela. Nenhuma otimização de código compensa isso, então o lote
    avisa antes de começar em vez de deixar o operador caçar fantasma.
    """
    try:
        import subprocess
        livre = int(subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().split("\n")[0])
    except Exception:
        return                                  # sem nvidia-smi: segue, é só um aviso

    try:
        precisa = os.path.getsize(settings.caminho_modelo_llama) / 1e6 + 900   # pesos + KV/compute
    except OSError:
        return
    if livre < precisa:
        telemetry.warn("IMPORT", "=" * 72)
        telemetry.warn("IMPORT", f"VRAM INSUFICIENTE: {livre} MB livres, o modelo precisa de ~{precisa:.0f} MB.")
        telemetry.warn("IMPORT", "O llama.cpp NÃO vai falhar — vai derramar os pesos para a RAM e ficar")
        telemetry.warn("IMPORT", "~3x MAIS LENTO (medido: 40 tok/s vs 116), sem avisar. Feche o servidor")
        telemetry.warn("IMPORT", "(main.py) e procure processos zumbis de imports anteriores:")
        telemetry.warn("IMPORT", '  powershell "Get-CimInstance Win32_Process -Filter \\"Name=\'python.exe\'\\""')
        telemetry.warn("IMPORT", "=" * 72)


# Nome de arquivo: só o título (que já carrega o assunto, via garantir_assunto) + a
# posição. O caminho até aqui gasta ~58 chars e o Windows corta em MAX_PATH=260, então
# o limite real é a legibilidade no Obsidian, não o SO.
SLUG_ATOMO = 60


def _nome_atomo(origem: str, atomo: str, seq: int, i: int) -> str:
    """Nome de arquivo legível. A UNICIDADE é do `_caminho_livre`, não daqui.

    Tentei antes pôr o slug da conversa no nome, para matar a colisão. Ficou
    `como_funciona_a_pool_de_pool_de_mineracao_de_zcash_distribuicao_da_tarefa_0_1.md`
    — 84 chars dizendo a mesma coisa duas vezes, porque o `garantir_assunto` JÁ prefixa
    o assunto no título. Redundância, e o próprio dono já tinha reclamado do tamanho.

    A separação certa é: o TÍTULO identifica a ideia (e já traz o assunto), o `origem:`
    do frontmatter diz de que conversa veio, e o `_caminho_livre` garante que nada é
    sobrescrito. `origem` fica na assinatura porque o chamador a tem e um dia pode
    voltar a importar — mas hoje ela não entra no nome de propósito.
    """
    return f"{_slug_titulo(atomo, SLUG_ATOMO)}_{seq}_{i}.md"


def _caminho_livre(nome: str) -> str:
    """Cinto de segurança: nunca sobrescrever um átomo existente, aconteça o que for."""
    base, ext = os.path.splitext(nome)
    caminho = os.path.join(DIR_SAIDA, nome)
    k = 1
    while os.path.exists(caminho):
        caminho = os.path.join(DIR_SAIDA, f"{base}__{k}{ext}")
        k += 1
    return caminho


def carregar_estado() -> dict:
    if os.path.exists(ESTADO):
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    return {"feitos": [], "atomos": 0}


def salvar_estado(estado: dict) -> None:
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


class Relogio:
    """Acumula onde o lote gasta tempo, para o operador CAÇAR GARGALO durante a corrida.

    Existe porque o orçamento medido antes de rodar dizia: 93% decode, 6% prefill, e o
    I/O nem aparece (7,8 MB em ~4h). Mas isso era estimativa — estes contadores são a
    medição real, ao vivo, e é o que diz se vale a pena atacar batching, prompt, ou
    nada. `tokens` é contagem real (a stream é consumida token a token), não estimativa.
    """

    def __init__(self) -> None:
        self.assunto_s = self.prefill_s = self.decode_s = self.dedup_s = self.io_s = 0.0
        self.tokens_out = 0
        self.janelas = 0
        self.atomos = 0
        self.infieis = 0        # átomos descartados por citar número fora da fonte
        self._lock = threading.Lock()

    @property
    def total_s(self) -> float:
        return self.assunto_s + self.prefill_s + self.decode_s + self.dedup_s + self.io_s

    @property
    def tok_s(self) -> float:
        """tok/s do DECODE puro — o número comparável com o baseline de 110 do projeto."""
        return self.tokens_out / self.decode_s if self.decode_s else 0.0
        
    def add_metricas(self, pref_s: float, dec_s: float, tok: int, dd_s: float, io_s: float, ass_s: float = 0.0):
        with self._lock:
            self.prefill_s += pref_s
            self.decode_s += dec_s
            self.tokens_out += tok
            self.dedup_s += dd_s
            self.io_s += io_s
            self.assunto_s += ass_s

    def add_atomos(self, n: int):
        with self._lock:
            self.janelas += 1
            self.atomos += n

    def add_infieis(self, n: int):
        with self._lock:
            self.infieis += n

    def resumo(self) -> str:
        t = self.total_s or 1e-9
        return (
            f"ONDE VAI O TEMPO: decode {100*self.decode_s/t:.0f}% | "
            f"prefill {100*self.prefill_s/t:.0f}% | assunto {100*self.assunto_s/t:.0f}% | "
            f"dedup {100*self.dedup_s/t:.0f}% | io {100*self.io_s/t:.0f}%"
            f"   ||   {self.tok_s:.0f} tok/s de decode (baseline 110) | "
            f"{self.tokens_out/max(self.janelas,1):.0f} tok/janela | "
            f"{self.atomos/max(self.janelas,1):.1f} átomos/janela | "
            f"{self.infieis} descartados por infidelidade"
        )


class Vigia:
    """Vigia RSS e VRAM e MANDA PARAR o lote ao primeiro sinal de vazamento.

    O problema de um vazamento MÍNIMO é que ele é invisível justamente enquanto ainda
    dá para agir: 2 MB por janela não é nada na janela 50, e são 5,7 GB na 2885 — aí o
    SO começa a paginar, ou a VRAM transborda e os pesos vão para a RAM (o bug de 2,9x
    que já nos custou 4 horas em silêncio). Por isso aqui não se mede só o CRESCIMENTO,
    projeta-se ele até o fim do lote. É a projeção que torna o vazamento mínimo visível.

    Parar é barato: o estado é salvo a cada janela, então abortar e retomar não perde
    nada. Descobrir 3 horas depois que o lote rodou em swap, custa tudo.
    """

    # Amostras da JANELA DESLIZANTE. A 1ª versão usava âncora FIXA (janela 50) e
    # extrapolava dali em linha reta — e abortou um lote saudável na janela ~244,
    # projetando +1,7 GB. Medido depois, o processo apenas ASSENTAVA: a inclinação cai
    # 14x entre a janela 50 e a 3386 (0,099 -> 0,007 MB/janela) enquanto o alocador, os
    # buffers CUDA e o llama.cpp acomodam. Âncora fixa não distingue "assentando" de
    # "vazando"; janela deslizante sim: se assenta, a inclinação RECENTE vai a zero; se
    # vaza de verdade, ela se mantém. É a inclinação recente que vale.
    JANELA_AMOSTRAS = 150
    MIN_AMOSTRAS = 300

    def __init__(self, total_janelas: int, tol_rss_mb: float, tol_vram_mb: float,
                 amostrador=None) -> None:
        self.total = max(total_janelas, 1)
        self.tol_rss, self.tol_vram = tol_rss_mb, tol_vram_mb
        self.base_rss = self.base_vram_usada = None
        self.n = 0
        self._amostras: Deque[Tuple[int, float]] = deque(maxlen=self.JANELA_AMOSTRAS)
        # Injetável para teste: o projeto já faz isso com o clock do LatencyTracker.
        # Sem isso, esta lógica só seria exercitável com 4h de GPU.
        self._amostrador = amostrador
        self._proc = None
        try:
            import psutil
            self._proc = psutil.Process()
        except Exception:
            telemetry.warn("VIGIA", "psutil ausente — SEM vigilância de RSS (pip install psutil).")

    def _rss_mb(self) -> Optional[float]:
        if self._amostrador is not None:
            return self._amostrador()
        if self._proc is None:
            return None
        try:
            return self._proc.memory_info().rss / 1e6
        except Exception:
            return None

    def _vram_usada_mb(self) -> Optional[float]:
        """Via torch (já carregado pelos embeddings) em vez de nvidia-smi: é in-process
        e custa microssegundos, então dá para amostrar a CADA janela sem pesar."""
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            livre, total = torch.cuda.mem_get_info()
            return (total - livre) / 1e6
        except Exception:
            return None

    def ancorar(self) -> None:
        """Marco zero DEPOIS do load e do warm-up — senão o baseline inclui a subida
        do modelo e todo crescimento fica escondido dentro dela."""
        self.base_rss, self.base_vram_usada = self._rss_mb(), self._vram_usada_mb()
        telemetry.track("VIGIA", f"Baseline: RSS {(self.base_rss or 0)/1000:.1f} GB | "
                                 f"VRAM em uso {(self.base_vram_usada or 0)/1000:.1f} GB")

    def _inclinacao(self) -> Optional[float]:
        """MB por janela nas últimas JANELA_AMOSTRAS. None se ainda não há amostras."""
        if len(self._amostras) < self.JANELA_AMOSTRAS:
            return None
        (n0, r0), (n1, r1) = self._amostras[0], self._amostras[-1]
        return (r1 - r0) / (n1 - n0) if n1 > n0 else None

    def _proj(self) -> Optional[float]:
        """Quanto ainda vai crescer daqui até o fim, no ritmo RECENTE. É a projeção —
        não o crescimento absoluto — que torna um vazamento MÍNIMO visível a tempo:
        0,5 MB/janela é invisível na janela 250 e é +1,7 GB na 3386."""
        inc = self._inclinacao()
        return None if inc is None else inc * (self.total - self.n)

    def checar(self) -> Optional[str]:
        """Devolve o MOTIVO para parar, ou None. Chamado a cada janela."""
        self.n += 1
        rss, vram = self._rss_mb(), self._vram_usada_mb()
        if rss is not None:
            self._amostras.append((self.n, rss))

        if rss is not None and self.base_rss is not None:
            if rss - self.base_rss > self.tol_rss:
                return f"RSS cresceu {rss - self.base_rss:.0f} MB (limite {self.tol_rss:.0f})"
            proj = self._proj()
            if self.n >= self.MIN_AMOSTRAS and proj is not None and proj > self.tol_rss:
                return (f"RSS cresce {self._inclinacao():.2f} MB/janela nas últimas "
                        f"{self.JANELA_AMOSTRAS} -> +{proj/1000:.1f} GB até o fim")
        if vram is not None and self.base_vram_usada is not None:
            if vram - self.base_vram_usada > self.tol_vram:
                return f"VRAM em uso cresceu {vram - self.base_vram_usada:.0f} MB (limite {self.tol_vram:.0f})"
        return None

    def status(self) -> str:
        rss, vram = self._rss_mb(), self._vram_usada_mb()
        if rss is None or self.base_rss is None:
            return "indisponível (psutil ausente)"
        inc, proj = self._inclinacao(), self._proj()
        txt = (f"{inc:+.3f} MB/janela recente, proj {proj:+.0f} MB até o fim"
               if inc is not None else "inclinação: aquecendo")
        return (f"RSS {rss/1000:.2f} GB ({rss - self.base_rss:+.0f} MB desde o início; "
                f"{txt}) | VRAM em uso {(vram or 0)/1000:.2f} GB")


def _eta(feitas: int, total: int, decorrido: float) -> str:
    if not feitas:
        return "?"
    seg = (total - feitas) * (decorrido / feitas)
    h, m = int(seg // 3600), int((seg % 3600) // 60)
    return f"{h}h{m:02d}" if h else f"{m}min"


async def _gerar_medido(llama, prompt: str, system: str, max_tokens: int,
                        temperature=None) -> Tuple[str, int, float, float]:
    """Como `collect`, mas devolve (texto, tokens, ttft_s, decode_s) SEPARADOS.

    A separação é o ponto: um tok/s que divide os tokens pelo tempo TOTAL mistura
    prefill com decode e esconde qual dos dois é o gargalo — que é justamente a
    pergunta que este lote existe para responder. Ex.: 250 tokens em 7s dá "35 tok/s"
    e parece um decode lento, quando na verdade são 0,4s de prefill + 2,3s de decode
    a 110 tok/s. Aqui o TTFT é o custo do prompt e o decode é o custo da resposta.
    """
    t0 = time.perf_counter()
    ttft = None
    pedacos: List[str] = []
    async for tok in llama.stream(prompt, max_tokens=max_tokens, system_prompt=system,
                                  temperature=temperature):
        if ttft is None:
            ttft = time.perf_counter() - t0
        pedacos.append(tok)
    total = time.perf_counter() - t0
    ttft = ttft if ttft is not None else total
    return "".join(pedacos), len(pedacos), ttft, max(total - ttft, 1e-9)


async def descobrir_assunto(llama, rel: Relogio, trecho: str, fallback: str) -> str:
    """ESTÁGIO 1: o assunto sai do TRECHO, não do nome do arquivo (a conversa muda de
    tema no meio). Cai no nome do arquivo se o modelo devolver lixo."""
    try:
        bruto, _n, ttft, dec = await _gerar_medido(
            llama, prompts.prompt_assunto(trecho), prompts.SYS_ASSUNTO, 24, temperature=0.0)
        rel.add_metricas(0.0, 0.0, 0, 0.0, 0.0, ass_s=ttft + dec)
    except Exception as exc:
        telemetry.warn("IMPORT", f"Falha ao detectar assunto: {exc}")
        return fallback
    assunto = " ".join(bruto.strip().strip('"\'.*').split())
    # Guarda contra o modelo que responde uma frase inteira em vez do assunto.
    if not assunto or len(assunto) > 70 or len(assunto.split()) > 10:
        return fallback
    return assunto


async def processar_janela(llama, dedup, rel: Relogio, tema: str, trecho: str,
                           origem: str, seq: int) -> int:
    """Uma janela -> N arquivos de átomo. Devolve quantos salvou."""
    assunto = await descobrir_assunto(llama, rel, trecho, tema)
    bruto, n_tok, ttft, dec = await _gerar_medido(
        llama, prompts.prompt_sintese_import(assunto, trecho),
        prompts.SYS_SINTESE_IMPORT, settings.max_tokens_resumo)
    
    bruto = bruto.strip()
    if not bruto or bruto.upper().strip(".!\n ") == "NADA":
        rel.add_metricas(ttft, dec, n_tok, 0.0, 0.0)
        return 0

    blocos = dividir_atomos(bruto)
    if not blocos:
        # Sem '##' NEM assinatura de átomo: o modelo devolveu prosa. Descartar é o
        # certo aqui (diferente do ETL ao vivo, onde o dump seria perdido): a fonte
        # continua no JSON e a janela pode ser reprocessada quando quisermos.
        telemetry.warn("IMPORT", f"Formato irrecuperável em '{assunto}' #{seq} — janela pulada.")
        rel.add_metricas(ttft, dec, n_tok, 0.0, 0.0)
        return 0

    agora = datetime.now()
    # ESTÁGIO 3 (Python): a garantia. O estágio 2 acerta a maioria; este fecha o resto.
    atomos = [
        normalizar_atomo(garantir_assunto(b, assunto), origem, agora, tags=TAGS_IMPORT)
        for b in blocos
    ]
    atomos = [a for a in atomos if a.strip()]

    # ESTÁGIO 3.5 (Python): FIDELIDADE. Descarta o átomo que cita número que não está
    # na fonte. Um átomo a menos é barato; um número inventado no vault, não.
    fieis = []
    for a in atomos:
        ok, motivo = e_fiel(strip_frontmatter(a), trecho)
        if ok:
            fieis.append(a)
        else:
            rel.add_infieis(1)
            telemetry.warn("FIDELIDADE", f"#{seq} descartado ({motivo}): {_slug_titulo(a)[:44]}")
    atomos = fieis

    # ESTÁGIO 4 (Python): uma ideia, uma nota. Dedup sobre o texto INDEXADO — é o que
    # vira vetor no Chroma, então é ali que a redundância machuca.
    # CPU OFFLOADING: Empurra o cálculo para thread secundária
    t_d = time.perf_counter()
    if dedup:
        manter = await asyncio.to_thread(dedup.filtrar, [strip_frontmatter(a).strip() for a in atomos])
    else:
        manter = [True] * len(atomos)
    dd_s = time.perf_counter() - t_d

    t_io = time.perf_counter()
    salvos = 0
    for i, (atomo, ok) in enumerate(zip(atomos, manter)):
        if not ok:
            continue
        caminho = _caminho_livre(_nome_atomo(origem, atomo, seq, i))
        try:
            with open(caminho, "w", encoding="utf-8") as f:
                f.write(atomo)
            salvos += 1
        except OSError as exc:
            telemetry.error("IMPORT", f"Falha ao salvar {os.path.basename(caminho)}", exc)
            
    io_s = time.perf_counter() - t_io
    rel.add_metricas(ttft, dec, n_tok, dd_s, io_s)
    return salvos


async def importar(filtro: str | None, limite_janelas: int | None) -> None:
    arquivos = sorted(f for f in os.listdir(DIR_JSON) if f.endswith(".json"))
    if filtro:
        arquivos = [f for f in arquivos if filtro.lower() in f.lower()]
    if not arquivos:
        print(f"Nenhum JSON casa com '{filtro}' em {DIR_JSON}")
        return

    checar_vram()
    os.makedirs(DIR_SAIDA, exist_ok=True)
    estado = carregar_estado()
    feitos = set(estado["feitos"])

    # Plano ANTES de carregar o modelo: sem o total não há % nem ETA, e é o operador
    # olhando o ETA que decide se vale atacar algum gargalo.
    plano = [(arq, janelar(ler_turnos(os.path.join(DIR_JSON, arq)))) for arq in arquivos]
    plano = [(a, js[:limite_janelas] if limite_janelas else js) for a, js in plano if js]
    total_janelas = sum(len(js) for _, js in plano)
    # Contra ESTE plano, não contra o estado global: com --so, `feitos` traz janelas de
    # outras conversas e o progresso saía como "[380/5] 7600%".
    ja_feitas_no_plano = sum(1 for a, js in plano for i in range(len(js)) if f"{a}#{i}" in feitos)
    pendentes = total_janelas - ja_feitas_no_plano
    telemetry.track("IMPORT", f"{len(plano)} conversas | {total_janelas} janelas | "
                              f"{pendentes} pendentes | {ja_feitas_no_plano} já feitas")

    llama = LlamaManager()
    await llama.load()
    if not llama.ready:
        print("ERRO: modelo não carregou (VRAM? o servidor está rodando?).")
        return

    # Dedup com o MESMO embedding do VectorStore: se dois átomos são clones aqui,
    # são indistinguíveis no Chroma também.
    provider = EmbeddingProvider()
    await asyncio.to_thread(provider.load)
    dedup = Deduplicador(provider.instance, LIMIAR_CLONE) if provider.instance else None
    if dedup is None:
        print("AVISO: sem embeddings — import seguirá SEM dedup.")
    else:
        # Semeia com o que já está em disco: sem isto, retomar o lote reintroduziria
        # duplicatas contra tudo que já foi importado.
        ja = [strip_frontmatter(open(p, encoding="utf-8").read()).strip()
              for p in sorted(glob.glob(os.path.join(DIR_SAIDA, "*.md")))]
        if ja:
            print(f"semeando dedup com {len(ja)} átomos já em disco...")
            await asyncio.to_thread(dedup.semear, ja)

    vigia = Vigia(pendentes, TOL_RSS_MB, TOL_VRAM_MB)
    vigia.ancorar()      # DEPOIS do load: o baseline não pode conter a subida do modelo

    rel = Relogio()
    
    # Semáforo de concorrência restrita: permite CPU overlap sem explodir o _inference_lock
    semaforo = asyncio.Semaphore(2)
    
    t0 = time.time()
    feitas_agora = 0
    parou: Optional[str] = None
    
    for arq, janelas in plano:
        tema = tema_do_arquivo(os.path.join(DIR_JSON, arq))
        
        janelas_pendentes = []
        for seq, trecho in enumerate(janelas):
            if f"{arq}#{seq}" not in feitos:
                janelas_pendentes.append((seq, trecho))
                
        if not janelas_pendentes:
            continue

        async def worker_janela(seq_alvo: int, trecho_alvo: str):
            async with semaforo:
                m = time.perf_counter()
                antes_d = dedup.fundidos if dedup else 0
                n_salvos = await processar_janela(llama, dedup, rel, tema, trecho_alvo, arq, seq_alvo)
                fund_agora = (dedup.fundidos - antes_d) if dedup else 0
                return seq_alvo, n_salvos, fund_agora, time.perf_counter() - m

        tarefas = [asyncio.create_task(worker_janela(s, t)) for s, t in janelas_pendentes]

        for tarefa in asyncio.as_completed(tarefas):
            seq_finalizada, n, clones, dt = await tarefa
            chave = f"{arq}#{seq_finalizada}"
            
            rel.add_atomos(n)
            feitas_agora += 1
            estado["atomos"] += n
            feitos.add(chave)
            estado["feitos"] = sorted(feitos)
            salvar_estado(estado)          # a cada janela: matar o processo não perde nada

            telemetry.track(
                "IMPORT",
                f"[{ja_feitas_no_plano + feitas_agora}/{total_janelas}] {100*(ja_feitas_no_plano + feitas_agora)/total_janelas:4.1f}% "
                f"{arq[:28]:28} #{seq_finalizada:<3} {dt:4.1f}s -> {n:2d} átomos"
                f"{f' ({clones}c)' if clones else '':5} "
                f"| {rel.tok_s:5.1f} tok/s decode  ETA {_eta(feitas_agora, pendentes, time.time()-t0)}",
            )
            
            # A cada 25 janelas, ONDE o tempo está indo. É este bloco que responde
            # "vale a pena otimizar o quê?" — sem ele, otimiza-se no escuro.
            if rel.janelas % 25 == 0:
                telemetry.track("IMPORT", f"  ┗━ {rel.resumo()}")
                telemetry.track("VIGIA", f"  ┗━ {vigia.status()}")

            # Vigia a CADA janela: parar cedo é barato (o estado já está no disco e o
            # lote retoma exatamente daqui). Descobrir em swap 3h depois, não é.
            parou = vigia.checar()
            if parou:
                break
        if parou:
            for t in tarefas:
                t.cancel()
            break

    if parou:
        telemetry.error("VIGIA", "=" * 70)
        telemetry.error("VIGIA", f"LOTE ABORTADO — possível vazamento: {parou}")
        telemetry.error("VIGIA", vigia.status())
        telemetry.error("VIGIA", "Nada se perdeu: o estado está salvo. Rode de novo para")
        telemetry.error("VIGIA", "retomar daqui. Se for falso positivo, suba TOL_RSS_MB.")
        telemetry.error("VIGIA", "=" * 70)

    print(f"\n{estado['atomos']} átomos em {DIR_SAIDA}")
    print("Eles NÃO estão no índice ainda: rode o servidor (o sync() indexa) quando quiser.")
    telemetry.track("IMPORT", rel.resumo())
    telemetry.track("VIGIA", vigia.status())
    llama.shutdown()


def listar() -> None:
    arquivos = sorted(f for f in os.listdir(DIR_JSON) if f.endswith(".json"))
    total_j = 0
    for arq in arquivos:
        turnos = ler_turnos(os.path.join(DIR_JSON, arq))
        j = len(janelar(turnos))
        total_j += j
        print(f"  {j:4d} janelas  {len(turnos):4d} turnos  {arq}")
    print(f"\n{len(arquivos)} conversas, {total_j} janelas (~{total_j*9/60:.0f} min de GPU)")


def main() -> None:
    p = argparse.ArgumentParser(description="Importa o histórico do Gemini como átomos")
    p.add_argument("--listar", action="store_true", help="mostra o plano sem rodar nada")
    p.add_argument("--so", default=None, help="só as conversas cujo nome contém isto")
    p.add_argument("--max-janelas", type=int, default=None, help="teto de janelas por conversa (prova)")
    a = p.parse_args()
    if a.listar:
        listar()
        return
    asyncio.run(importar(a.so, a.max_janelas))
    # os._exit e proposital: o processo estava FICANDO VIVO depois de terminar (a
    # thread do executor + o contexto CUDA do llama.cpp), segurando 8,4 GB de VRAM. O
    # lote seguinte então derramava os pesos para a RAM e rodava a 40 tok/s em vez de
    # 116, em silêncio. Aqui tudo já está no disco (o estado é salvo a cada janela),
    # então sair na marra é seguro — e devolver a VRAM importa mais que um teardown
    # elegante num script de lote.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()