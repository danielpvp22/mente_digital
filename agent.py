"""
Agente Omni — orquestra o pipeline de resposta e o ETL idle.

Mantém intactos os pilares da arquitetura:
- GPU serializada (via LlamaManager).
- Streaming + chunking de TTS (via SentenceChunker) para baixar o TTFA.
- Speculative Pre-Fetch em background.
- ETL Post-Chat só no idle (end_session), com PRIORIDADE REAL para a inferência
  interativa. Antes esta linha mentia: o ETL esperava `interactive_idle` apenas
  ENTRE documentos, então a pergunta que chegasse no meio de uma síntese esperava o
  decode inteiro no lock da GPU (medido: 4,6s de TTFT, e o teto é max_tokens_sintese).
  Agora o pipeline marca a GPU como ocupada e PREEMPTA o decode de background em
  curso (llm.preempt), que morre em ~1 token e devolve o item pra fila — medido:
  TTFT de 4605ms -> 26ms.
- Anti-alucinação via system prompt.

O pipeline não conhece o WebSocket: recebe um callback `send(dict) -> bool`.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from typing import Awaitable, Callable, Deque, List, Optional, Tuple

import mestre
import prompts
import textutils
import tools
import verbosidade
from audio import SentenceChunker
from config import settings
from llm import InferenciaPreemptada, LlamaManager
from rag import NENHUM, LocalResult, strip_frontmatter
from state import AppContext, SessionMemory
from telemetry import db, telemetry

Sender = Callable[[dict], Awaitable[bool]]
STOP_WORDS = {"não", "nao", "sim", "nada", "tudo", "pode falar", "continue", "isso", "nenhum"}
# "Sem banco local" — usado no estágio RAM para reaproveitar _montar_contexto sem
# tocar no vetor (o builder ignora o local quando o texto é NENHUM).
NENHUM_LOCAL = LocalResult(NENHUM, None, False)
# Sentinela anti-alucinação (normalizado) — REGRA 1 do system prompt de resposta.
SENTINELA_INSUF = "nao tenho informacoes suficientes"


class LatencyTracker:
    """Mede o pilar de latência: TTFT (1º token) e TTFA (1º áudio) por resposta.

    Marca o primeiro instante em que cada tipo de mensagem sai pelo `send`. O clock
    é injetável para permitir teste determinístico (sem depender do relógio real).
    """

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self.t0 = clock()
        self.ttft: float | None = None
        self.ttfa: float | None = None

    def note(self, msg: dict) -> None:
        tipo = msg.get("tipo")
        if tipo == "token" and self.ttft is None:
            self.ttft = self._clock() - self.t0
        elif tipo == "audio" and self.ttfa is None:
            self.ttfa = self._clock() - self.t0

    def total(self) -> float:
        return self._clock() - self.t0

    @staticmethod
    def _ms(seg: float | None) -> int | None:
        return round(seg * 1000) if seg is not None else None


async def append_chat_dump(ator: str, texto: str) -> None:
    """Grava o dump bruto da conversa (Obsidian). IO em thread."""
    def _write() -> None:
        with open(settings.arquivo_chat_dump, "a", encoding="utf-8") as f:
            if ator == "User":
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n## [{ts}]\n**Usuário:** {texto}\n")
            else:
                f.write(f"**Mente Digital:** {texto}\n")

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        telemetry.error("DUMP", "Erro ao gravar chat dump", exc)


# ==========================================================================
# Atomização de arquivos (um .md por ideia — Zettelkasten puro)
# ==========================================================================
# Linha só de tags ('#a #b') e a tag isolada — usadas para canonizar o rodapé do átomo.
_TAG_LINHA_RE = re.compile(r"^\s*#[\w/\-]+(?:\s+#[\w/\-]+)*\s*$")
_TAG_RE = re.compile(r"#[\w/\-]+")
# Tags penduradas no FIM de uma linha de conteúdo. Visto no import real: o modelo
# escreve '**Malha Neural:** [[X]] #zettelkasten_atomico' — a tag não está sozinha na
# linha, então o _TAG_LINHA_RE não a via e normalizar_atomo acrescentava a canônica
# de novo (12 de 19 átomos saíam com a tag duplicada). Exige espaço antes do '#',
# então nunca casa um cabeçalho ('## Título').
_TAGS_FIM_RE = re.compile(r"(?:\s+#[\w/\-]+)+\s*$")
_MALHA = "**Malha Neural:**"


def _parece_atomo(bloco: str) -> bool:
    """O bloco tem a ASSINATURA de um átomo, mesmo sem o '## '?

    Conservador de propósito: só a presença das tags canônicas ou da Malha Neural
    conta. Prosa solta não vira átomo por acidente (ver dividir_atomos).
    """
    return prompts.TAG_ATOMO in bloco or prompts.TAG_NOVO in bloco or _MALHA in bloco


def dividir_atomos(texto: str) -> List[str]:
    """Quebra a saída de síntese (vários '## título') em blocos atômicos individuais.

    Puro/testável. Cada bloco começa num cabeçalho '## ' e vai até o próximo. Ignora
    qualquer preâmbulo antes do 1º '##' (o prompt proíbe introdução, mas o LLM às vezes
    escapa uma). Sem nenhum '##', devolve [] — o chamador decide o fallback.
    """
    blocos: List[str] = []
    atual: List[str] = []
    for ln in texto.splitlines():
        if ln.lstrip().startswith("## "):
            if atual:
                bloco = "\n".join(atual).strip()
                if bloco:
                    blocos.append(bloco)
            atual = [ln]
        elif atual:
            atual.append(ln)
    if atual:
        bloco = "\n".join(atual).strip()
        if bloco:
            blocos.append(bloco)
    if blocos:
        return blocos

    # FALLBACK — medido no A/B (eval/ab_modelos.py): o Qwen2.5-7B-Instruct emite
    # título + corpo + Malha Neural + as duas tags, e ESQUECE só o '## ' (2 de 3
    # sínteses). Sem isto, dividir_atomos devolve [] e o fallback de _salvar_atomos
    # cola a síntese INTEIRA num arquivo — matando o "um arquivo por átomo" e, com
    # ele, a precisão da promoção. Separa por linha em branco e só aceita blocos com
    # assinatura de átomo, então prosa solta continua devolvendo [].
    candidatos = [b.strip() for b in re.split(r"\n\s*\n", texto) if b.strip()]
    return [b for b in candidatos if _parece_atomo(b)]


def _slug_titulo(bloco: str, max_len: int = 40) -> str:
    """Slug curto a partir do título ('## ...') do átomo, para o nome do arquivo.

    Procura a linha do título em vez de assumir a 1ª: o átomo normalizado começa com
    frontmatter, então `splitlines()[0]` seria '---'.

    `max_len` é parâmetro porque o import de histórico precisa de mais espaço: lá o
    `garantir_assunto` PREFIXA o assunto no título, e em 40 chars o prefixo comia a
    parte que distingue os átomos — "Custo de 40 balas de MAI AP" e "Custo de 30 balas
    de BP" viravam slugs que só diferiam no 40/30, perdendo o "MAI AP" vs "BP". Corta
    em fronteira de palavra: nome cortado no meio de uma sílaba não ajuda ninguém.
    """
    titulo = ""
    for ln in bloco.splitlines():
        if ln.lstrip().startswith("## "):
            titulo = ln.lstrip("#").strip()
            break
    if not titulo:
        primeira = bloco.splitlines()[0] if bloco.strip() else ""
        titulo = primeira.lstrip("#").strip()
    slug = re.sub(r"[^a-z0-9]+", "_", textutils.normaliza(titulo)).strip("_")
    if len(slug) > max_len:
        corte = slug.rfind("_", 0, max_len + 1)      # não corta no meio da palavra
        slug = slug[: corte if corte > max_len // 2 else max_len]
    return slug.strip("_") or "atomo"


# Conteúdo da linha de Malha Neural, e os separadores que o modelo realmente usa.
# ' e ' fica de FORA de propósito: aparece dentro de nomes ("Pesquisa e Ranking") e
# quebraria conceitos legítimos em dois. Vírgula/ponto-e-vírgula/' ou ' bastam.
_MALHA_RE = re.compile(r"^\s*\*\*Malha Neural:\*\*\s*(.*)$", re.IGNORECASE)
_MALHA_SEP = re.compile(r"\s*(?:,|;|\bou\b)\s*", re.IGNORECASE)
_PARENTESE = re.compile(r"\([^)]*\)")


def normalizar_malha(conteudo: str) -> str:
    """Conteúdo cru da Malha Neural -> '[[A]] [[B]] [[C]]'. Puro/testável.

    Por que existe: o Obsidian só resolve wikilink com colchete DUPLO. Medido em 84
    átomos reais do import, 44 (58%) saíram com colchete SIMPLES — '[FastAPI,
    Faster-Whisper, Silero VAD, Ollama, Piper TTS]'. Isso não é link: é sintaxe de link
    markdown sem a URL, então o Obsidian renderiza como texto literal, e cinco conceitos
    distintos viram uma string só. Sem link não há grafo, e sem grafo a "Malha Neural"
    não existe — é o nome da coisa.

    Mesma lição de `normalizar_atomo`, aplicada onde eu tinha esquecido: o LLM entrega
    os CONCEITOS, o Python entrega a SINTAXE.

    Se já houver [[...]], respeita e devolve só eles (o modelo acertou). Senão, descasca
    os colchetes externos, joga fora parênteses ("(Opcional, mas recomendado)" viraria
    dois links-lixo se a vírgula dentro dele fosse separador) e fatia o resto.
    """
    conteudo = conteudo.strip()
    if not conteudo:
        return ""
    # Extrai QUALQUER grupo entre colchetes — duplo, simples ou torto. A 1ª versão
    # tratava '[[x]]' e '[x]' como dois casos e deixava escapar o aninhamento que o
    # modelo realmente produz: '[[Córtex Auditivo] [Solicitações HTTP] [Pesos]]' (abre
    # duplo, fecha simples). O regex de '[[...]]' não casava, o descasque de colchetes
    # externos deixava 'Córtex Auditivo] [Solicitações HTTP] [Pesos' como UM conceito, e
    # o resultado saía re-embrulhado e igualmente quebrado — 45 de 378 átomos reais.
    # Um regex que aceita qualquer número de colchetes resolve os três casos de uma vez.
    grupos = re.findall(r"\[+([^\[\]]+)\]+", conteudo) or [conteudo]

    conceitos: List[str] = []
    for g in grupos:
        # Parêntese fora ANTES de fatiar: a vírgula dentro de "(Opcional, mas
        # recomendado)" viraria dois links-lixo.
        conceitos.extend(_MALHA_SEP.split(_PARENTESE.sub("", g)))

    vistos: set = set()
    finais: List[str] = []
    for c in conceitos:
        c = c.strip(" .*[]").strip()
        # Conceito é um NOME, não uma frase: o que for longo demais é ruído do modelo.
        if not c or len(c) > 60 or c.lower() in vistos:
            continue
        vistos.add(c.lower())
        finais.append(f"[[{c}]]")
    return " ".join(finais)


def _e_titulo(linha: str) -> bool:
    """Linha curta que não termina em pontuação de frase = título sem '#'."""
    ln = linha.strip()
    return bool(ln) and len(ln) <= 80 and not ln.endswith((".", "!", "?", ":", ";", ","))


def normalizar_atomo(
    bloco: str, origem: str, agora: datetime, tags: Tuple[str, ...] = (prompts.TAG_ATOMO, prompts.TAG_NOVO)
) -> str:
    """Impõe a ESTRUTURA do átomo em vez de PEDI-LA ao LLM. Puro/testável.

    Por que existe: o A/B (eval/ab_modelos.py) provou que NENHUM modelo entrega o
    formato de forma confiável — e que cada um falha de um jeito oposto. O
    Qwen2.5-Coder emite '##' e esquece as tags (1 de 6 blocos com tag; no vault,
    8 de 177) → `_consolidar_fontes` faz `if tag not in conteudo: return False`, ou
    seja, a promoção vira no-op em 95% da base. O Qwen2.5-Instruct acerta as tags
    (3/3) e esquece o '##' (2 de 3 sínteses) → colapso num arquivo só. Trocar de
    modelo não conserta: move o defeito. Então o LLM entrega a IDEIA e o Python
    entrega a ESTRUTURA.

    - Título: qualquer nível de '#' vira '## '; 1ª linha solta e curta é promovida a
      título; formato totalmente quebrado deriva das primeiras palavras.
    - Tags: as canônicas são GARANTIDAS. As que o modelo inventou (#tempo, #financas)
      são preservadas — são úteis no Obsidian e não custam nada.
    - Proveniência: vai em FRONTMATTER, não no corpo. `rag.strip_frontmatter` já a
      remove antes do chunking, então ela NÃO polui o embedding nem o aterramento
      léxico (o vault já sofre com boilerplate: 'neural' aparece em 97,6% das notas).
      O `colhido_em` é o que habilita a poda por idade depois, a custo zero de busca.
    """
    # Idempotência: um átomo já normalizado volta por aqui (re-síntese, passada de
    # manutenção). Sem descascar o frontmatter, o '---' viraria o título ('## ---').
    linhas = [ln.rstrip() for ln in strip_frontmatter(bloco.strip()).strip().splitlines()]
    if not linhas:
        return ""

    titulo = ""
    titulo_explicito = False          # veio de um '## ' real (não de linha promovida)
    corpo: List[str] = []
    achadas: List[str] = []          # tags que o próprio modelo emitiu
    primeira_analisada = False
    for ln in linhas:
        if _TAG_LINHA_RE.match(ln):
            achadas.extend(_TAG_RE.findall(ln))
            continue
        # Tag pendurada no fim de uma linha de conteúdo: colhe e limpa, senão a
        # canônica seria acrescentada de novo mais abaixo (tag duplicada no arquivo).
        fim = _TAGS_FIM_RE.search(ln)
        if fim:
            achadas.extend(_TAG_RE.findall(fim.group()))
            ln = ln[: fim.start()].rstrip()
            if not ln:
                continue
        # Malha Neural: a SINTAXE é imposta aqui, não pedida ao modelo (58% dos átomos
        # reais saíam com colchete simples, que o Obsidian não resolve). Ver normalizar_malha.
        m = _MALHA_RE.match(ln)
        if m:
            links = normalizar_malha(m.group(1))
            if links:
                corpo.append(f"{_MALHA} {links}")
            continue
        if not primeira_analisada:
            primeira_analisada = True
            if ln.lstrip().startswith("#"):
                titulo = ln.lstrip("#").strip()
                titulo_explicito = True
                continue
            if _e_titulo(ln):
                titulo = ln.strip()
                continue
        corpo.append(ln)

    corpo_txt = "\n".join(corpo).strip()

    # PORTÃO NADA/vazio: um átomo sem FATO não é átomo. O sentinela "nada a extrair"
    # vazava por bloco — o check de NADA do importador e do ETL só pega a saída INTEIRA,
    # então uma síntese com átomos bons + um bloco "NADA" salvava '## Assunto: NADA'
    # (corpo 'NADA'), medido: 11 na base. E '## Título\n**Malha**' sem corpo virava
    # átomo oco (9 na base). Fatiar aqui protege OS DOIS caminhos (importar_gemini E
    # criação de MD pós-conversa), porque ambos passam por normalizar_atomo.
    #
    # CUIDADO com o FALLBACK do ETL: quando o LLM manda prosa SEM '##', a 1ª linha é
    # PROMOVIDA a título e o átomo fica sem corpo DE PROPÓSITO (não perder conhecimento
    # — ver test_atomo_sem_cabecalho...). Esse caso tem título promovido, não explícito.
    # Só rejeitamos corpo vazio quando o título era um '## ' REAL; a prosa promovida passa.
    corpo_sem_malha = "\n".join(ln for ln in corpo if not ln.startswith(_MALHA)).strip()
    nada = textutils.normaliza(corpo_sem_malha).strip(".!?") == "nada"
    vazio_com_titulo_real = not corpo_sem_malha and titulo_explicito
    # IDIOMA ERRADO: a síntese às vezes sai em chinês (medido: 30 na base). Um átomo
    # cujo corpo é substancialmente CJK nunca serve a uma pergunta em PT e só dilui o
    # contexto — rejeita na fonte (importador E ETL vivo E pesquisa proativa passam aqui).
    lingua_errada = textutils.fracao_cjk(corpo_sem_malha) > 0.15
    if nada or vazio_com_titulo_real or lingua_errada:
        return ""

    if not titulo:
        # Formato irrecuperável: melhor um título derivado que um átomo sem título.
        titulo = " ".join(corpo_txt.split()[:6]) or "Atomo"

    # `tags` (parâmetro) são as CANÔNICAS garantidas; `achadas` são as que o modelo
    # inventou e que preservamos. O import de histórico passa (#zettelkasten_atomico,
    # #memoria_legada): não é curiosidade auto-colhida, é o passado do usuário, então
    # não nasce com #conhecimento_novo nem entra no ciclo de promoção.
    for t in tags:
        if t not in achadas:
            achadas.append(t)
    vistas: set = set()
    finais = [t for t in achadas if not (t in vistas or vistas.add(t))]

    out = [
        "---",
        f"origem: {origem}",
        f"colhido_em: {agora.strftime('%Y-%m-%d')}",
        "---",
        f"## {titulo}",
    ]
    if corpo_txt:
        out.append(corpo_txt)
    out.append(" ".join(finais))
    return "\n".join(out) + "\n"


# Marcadores de que a pergunta REFERENCIA o contexto anterior (pronomes/demonstrativos
# que apontam pra trás). Só quando um deles aparece vale passar o histórico ao extrator
# — senão o LLM MISTURA o assunto velho no novo. Medido em produção: 'como funciona o
# tensor RT?' (sem pronome, troca limpa de assunto) virou query 'tensor rt esp32',
# puxando 'esp32' do turno anterior. Referência ausente -> pergunta é auto-contida.
_REFERENCIAS_CONTEXTO = {
    "ele", "ela", "eles", "elas", "dele", "dela", "deles", "delas", "nele", "nela",
    "isso", "isto", "esse", "essa", "esses", "essas", "este", "esta", "estes", "estas",
    "nisso", "disso", "nesse", "neste", "nessa", "nesta", "aquele", "aquela", "aquilo",
    "aqueles", "aquelas", "mesmo", "mesma", "tal", "ai", "assim", "acima", "citado",
}


def referencia_contexto(pergunta: str) -> bool:
    """A pergunta aponta pra um assunto anterior (tem pronome/demonstrativo)? Puro."""
    return bool(set(textutils.tokens(pergunta)) & _REFERENCIAS_CONTEXTO)


def lacuna_pesquisavel(termos: str) -> bool:
    """A lacuna vale uma pesquisa proativa (autônoma, escreve no vault)? Puro/testável.

    Dois defeitos medidos em produção, um filtro:
    - TRIVIAL: 'ok'/'sim' (falso-positivo do VAD/Whisper, 0 keywords) escalava e a
      proativa pesquisava a etimologia de "ok" — 8 átomos-lixo.
    - SEM NÚCLEO: 'dolar 542' (moeda + número). Tirando o número e o gatilho efêmero,
      não sobra ASSUNTO a pesquisar — e a resposta (cotação) expiraria de qualquer jeito.

    Aplicar e_efemero cru aos termos seria agressivo demais: 'protocolo stratum v2
    mineracao bitcoin' é efêmero pela palavra 'bitcoin', mas é pergunta técnica legítima.
    Por isso o teste é ter NÚCLEO — ao menos 1 keyword que não seja número puro nem
    gatilho efêmero. 'dolar 542' -> núcleo vazio; 'stratum...bitcoin' -> {protocolo,
    stratum, mineracao}. Distingue o lixo do assunto real que só menciona cripto.
    """
    kws = textutils.palavras_chave(termos)
    if len(kws) < settings.lacuna_min_keywords:
        return False
    nucleo = {k for k in kws if not k.isdigit() and not tools.e_efemero(k)}
    return bool(nucleo)


# Gatilhos de pergunta META-LINGUÍSTICA: quem pergunta "de onde saiu a EXPRESSÃO X"
# está perguntando SOBRE a frase X, e X (não a moldura) é o alvo da busca.
_GATILHOS_CITACAO = {
    "expressao", "expressoes", "frase", "frases", "ditado", "ditados", "giria",
    "girias", "proverbio", "proverbios", "dito", "ditado popular", "jargao",
}


# Gatilhos da Síntese sob Demanda (#23) — quem diz "o que eu sei sobre X" quer uma
# varredura do tema X no vault, não a resposta pontual do pipeline normal.
_GATILHOS_SINTESE = (
    "o que eu sei sobre", "o que sei sobre", "o que eu tenho sobre", "o que tenho sobre",
    "tudo que eu sei sobre", "tudo que sei sobre", "tudo sobre", "resuma o que",
    "faca uma sintese sobre", "faz uma sintese sobre", "sintese sobre", "sintetiza sobre",
    "me resuma sobre", "resuma sobre", "resuma tudo sobre",
)


def extrair_tema_sintese(pergunta: str) -> "str | None":
    """Extrai o TEMA de um pedido de síntese ('o que eu sei sobre X' -> 'X'). Puro.

    Casa o gatilho numa versão sem acento e length-preservada (para fatiar o original
    pelos mesmos índices, preservando acentos do tema). None se não for um pedido."""
    baixa = pergunta.lower()
    ascii_b = textutils.sem_acento(baixa)
    hay = ascii_b if len(ascii_b) == len(baixa) else baixa
    for g in _GATILHOS_SINTESE:
        i = hay.find(g)
        if i != -1:
            tema = pergunta[i + len(g):].strip(" ?.!:,\"'")
            tema = re.sub(
                r"^(?:o|a|os|as|meu|minha|sobre|tema|assunto)\s+", "", tema, flags=re.IGNORECASE
            ).strip()
            return tema or None
    return None


def frase_citada(pergunta: str) -> str:
    """Extrai a FRASE que a pergunta cita, quando ela é sobre uma expressão. Puro.

    Bug medido em produção: "da onde saiu a expressão pega um prato faz a linha dá um
    tiro na farinha?" → o extrator reduziu a 5 palavras e cuspiu 'saiu expressão pega
    prato', JOGANDO FORA a expressão inteira — que é exatamente o que se busca. A query
    de 5 palavras serve ao aterramento léxico LOCAL; para a web, a frase citada é o
    alvo de maior sinal (o Google acha 'pega um prato...' num instante).

    Pega tudo depois do gatilho ('expressão', 'ditado', ...). Exige >=3 palavras de
    resto para não disparar em 'qual sua expressão favorita?' (moldura sem citação)."""
    palavras = pergunta.strip().rstrip("?.!").split()
    norm = [textutils.normaliza(w) for w in palavras]
    for i, w in enumerate(norm):
        if w in _GATILHOS_CITACAO:
            resto = palavras[i + 1:]
            # pula um conector logo após o gatilho ('o ditado QUE DIZ x', 'a frase: x')
            while resto and textutils.normaliza(resto[0]) in {"que", "diz", "e", ":"}:
                resto = resto[1:]
            if len(resto) >= 3:
                return " ".join(resto).strip(" :\"'")
    return ""


# ==========================================================================
# Extrator de query (resolve pronomes cruzados)
# ==========================================================================
class QueryOptimizer:
    def __init__(self, llama: LlamaManager) -> None:
        self._llama = llama

    async def optimize(self, pergunta: str, historico: Deque[Tuple[str, str]]) -> str:
        limpa = pergunta.lower().strip().replace(".", "").replace(",", "")
        if limpa in STOP_WORDS or len(limpa) < 4:
            if historico:
                # continuação: reaproveita a última pergunta, já enxuta
                return textutils.limpar_query(historico[-1][0]) or historico[-1][0]
            return limpa

        # SÓ passa o histórico se a pergunta REFERENCIA o assunto anterior. Uma pergunta
        # auto-contida ('como funciona o tensor RT?') não pode ver o turno de 'esp32' —
        # o extrator misturava os dois numa query só ('tensor rt esp32'). Sem referência,
        # contexto="NENHUM": o assunto novo entra limpo.
        contexto = "NENHUM"
        if historico and referencia_contexto(pergunta):
            turnos = []
            for q, a in list(historico)[-2:]:
                resumo = a[:150] + "..." if len(a) > 150 else a
                turnos.append(f"U: {q}\nIA: {resumo}")
            contexto = "\n".join(turnos)

        bruto = await self._llama.collect(
            prompts.prompt_extrator(contexto, pergunta),
            max_tokens=settings.max_tokens_query,
            system_prompt=prompts.SYS_EXTRATOR,
        )
        bruto = re.sub(r"[\"',.!?:*\[\]\n\r]", "", bruto).strip()
        # Rede contra o modelo que "ecoa" a frase inteira: tira saudação/fillers e capa
        # a ~6 palavras. Assim "Olá gostaria de entender... TensorFlow RT" -> "TensorFlow RT".
        termos = textutils.limpar_query(bruto) or textutils.limpar_query(pergunta) or limpa
        telemetry.track("EXTRATOR", f"Query final: [{termos}]")
        return termos


# ==========================================================================
# Pipeline principal
# ==========================================================================
class Agent:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.optimizer = QueryOptimizer(ctx.llama)
        self.tools = tools.criar_registry()
        self._filler_i = 0  # rotaciona o texto do filler p/ não ficar repetitivo
        self._atalhos: Optional[dict] = None  # cache dos atalhos-mestre (#2), lazy do DB

    async def _falar(self, send: Sender, frases: List[str]) -> None:
        """Envia frases prontas para o TTS conforme o chunker fecha sentenças."""
        for frase in frases:
            audio = await self.ctx.tts.synth_base64(frase)
            if audio:
                await send({"tipo": "audio", "base64": audio})

    async def _falar_texto(self, send: Sender, texto: str) -> None:
        """Fatia um texto já pronto em frases e sintetiza cada uma."""
        chunker = SentenceChunker()
        for frase in chunker.push(texto):
            await self._falar(send, [frase])
        resto = chunker.flush()
        if resto:
            await self._falar(send, [resto])

    def _ram_relevante(self, termos: str, mem: SessionMemory) -> List[str]:
        """Só injeta memória da sessão cujo TEMA casa com a pergunta atual.

        Corrige o Cache Hit falso: antes as 2 últimas entradas entravam sempre,
        então uma busca velha sobre 'TensorFlow' contaminava toda pergunta seguinte.
        """
        chaves = textutils.palavras_chave(termos)
        if not chaves:
            return []
        out: List[str] = []
        for tema, dados in reversed(mem.conhecimento_sessao):
            if textutils.tem_sobreposicao(chaves, textutils.palavras_chave(tema)):
                out.append(dados)
            if len(out) >= 2:
                break
        return out

    @staticmethod
    def _montar_contexto(local: LocalResult, ram: List[str]) -> str:
        partes = []
        if local.texto != NENHUM:
            partes.append(f"[Banco Local]\n{local.texto}")
        if ram:
            # A RAM da sessão é SEMPRE prefetch da WEB (só o _prefetch chama lembrar).
            # Rotulá-la como GENÉRICA impede que a fusão apresente um kit qualquer da
            # web como se fosse a lista do PROJETO do usuário (medido: pergunta sobre
            # 'a lista de compra do meu projeto' respondida com um kit ESP32 aleatório).
            partes.append(
                "[Contexto amplo da WEB — informação GENÉRICA, pode não ser específica "
                "do caso do usuário]\n" + "\n\n".join(ram)
            )
        return "\n".join(partes)

    async def pipeline_resposta(
        self, texto_usuario: str, send: Sender, mem: SessionMemory
    ) -> None:
        # Instrumenta o TTFT/TTFA sem tocar no resto: cada msg passa pelo tracker.
        tracker = LatencyTracker()

        async def send_medido(msg: dict) -> bool:
            tracker.note(msg)
            return await send(msg)

        # A ORDEM É O MECANISMO, e ela tem que ser esta:
        #   1) `interativo()` faz o clear() -> o ETL que acordar agora volta a dormir;
        #   2) `preempt()` corta o decode de ETL que JÁ estava rodando.
        # Invertido, existe uma janela entre o preempt e o clear em que o ETL vê
        # "GPU livre", pega o lock e começa OUTRA síntese — e a pergunta espera de novo.
        async with self.ctx.interativo():
            self.ctx.llama.preempt()
            await self._pipeline(texto_usuario, send_medido, tracker, mem)

    async def _pipeline(
        self, texto_usuario: str, send_medido: Sender, tracker: LatencyTracker, mem: SessionMemory
    ) -> None:
        rota = "web"
        try:
            # PALAVRA-MESTRE: se a mensagem começa por ela, é um COMANDO de agente e vai
            # por um fluxo ISOLADO (determinístico primeiro, LLM só se necessário). Não
            # cai no pipeline de conhecimento. Sem a palavra-mestre, tudo segue como hoje.
            if settings.palavra_mestre_habilitada:
                comando = mestre.separar(texto_usuario, settings.palavra_mestre)
                if comando is not None:
                    await self._fluxo_mestre(comando, send_medido, tracker, mem)
                    return

            # EFEMERIDADE — decidida UMA vez, no topo, porque governa os DOIS caminhos
            # de ingestão: a fila do ETL (web) e o dump da conversa (que o idle atomiza).
            # Medido no vault: 45 dos 48 átomos-lixo vieram da fila, 3 do dump.
            efemero = tools.e_efemero(texto_usuario)

            # SÍNTESE SOB DEMANDA (#23): "o que eu sei sobre X" tem FLUXO PRÓPRIO
            # (map-reduce sobre o vault), separado do pipeline de resposta pontual —
            # é o que evita estourar o contexto num tema grande.
            tema_sintese = extrair_tema_sintese(texto_usuario)
            if tema_sintese:
                telemetry.track("AGENT", f"Síntese sob demanda: '{tema_sintese}'.")
                await self._sintese_sob_demanda(tema_sintese, send_medido, mem, tracker)
                return

            # ROTEAMENTO DE AÇÃO (aditivo): só mensagens que parecem AÇÃO chamam o
            # roteador LLM. Pergunta de conhecimento nem paga essa chamada — cai
            # direto no pipeline afinado abaixo (TTFA preservado).
            if tools.talvez_acao(texto_usuario):
                decisao = await self._rotear(texto_usuario)
                if decisao and decisao.tool != "responder" and self.tools.get(decisao.tool):
                    telemetry.track("AGENT", f"Ação -> ferramenta '{decisao.tool}'.")
                    tool = self.tools.get(decisao.tool)
                    texto_final = await self._pipeline_tools(texto_usuario, send_medido, decisao, auditar=not mem.confidencial, mem=mem)
                    if texto_final:
                        # Ações de AGENDA/LISTA (registra_conhecimento=False) não vão para o
                        # dump: "lembrete #3 criado" não é conhecimento a eternizar no vault.
                        # Modo confidencial (#5) também não persiste nada (só a RAM).
                        if not efemero and tool.registra_conhecimento and not mem.confidencial:
                            await append_chat_dump("IA", texto_final)
                        mem.registrar_turno(texto_usuario, texto_final)
                        if not mem.confidencial:
                            await asyncio.to_thread(db.save_chat, texto_usuario, texto_final, mem.conversa_id)
                        await self._registrar_latencia(tracker, f"tool:{decisao.tool}")
                    return

            termos = await self.optimizer.optimize(texto_usuario, mem.chat_history)
            # Pergunta enriquecida com o histórico p/ o GERADOR da resposta (não a
            # recuperação, que já resolve o pronome via QueryOptimizer). Sem isso, um
            # follow-up cru como "poderia explicar melhor?" chegava ao LLM SEM antecedente
            # → ele dizia sentinela mesmo com os átomos certos na mão. Dump/memória/busca
            # seguem com o texto_usuario ORIGINAL; só o prompt de resposta recebe o contexto.
            pergunta_resp = self._pergunta_com_contexto(texto_usuario, mem)

            # FUSÃO EM CASCATA: cada fonte com átomos relevantes contribui com UM
            # parágrafo (passada de inferência própria), na ordem memória > banco > web.
            # A web só entra se o local não produziu NADA real (preserva TTFA e o
            # anti-alucinação local-first). Parágrafos separados por linha em branco.
            paragrafos: List[str] = []
            fontes: List[str] = []

            # Verbosidade (#7): a pergunta define o tamanho da resposta (e a latência).
            nivel = verbosidade.classificar(texto_usuario)
            if nivel.nome != "normal":
                telemetry.track("VERBOSIDADE", f"nível={nivel.nome} max_tokens={nivel.max_tokens}")

            async def passada(contexto: str, fonte: str) -> None:
                # prefixo só quando JÁ há parágrafo antes (separa as passadas); é enviado
                # dentro do _responder_contexto, na 1ª emissão real (não vaza se der sentinela).
                p = await self._responder_contexto(
                    contexto, pergunta_resp, send_medido,
                    prompt_fn=prompts.prompt_resposta_atomos,
                    system=prompts.SYS_FUSAO,
                    prefixo="\n\n" if paragrafos else "",
                    max_tokens=nivel.max_tokens,
                    instrucao_extra=nivel.instrucao,
                )
                if p:
                    paragrafos.append(p)
                    fontes.append(fonte)

            # ATALHO TIME-SENSITIVE: cotação/preço agora, notícias/clima de hoje. O banco
            # é inútil e desatualizado nesses casos — pula RAM+Banco e vai DIRETO pra web
            # (fresco, e sem pagar a passada local morta). Fora isso, cascata normal.
            if tools.talvez_tempo_real(texto_usuario):
                telemetry.track("AGENT", f"Time-sensitive — direto pra web: '{termos}'.")
                web = await self._responder_web(
                    termos, pergunta_resp, send_medido, mem,
                    consulta_rank=texto_usuario, efemero=efemero, nivel=nivel,
                )
                if web:
                    paragrafos.append(web)
                    fontes.append("web")
            else:
                ram = self._ram_relevante(termos, mem)

                # ESTÁGIO 1 — RAM (memória fresca da sessão): a mais fresca, já por tema.
                if ram:
                    telemetry.track("AGENT", f"Fusão: passada RAM ({len(ram)} tópico(s)).")
                    await passada(self._montar_contexto(NENHUM_LOCAL, ram), "ram")

                # EARLY-STOP (#3): se uma fonte já respondeu com confiança (passada
                # não-sentinela), PARA a cascata — não roda o Banco (nem a busca vetorial,
                # nem sua passada de inferência). Economiza um decode na GPU serializada.
                # Botão MENTE_EARLY_STOP_CASCATA; desligado, volta à fusão RAM+Banco.
                if settings.early_stop_cascata and paragrafos:
                    telemetry.track("AGENT", "Early-stop: RAM respondeu — pula Banco/Web.")
                else:
                    # ESTÁGIO 2 — Banco vetorial: query atomizada (mesmo formato da base)
                    # colhe dezenas de átomos Zettelkasten e os funde num parágrafo.
                    texto_busca = await self._texto_busca(texto_usuario, termos)
                    local = await self.ctx.vectorstore.search(termos, texto_busca=texto_busca)
                    telemetry.track(
                        "LOCAL",
                        f"melhor_dist={local.melhor_dist} relevante={local.relevante} ram={len(ram)}",
                    )
                    if local.relevante:
                        telemetry.track("AGENT", "Fusão: passada Banco.")
                        antes = len(paragrafos)
                        await passada(self._montar_contexto(local, []), "banco")
                        # PROMOÇÃO: se o Banco de fato contribuiu (passada não-sentinela),
                        # os átomos usados "amadureceram" — tira o #conhecimento_novo deles.
                        # Em background: não pesa no TTFA da resposta atual.
                        if len(paragrafos) > antes and local.fontes:
                            self.ctx.track_task(self._consolidar_fontes(local.fontes))

                # ESTÁGIO 3 — Web (só SE NECESSÁRIO: nenhuma fonte local produziu algo real).
                if not paragrafos:
                    telemetry.track("AGENT", f"Local insuficiente para '{termos}'. Escalando para a web.")
                    # LACUNA: nem a RAM nem o banco tinham. Registra para a pesquisa
                    # proativa do idle trazer isto pronto na próxima vez. `efemero` (da
                    # pergunta original) barra 'clima amanhã'; `lacuna_pesquisavel` barra
                    # o trivial ('ok') e o sem-núcleo ('dolar 542') — ver a função.
                    # Modo confidencial (#5): a lacuna é um artefato DERIVADO da pergunta
                    # sigilosa — persisti-la vazaria o assunto; fica de fora.
                    if not efemero and not mem.confidencial and lacuna_pesquisavel(termos):
                        await asyncio.to_thread(
                            db.save_lacuna, textutils.normaliza(termos), termos
                        )
                    # `efemero` também aqui: é POR ESTE caminho que "clima em lisboa
                    # amanhã" chega (o gate de rota não o pega), escala pra web e hoje
                    # é ingerido. Sem isto o bloqueio vazaria os casos reais medidos.
                    web = await self._responder_web(
                        termos, pergunta_resp, send_medido, mem,
                        consulta_rank=texto_usuario, efemero=efemero, nivel=nivel,
                    )
                    if web:
                        paragrafos.append(web)
                        fontes.append("web")

            texto_final = "\n\n".join(paragrafos) if paragrafos else None
            rota = "+".join(fontes) if fontes else "web"

            if texto_final:
                # Dump só do que pode virar conhecimento: o idle atomiza este arquivo,
                # então um turno efêmero aqui vira "## Hora atual" no Zettelkasten. O
                # turno segue no SQLite (histórico do usuário) e na RAM (follow-up).
                # Modo confidencial (#5): nada é persistido — vive só na RAM da sessão.
                if not efemero and not mem.confidencial:
                    await append_chat_dump("IA", texto_final)
                mem.registrar_turno(texto_usuario, texto_final)
                if not mem.confidencial:
                    await asyncio.to_thread(db.save_chat, texto_usuario, texto_final, mem.conversa_id)
                await self._registrar_latencia(tracker, rota)
        except asyncio.CancelledError:
            raise  # barge-in: propaga para o LlamaManager parar o decode
        except Exception as exc:
            telemetry.error("PIPELINE", "Erro no pipeline de resposta", exc)
        # Sem `finally` liberando o idle: quem faz isso é o `interativo()` do chamador,
        # e só quando o ÚLTIMO pipeline em voo sair (ver AppContext.interativo).

    async def _registrar_latencia(self, tracker: LatencyTracker, rota: str) -> None:
        ttft, ttfa, total = (
            LatencyTracker._ms(tracker.ttft),
            LatencyTracker._ms(tracker.ttfa),
            LatencyTracker._ms(tracker.total()),
        )
        telemetry.track("LATENCIA", f"rota={rota} TTFT={ttft}ms TTFA={ttfa}ms total={total}ms")
        await asyncio.to_thread(db.save_latency, rota, ttft, ttfa, total)

    async def _rotear(self, texto_usuario: str, observacoes: str = ""):
        """Pergunta ao LLM qual ferramenta usar; devolve uma `tools.Decisao` ou None."""
        bruto = await self.ctx.llama.collect(
            prompts.prompt_router(self.tools.menu(), texto_usuario, observacoes),
            max_tokens=settings.max_tokens_router,
            system_prompt=prompts.SYS_ROUTER,
            temperature=0.0,
        )
        return tools.parse_decisao(bruto)

    async def _pipeline_tools(
        self, texto_usuario: str, send: Sender, primeira, auditar: bool = True,
        mem: Optional[SessionMemory] = None,
    ) -> str:
        """Loop agêntico CAPADO: executa ferramentas e então fala a resposta final.

        Ferramentas terminais (calcular/hora/salvar) encerram no 1º passo. As não
        terminais (buscar_web/ler_nota/listar_notas) podem encadear até
        `max_tool_steps`, mas o loop para assim que o roteador devolve 'responder'.
        A resposta final vai por streaming + chunking (TTS), preservando o pilar.

        `auditar` (False em modo confidencial) controla o registro na trilha (#27).
        `mem` (quando dado) recebe a reversão da mutação p/ o "mestre, desfaça" (#8) —
        cobre o caminho LLM (ex.: "me lembra de ligar amanhã", que o parse_rapido defere)."""
        observacoes: List[str] = []
        executadas: List[tuple] = []   # (Decisao, resultado) p/ computar a reversão (#8)
        decisao = primeira
        passos = 0
        while decisao and decisao.tool != "responder" and passos < settings.max_tool_steps:
            tool = self.tools.get(decisao.tool)
            if tool is None:
                break
            try:
                obs = await tool.executar(decisao.args, self.ctx)
            except Exception as exc:
                telemetry.error("TOOL", f"Falha na ferramenta '{decisao.tool}'", exc)
                obs = f"erro ao executar {decisao.tool}"
            if auditar and tool.auditavel:
                await asyncio.to_thread(db.registrar_auditoria, decisao.tool, obs)
            observacoes.append(f"[{decisao.tool}] {obs}")
            executadas.append((decisao, obs))
            passos += 1
            if tool.terminal:
                break
            decisao = await self._rotear(texto_usuario, "\n".join(observacoes))
        self._lembrar_reversao(mem, executadas)

        resultados = "\n".join(observacoes) if observacoes else "nenhum resultado"
        resposta = await self._responder_stream(
            prompts.prompt_resposta_ferramentas(resultados, texto_usuario), send
        )
        if resposta.strip():
            return resposta
        # Caso raro: o phrasing final saiu vazio. Não deixe o usuário no silêncio —
        # fala o resultado bruto da ferramenta. Só executa quando vazio, então não
        # pesa no TTFA do caminho normal.
        fala = resultados[:300] if observacoes else "Pronto."
        await send({"tipo": "token", "texto": fala})
        audio = await self.ctx.tts.synth_base64(fala)
        if audio:
            await send({"tipo": "audio", "base64": audio})
        return fala

    async def _emitir_falado(self, send: Sender, texto: str) -> None:
        """Emite um texto pronto: token (para a tela) + áudio (TTS). Sem LLM."""
        await send({"tipo": "token", "texto": texto})
        await self._falar_texto(send, texto)

    async def _executar_acoes_rapidas(
        self, acoes: List["tools.Decisao"], send: Sender, auditar: bool = True,
        mem: Optional[SessionMemory] = None, prefixo: str = "",
    ) -> str:
        """Executa uma ou mais ferramentas determinísticas e FALA o resultado direto —
        sem passar pelo LLM (o texto de retorno da ferramenta já é amigável). É o que
        torna 'mestre, põe pão na lista' instantâneo e barato.

        `auditar` (False em modo confidencial) controla o registro na trilha (#27).
        `mem` (quando dado) recebe a reversão desta ação p/ o "mestre, desfaça" (#8);
        `prefixo` prefixa a fala (ex.: "Desfeito. " ao executar a própria reversão)."""
        resultados: List[str] = []
        executadas: List[tuple] = []   # (Decisao, resultado) p/ computar a reversão
        for dec in acoes:
            tool = self.tools.get(dec.tool)
            if tool is None:
                continue
            try:
                obs = await tool.executar(dec.args, self.ctx)
            except Exception as exc:
                telemetry.error("MESTRE", f"Falha na ferramenta '{dec.tool}'", exc)
                obs = f"não consegui executar {dec.tool}"
            if auditar and tool.auditavel:
                await asyncio.to_thread(db.registrar_auditoria, dec.tool, obs)
            resultados.append(obs)
            executadas.append((dec, obs))
        self._lembrar_reversao(mem, executadas)
        final = prefixo + (" ".join(r for r in resultados if r) or "Pronto.")
        await self._emitir_falado(send, final)
        return final

    def _lembrar_reversao(self, mem: Optional[SessionMemory], executadas: List[tuple]) -> None:
        """Guarda na sessão as ações que DESFAZEM o que acabou de rodar (#8).

        Só sobrescreve o alvo de undo quando há de fato algo reversível — assim uma
        leitura (ler_lista) ou uma ação que falhou não apaga o undo pendente anterior."""
        if mem is None or not executadas:
            return
        rev = mestre.reverter(executadas)
        if rev:
            mem.ultima_reversivel = rev
            # guarda também as ações FORWARD p/ o "corrige" refazer com o valor certo (#9).
            mem.ultima_acao = [dec for dec, _ in executadas]

    async def _desfazer(self, send: Sender, mem: SessionMemory, auditar: bool) -> tuple:
        """Executa a reversão da última ação da sessão (#8). Devolve (fala, rota)."""
        revs = mem.ultima_reversivel
        if not revs:
            fala = "Não tenho nenhuma ação recente para desfazer."
            await self._emitir_falado(send, fala)
            return fala, "mestre:desfazer_vazio"
        # Consome ANTES de executar: a própria reversão não vira novo alvo de undo
        # (mem=None abaixo), então "desfaça, desfaça" não fica num ping-pong.
        mem.ultima_reversivel = None
        mem.ultima_acao = None
        fala = await self._executar_acoes_rapidas(
            revs, send, auditar=auditar, mem=None, prefixo="Desfeito. "
        )
        return fala, "mestre:desfazer"

    async def _corrigir(self, comando: str, send: Sender, mem: SessionMemory, auditar: bool) -> tuple:
        """Corta-e-corrige (#9): desfaz a última adição e refaz com o valor certo.

        "mestre, corrige para leite" (após "adiciona pão") = remover pão + adicionar leite.
        Reaproveita a reversão do #8 (undo) e refaz a ação forward trocando o valor."""
        certo = mestre.parse_correcao(comando)
        if not certo:
            fala = "Não entendi a correção. Diga, por exemplo, 'corrige para leite'."
            await self._emitir_falado(send, fala)
            return fala, "mestre:corrige_incompleto"
        redo = mestre.refazer_com(mem.ultima_acao or [], certo)
        if not redo:
            fala = "Não tenho uma ação recente de lista para corrigir."
            await self._emitir_falado(send, fala)
            return fala, "mestre:corrige_vazio"
        undo = list(mem.ultima_reversivel or [])
        mem.ultima_reversivel = None
        mem.ultima_acao = None
        # Executa undo + redo SEM registrar reversão automática (mem=None): abaixo
        # definimos o alvo de undo manualmente como só o REDO — assim correções
        # ENCADEADAS ("corrige para leite", depois "para água") ficam limpas, sem
        # ressuscitar o valor original a cada nova correção.
        fala = await self._executar_acoes_rapidas(
            undo + list(redo), send, auditar=auditar, mem=None, prefixo="Corrigido. "
        )
        mem.ultima_acao = list(redo)
        mem.ultima_reversivel = [tools.Decisao("remover_item", dict(d.args)) for d in redo]
        return fala, "mestre:corrige"

    async def _atalhos_cache(self) -> dict:
        """Atalhos-mestre (#2) carregados do DB uma vez por processo (apelido -> comando)."""
        if self._atalhos is None:
            self._atalhos = await asyncio.to_thread(db.listar_atalhos)
        return self._atalhos

    async def _criar_atalho(self, nome: str, send: Sender, mem: SessionMemory) -> tuple:
        """Grava um atalho nomeado para o ÚLTIMO comando-mestre resolvido (#2)."""
        alvo = mem.ultimo_comando_mestre
        if not alvo:
            fala = "Não tenho um comando recente para transformar em atalho."
            await self._emitir_falado(send, fala)
            return fala, "mestre:atalho_vazio"
        chave = textutils.normaliza(nome)
        await asyncio.to_thread(db.salvar_atalho, chave, alvo)
        # Já tem atalho -> não ofereça atalho de novo para essa intenção.
        await asyncio.to_thread(db.marcar_sugerido, textutils.normaliza(alvo))
        (await self._atalhos_cache())[chave] = alvo
        fala = f"Pronto. Agora é só dizer '{settings.palavra_mestre}, {nome}' que eu faço isso."
        await self._emitir_falado(send, fala)
        return fala, "mestre:atalho_criado"

    async def _talvez_sugerir_atalho(self, comando: str, send: Sender) -> Optional[str]:
        """Conta a intenção e, se ela virou hábito (e ainda não foi sugerida), OFERECE um
        atalho — uma vez só (#2). Devolve a fala da sugestão (p/ anexar ao histórico) ou None."""
        minimo = settings.atalho_sugestao_min
        if minimo <= 0:
            return None
        assinatura = textutils.normaliza(comando)
        n, ja_sugerido = await asyncio.to_thread(db.registrar_frequencia, assinatura, comando)
        if n < minimo or ja_sugerido:
            return None
        await asyncio.to_thread(db.marcar_sugerido, assinatura)
        fala = (
            f"Aliás, você já pediu isso {n} vezes. Se quiser um atalho, diga "
            f"'{settings.palavra_mestre}, atalho' e um apelido curto."
        )
        await self._emitir_falado(send, fala)
        return fala

    def _acao_confirmavel(self, acoes: List["tools.Decisao"]) -> Optional["tools.Decisao"]:
        """A 1ª ação DESTRUTIVA que exige confirmação (#25), ou None. Respeita o botão
        `confirmacao_habilitada` — desligado, nada é gateado (executa direto)."""
        if not settings.confirmacao_habilitada:
            return None
        for dec in acoes:
            tool = self.tools.get(dec.tool)
            if tool is not None and tool.confirmavel:
                return dec
        return None

    async def _fluxo_mestre(
        self, comando: str, send: Sender, tracker: LatencyTracker, mem: SessionMemory
    ) -> None:
        """Fluxo ISOLADO da palavra-mestre: comando de agente, não pergunta.

        1) `parse_rapido` resolve os comandos regulares SEM LLM. 2) Senão, o roteador
        LLM tenta mapear numa ferramenta. 3) Se nem o roteador reconhece uma AÇÃO, o
        comando é RECUSADO (isolamento rígido, decidido com o dono) e REGISTRADO como
        melhoria a revisar — nunca vira uma resposta de conhecimento."""
        if not comando.strip():
            fala = f"Sim, {settings.palavra_mestre.capitalize()}? Pode falar."
            await self._emitir_falado(send, fala)
            return

        # ATALHO (#2): se o comando INTEIRO casa um apelido salvo, expande para o comando
        # original antes de qualquer coisa — daí segue o fluxo normal como se o usuário
        # tivesse dito o comando completo.
        atalhos = await self._atalhos_cache()
        expandido = atalhos.get(textutils.normaliza(comando))
        if expandido:
            telemetry.track("MESTRE", f"Atalho '{comando}' -> '{expandido}'.")
            comando = expandido

        # COFRE DE CONFIRMAÇÃO (#25): se há uma ação destrutiva PENDENTE, "confirma" a
        # executa e "não/deixa" a aborta (abort tem precedência — na dúvida, não faz).
        # Qualquer OUTRO comando ABANDONA a pendência e segue normal (não prende o
        # usuário). Os gatilhos só valem com algo pendente (#15: sem gatilho global).
        pend = mem.confirmacao_pendente
        abortando = pend is not None and mestre.comando_abortar(comando)
        confirmando = pend is not None and not abortando and mestre.comando_confirmar(comando)
        if pend is not None and not abortando and not confirmando:
            mem.confirmacao_pendente = None   # comando novo supera a pendência

        # MODO CONFIDENCIAL (#5): meta-comando que mexe no estado da SESSÃO (por isso é
        # tratado aqui, não numa ferramenta). Liga/desliga e NÃO registra o próprio turno.
        modo = mestre.modo_confidencial(comando)
        if modo is not None:
            mem.confidencial = modo
            fala = (
                "Modo confidencial ativado. O que falarmos agora fica só nesta sessão — "
                "não salvo nada nem transformo em conhecimento."
                if modo else
                "Voltando ao normal. As conversas voltam a ser salvas e aprendidas."
            )
            await self._emitir_falado(send, fala)
            return

        # CONFIRMAÇÃO PENDENTE (#25) tem prioridade sobre um comando novo.
        if confirmando:
            dec = mem.confirmacao_pendente
            mem.confirmacao_pendente = None
            texto_final = await self._executar_acoes_rapidas(
                [dec], send, auditar=not mem.confidencial, mem=mem
            )
            rota = "mestre:confirmado"
        elif abortando:
            mem.confirmacao_pendente = None
            texto_final = "Ok, não vou fazer isso."
            await self._emitir_falado(send, texto_final)
            rota = "mestre:confirmacao_abortada"
        # ATALHO (#2): "mestre, atalho <nome>" nomeia o último comando resolvido.
        elif (nome_atalho := mestre.parse_atalho(comando)) is not None:
            texto_final, rota = await self._criar_atalho(nome_atalho, send, mem)
        # DESFAZER (#8): meta-comando que reverte a última ação reversível da sessão.
        # Vem antes do parse_rapido: "desfaça" não é uma ação nova a rotear.
        elif mestre.comando_desfazer(comando):
            texto_final, rota = await self._desfazer(send, mem, auditar=not mem.confidencial)
        elif mestre.tem_correcao(comando):
            # CORTA-E-CORRIGE (#9): "corrige para X" — antes do parse_rapido, pois um
            # "corrige ... na lista" tem gatilho de lista mas é correção, não add.
            texto_final, rota = await self._corrigir(comando, send, mem, auditar=not mem.confidencial)
        elif acoes := mestre.parse_composto(comando, datetime.now()):
            # #12 já pode ter devolvido VÁRIAS ações (comando encadeado "faz X e faz Y").
            # COFRE (#25): se alguma é destrutiva não-desfazível, executa as SEGURAS já e
            # deixa a destrutiva aguardando "confirma".
            confirmavel = self._acao_confirmavel(acoes)
            if confirmavel is not None:
                seguras = [a for a in acoes if a is not confirmavel]
                if seguras:
                    await self._executar_acoes_rapidas(
                        seguras, send, auditar=not mem.confidencial, mem=mem
                    )
                mem.confirmacao_pendente = confirmavel
                texto_final = f"Confirma que quer {mestre.descrever_acao(confirmavel)}? Diga 'mestre, confirma'."
                await self._emitir_falado(send, texto_final)
                rota = "mestre:aguarda_confirmacao"
            else:
                texto_final = await self._executar_acoes_rapidas(
                    acoes, send, auditar=not mem.confidencial, mem=mem
                )
                rota = "mestre:rapido"
        else:
            decisao = await self._rotear(comando)
            if decisao and decisao.tool != "responder" and self.tools.get(decisao.tool):
                tool = self.tools.get(decisao.tool)
                if settings.confirmacao_habilitada and tool.confirmavel:
                    mem.confirmacao_pendente = decisao
                    texto_final = f"Confirma que quer {mestre.descrever_acao(decisao)}? Diga 'mestre, confirma'."
                    await self._emitir_falado(send, texto_final)
                    rota = "mestre:aguarda_confirmacao"
                else:
                    texto_final = await self._pipeline_tools(
                        comando, send, decisao, auditar=not mem.confidencial, mem=mem
                    )
                    rota = f"mestre:tool:{decisao.tool}"
            else:
                # Não é ação reconhecida: recusa + registra para revisão.
                await asyncio.to_thread(db.registrar_comando_desconhecido, comando)
                fala = (
                    "Não reconheci um comando de agente aí. Posso, por exemplo, adicionar "
                    "itens a uma lista, criar um lembrete ou avisar quando algo acontecer."
                )
                await self._emitir_falado(send, fala)
                texto_final = fala
                rota = "mestre:desconhecido"

        # ATALHO DE INTENÇÃO FREQUENTE (#2): só AÇÕES resolvidas contam como "intenção"
        # (meta-comandos como desfazer/confirmar/atalho não). Guarda o último comando p/
        # o "atalho <nome>" e, se a intenção virou hábito, OFERECE um atalho (uma vez).
        if rota == "mestre:rapido" or rota.startswith("mestre:tool:"):
            mem.ultimo_comando_mestre = comando
            if not mem.confidencial:
                sugestao = await self._talvez_sugerir_atalho(comando, send)
                if sugestao and texto_final:
                    texto_final = f"{texto_final} {sugestao}"   # anexa ao histórico

        # Comando de agente NÃO alimenta o dump (não é conhecimento). Segue no SQLite
        # (histórico) e na RAM (contexto de follow-up), como as demais ações — salvo
        # em modo confidencial, onde nada é persistido (só a RAM).
        if texto_final:
            mem.registrar_turno(comando, texto_final)
            if not mem.confidencial:
                await asyncio.to_thread(db.save_chat, comando, texto_final, mem.conversa_id)
            await self._registrar_latencia(tracker, rota)

    def _pergunta_com_contexto(self, texto_usuario: str, mem: SessionMemory) -> str:
        """Prefixa o histórico recente à pergunta, só para o LLM de RESPOSTA.

        A recuperação já resolve pronomes (QueryOptimizer), mas o gerador recebia o
        texto cru e ficava cego a follow-ups ("explique melhor", "e sobre isso") — daí
        respondia sentinela mesmo com os átomos certos. Damos 2 turnos recentes (resposta
        anterior truncada) e marcamos [PERGUNTA ATUAL] para manter o foco. Sem histórico,
        devolve a pergunta intacta (custo zero na 1ª pergunta da sessão)."""
        hist = mem.chat_history
        if not hist:
            return texto_usuario
        turnos = []
        for q, a in list(hist)[-2:]:
            resumo = (a[:200] + "…") if len(a) > 200 else a
            turnos.append(f"Usuário: {q}\nVocê: {resumo}")
        return "[CONVERSA RECENTE]\n" + "\n".join(turnos) + f"\n\n[PERGUNTA ATUAL] {texto_usuario}"

    async def _texto_busca(self, texto_usuario: str, termos: str) -> str:
        """Texto que vai ao EMBEDDING da busca vetorial (o aterramento léxico segue
        usando `termos`, a query enxuta).

        Base (grátis): a pergunta natural inteira. O modelo de embedding é simétrico —
        uma frase completa casa muito melhor com os parágrafos do banco do que a query
        de 5 palavras. Com MENTE_RAG_HYDE=true, o LLM gera uma passagem hipotética no
        estilo das notas (HyDE) e a anexamos à pergunta: recall melhor ao custo de UMA
        chamada extra ao LLM — e só neste estágio, nunca quando a RAM já resolveu.
        """
        base = texto_usuario.strip() or termos
        if not settings.rag_hyde:
            return base
        try:
            hyde = await self.ctx.llama.collect(
                prompts.prompt_hyde(texto_usuario),
                max_tokens=settings.max_tokens_hyde,
                system_prompt=prompts.SYS_HYDE,
                temperature=0.0,
            )
            hyde = hyde.strip()
            if hyde:
                telemetry.track("HYDE", f"passagem: {hyde[:90]!r}")
                return f"{base}\n{hyde}"  # ancora na pergunta e enriquece o vetor
        except Exception as exc:
            telemetry.error("HYDE", "Falha ao gerar passagem hipotética", exc)
        return base

    async def _responder_contexto(
        self,
        contexto: str,
        texto_usuario: str,
        send: Sender,
        *,
        prompt_fn: Callable[[str, str], str] = prompts.prompt_resposta_cache,
        system: str = prompts.SYS_RESPOSTA,
        prefixo: str = "",
        max_tokens: int | None = None,
        instrucao_extra: str = "",
    ):
        """Responde por um contexto (RAM ou Banco) COM streaming, segurando o áudio até
        ter certeza de que não é o sentinela 'Não tenho informações suficientes'.

        Enquanto os tokens iniciais forem prefixo do sentinela, o buffer fica retido.
        Se o sentinela se confirmar, devolve None (o pipeline escala) e NADA é falado.
        Se divergir, libera o buffer e segue em streaming normal (TTFA preservado).

        `prompt_fn`/`system` permitem reusar este guard na FUSÃO por fonte (SYS_FUSAO).
        `prefixo` (ex.: '\\n\\n') é emitido só na 1ª emissão REAL — separa parágrafos das
        passadas sem vazar quebra de linha quando a passada acaba em sentinela.
        """
        chunker = SentenceChunker()
        texto_final = ""
        buffer = ""
        decidido = False
        # Governador de verbosidade (#7): a pergunta define quanto a GPU decodifica e se
        # há instrução de brevidade. Sem nível (None) = comportamento de sempre.
        sistema = f"{system}\n{instrucao_extra}" if instrucao_extra else system
        async for token in self.ctx.llama.stream(
            prompt_fn(contexto, texto_usuario),
            max_tokens=max_tokens if max_tokens is not None else settings.max_tokens_resposta,
            system_prompt=sistema,
        ):
            texto_final += token
            if decidido:
                await send({"tipo": "token", "texto": token})
                frases = chunker.push(token)
                if frases:
                    await self._falar(send, frases)
                continue

            buffer += token
            norm = textutils.normaliza(buffer)
            if SENTINELA_INSUF in norm:
                return None                       # confirmou insuficiência
            if not SENTINELA_INSUF.startswith(norm):
                decidido = True                   # divergiu do sentinela -> resposta real
                if prefixo:
                    await send({"tipo": "token", "texto": prefixo})
                await send({"tipo": "token", "texto": buffer})
                frases = chunker.push(buffer)
                if frases:
                    await self._falar(send, frases)
            # senão: ainda é prefixo do sentinela -> segura o buffer

        if not decidido:
            return None                           # só saiu (prefixo do) sentinela
        resto = chunker.flush()
        if resto:
            await self._falar(send, [resto])
        return texto_final

    async def _falar_status(self, send: Sender, texto: str) -> None:
        """Filler ESPECÍFICO: diz em poucas palavras o que está sendo feito (texto+voz).
        Só na escalada web (a única espera longa) — o local já streama rápido, sem filler.
        Sem chamada extra ao LLM: é template, então não pesa no TTFA."""
        await send({"tipo": "token", "texto": texto + " "})
        audio = await self.ctx.tts.synth_base64(texto)
        if audio:
            await send({"tipo": "audio", "base64": audio})

    def _msg_web(self, termos: str) -> str:
        # Rotaciona para não ficar repetitivo/chato quando várias perguntas escalam.
        variantes = [
            f"Isso não está nas suas notas — vou buscar sobre {termos} na web.",
            f"Não tenho isso salvo. Procurando {termos} online.",
            f"Vou complementar com a web sobre {termos}.",
        ]
        msg = variantes[self._filler_i % len(variantes)]
        self._filler_i += 1
        return msg

    async def _responder_web(
        self, termos: str, texto_usuario: str, send: Sender, mem: SessionMemory,
        consulta_rank: str | None = None, efemero: bool = False,
        nivel: "verbosidade.Nivel | None" = None,
    ) -> str:
        # Query da WEB: se a pergunta CITA uma expressão/ditado, busca a frase citada —
        # ela é o alvo, e o extrator de 5 palavras a descartava ('saiu expressão pega
        # prato' em vez de 'pega um prato faz a linha dá um tiro na farinha'). Senão, a
        # query enxuta de sempre.
        query_web = frase_citada(consulta_rank or texto_usuario) or termos

        # Filler específico mascara a latência da busca web (diz o que está fazendo).
        await self._falar_status(send, self._msg_web(query_web))

        # `query_web` faz o DDG; `consulta_rank` (pergunta natural crua) guia o ranking
        # dos trechos do deep-fetch — o embedding é simétrico, então a frase inteira
        # casa melhor com os parágrafos das páginas que 5 keywords.
        dados_web = await self.ctx.web.search(query_web, consulta=consulta_rank or termos)
        # Pre-fetch é "curiosidade": baixa contexto AMPLO do tema para virar átomo.
        # Não faz sentido nenhum sobre um dado que expira em horas — e era ele que
        # engordava o vault com dezenas de notas por pergunta sobre o tempo. Em modo
        # confidencial (#5) também não: a curiosidade viraria átomo permanente.
        if not efemero and not mem.confidencial:
            self.ctx.track_task(self._prefetch(termos, mem))  # background, web-only (ref. retida)

        # Sem dados locais NEM web (ex.: pergunta não-buscável): NÃO deixe o LLM falar
        # o sentinela cru "Não tenho informações suficientes". Fala um retorno gracioso
        # e pula o decode (mais rápido). O sentinela é sinal interno, não fala de UX.
        if NENHUM in dados_web:
            fala = "Não encontrei isso nas suas notas nem na web."
            await send({"tipo": "token", "texto": fala})
            audio = await self.ctx.tts.synth_base64(fala)
            if audio:
                await send({"tipo": "audio", "base64": audio})
            return fala

        # RAM SEMPRE: é memória de SESSÃO, morre com ela, e é o que faz o follow-up
        # ("e amanhã?") enxergar o dado. O que não pode é virar átomo permanente.
        mem.lembrar(termos, dados_web)
        if efemero:
            telemetry.track("ETL", f"'{termos}' é efêmero — fora da fila (não vira átomo).")
        elif mem.confidencial:
            telemetry.track("ETL", "Modo confidencial — resultado fora da fila (não vira átomo).")
        else:
            mem.enfileirar_etl(termos, dados_web)

        # A web VOLTOU dados, mas eles podem não responder (projeto privado, snippet
        # sem o número etc.). Passa pelo MESMO guard anti-sentinela do local: se o LLM
        # concluir insuficiência, NÃO fala o sentinela cru — devolve None e caímos num
        # retorno gracioso. Antes usava _responder_stream (sem guard) e o sentinela
        # "Não tenho informações suficientes" vazava falado para o usuário.
        resposta = await self._responder_contexto(
            dados_web, texto_usuario, send,
            prompt_fn=prompts.prompt_resposta_web, system=prompts.SYS_RESPOSTA_WEB,
            max_tokens=nivel.max_tokens if nivel else None,
            instrucao_extra=nivel.instrucao if nivel else "",
        )
        if resposta is not None:
            return resposta
        fala = "Procurei, mas não achei uma resposta clara sobre isso nas suas notas nem na web."
        await send({"tipo": "token", "texto": fala})
        audio = await self.ctx.tts.synth_base64(fala)
        if audio:
            await send({"tipo": "audio", "base64": audio})
        return fala

    async def _prefetch(self, tema: str, mem: SessionMemory) -> None:
        ctx_amplo = await self.ctx.web.prefetch(tema)
        if ctx_amplo:
            mem.lembrar(tema, ctx_amplo)
            # A "curiosidade" — contexto amplo que veio junto mas NÃO era necessário
            # falar agora — também é enfileirada pro ETL: vira átomos #conhecimento_novo
            # e engorda a base sobre o que o usuário demonstrou interesse.
            mem.enfileirar_etl(tema, ctx_amplo)

    async def _consolidar_fontes(self, fontes: List[str]) -> None:
        """Promoção: tira a tag #conhecimento_novo dos arquivos-fonte que foram usados
        numa resposta local. Best-effort e idempotente — só reescreve se a tag existir
        (evita bumps de mtime inúteis que disparariam reindexação à toa). O reindex do
        texto no Chroma acontece na próxima sync (fim de sessão); o arquivo no vault já
        reflete a mudança na hora, que é o que o usuário vê no Obsidian."""
        tag = prompts.TAG_NOVO
        promovidos = 0
        for src in fontes:
            try:
                def _promover(caminho=src) -> bool:
                    with open(caminho, "r", encoding="utf-8") as f:
                        conteudo = f.read()
                    if tag not in conteudo:
                        return False
                    novo = textutils.remover_tag(conteudo, tag)
                    with open(caminho, "w", encoding="utf-8") as f:
                        f.write(novo)
                    return True

                if await asyncio.to_thread(_promover):
                    promovidos += 1
            except OSError as exc:
                telemetry.warn("PROMOCAO", f"Não consegui consolidar {src}: {exc}")
        if promovidos:
            telemetry.track("PROMOCAO", f"{promovidos} nota(s) consolidada(s) (tirado {tag}).")

    async def _responder_stream(
        self, prompt_resposta: str, send: Sender, system: str = prompts.SYS_RESPOSTA
    ) -> str:
        chunker = SentenceChunker()
        texto_final = ""
        async for token in self.ctx.llama.stream(
            prompt_resposta,
            max_tokens=settings.max_tokens_resposta,
            system_prompt=system,
        ):
            texto_final += token
            await send({"tipo": "token", "texto": token})
            frases = chunker.push(token)
            if frases:
                await self._falar(send, frases)
        resto = chunker.flush()
        if resto:
            await self._falar(send, [resto])
        return texto_final

    @staticmethod
    def _lotes_por_chars(itens: List[str], budget: int) -> List[List[str]]:
        """Agrupa átomos em lotes cujo texto somado cabe em `budget` (protege o n_ctx)."""
        lotes: List[List[str]] = []
        atual: List[str] = []
        tam = 0
        for it in itens:
            if atual and tam + len(it) > budget:
                lotes.append(atual)
                atual, tam = [], 0
            atual.append(it)
            tam += len(it)
        if atual:
            lotes.append(atual)
        return lotes

    async def _sintese_sob_demanda(
        self, tema: str, send: Sender, mem: SessionMemory, tracker: LatencyTracker
    ) -> None:
        """Síntese sob Demanda (#23): "o que eu sei sobre X". Fluxo SEPARADO em map-reduce
        para não estourar o n_ctx: recupera MUITOS átomos, resume em lotes (map) e combina
        os parciais numa fala coerente (reduce). Só o banco local — é "o que EU sei"."""
        await self._falar_status(send, f"Deixa eu reunir o que você tem sobre {tema}.")
        atomos = await self.ctx.vectorstore.buscar_conteudos(tema, settings.sintese_top_k)

        if not atomos:
            texto_final = f"Não encontrei nada sobre {tema} nas suas notas."
            await self._emitir_falado(send, texto_final)
        else:
            lotes = self._lotes_por_chars(atomos, settings.sintese_lote_chars)
            parciais: List[str] = []
            for lote in lotes:
                p = await self.ctx.llama.collect(
                    prompts.prompt_sintese_tema_map(tema, "\n\n".join(lote)),
                    max_tokens=settings.max_tokens_sintese_tema,
                    system_prompt=prompts.SYS_SINTESE_TEMA,
                )
                if p.strip():
                    parciais.append(p.strip())
            telemetry.track("SINTESE", f"'{tema}': {len(atomos)} átomos em {len(lotes)} lote(s).")
            if not parciais:
                texto_final = f"Tenho notas sobre {tema}, mas não consegui resumir agora."
                await self._emitir_falado(send, texto_final)
            else:
                # REDUCE: combina os parciais numa fala (streaming). Um lote só também passa
                # pelo reduce — ele limpa/encurta o resumo bruto do map numa fala coerente.
                juntos = "\n\n".join(f"- {p}" for p in parciais)
                texto_final = await self._responder_stream(
                    prompts.prompt_sintese_tema_reduce(tema, juntos), send,
                    system=prompts.SYS_SINTESE_TEMA,
                )

        if texto_final:
            mem.registrar_turno(f"o que eu sei sobre {tema}", texto_final)
            if not mem.confidencial:
                await asyncio.to_thread(
                    db.save_chat, f"o que eu sei sobre {tema}", texto_final, mem.conversa_id
                )
        await self._registrar_latencia(tracker, "sintese")


# ==========================================================================
# ETL Post-Chat / Idle
# ==========================================================================
class EtlProcessor:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def _esperar_idle(self) -> None:
        """Cede a vez para a inferência interativa antes de cada tarefa pesada."""
        await self.ctx.interactive_idle.wait()

    async def _salvar_atomos(self, texto: str, prefixo: str, tipo_log: str) -> int:
        """Salva UM ARQUIVO POR ÁTOMO (Zettelkasten puro). Assim a promoção fica
        precisa por ideia: só o átomo realmente reusado perde o #conhecimento_novo,
        não os vizinhos que calharam de estar no mesmo documento. Devolve quantos salvou.

        Todo bloco passa por `normalizar_atomo` ANTES de virar arquivo: o formato do
        átomo é imposto no código, não confiado ao LLM (ver a docstring de lá — o A/B
        mostrou que nenhum modelo o entrega de forma confiável, e sem as tags a
        promoção nunca acontece).

        Fallback: se o LLM não usou nenhum '##' (formato quebrado), salva o texto inteiro
        como 1 átomo em vez de descartar o conhecimento em silêncio."""
        blocos = dividir_atomos(texto)
        if not blocos and texto.strip():
            blocos = [texto.strip()]
        agora = datetime.now()
        salvos = 0
        duplicados = 0
        for i, bloco in enumerate(blocos):
            bloco = normalizar_atomo(bloco, prefixo, agora)
            if not bloco.strip():
                continue
            # DEDUP contra o banco (pedido: "impeça a duplicação"). Um átomo quase
            # idêntico a um já indexado não vira arquivo novo — senão a base incha com
            # a mesma ideia e o rag_top_k recupera clones. Fail-open sem embeddings.
            if await self._ja_no_banco(strip_frontmatter(bloco)):
                duplicados += 1
                continue
            nome = f"{prefixo}_{_slug_titulo(bloco)}_{int(time.time())}_{i}.md"
            caminho = os.path.join(str(self.ctx.settings.dir_conhecimento_novo), nome)

            def _save(c=caminho, body=bloco) -> None:
                with open(c, "w", encoding="utf-8") as f:
                    f.write(body + "\n")

            try:
                await asyncio.to_thread(_save)
                await asyncio.to_thread(db.log_etl, tipo_log, nome, "CONCLUIDO")
                salvos += 1
            except OSError as exc:
                telemetry.error(tipo_log, f"Falha ao salvar átomo {nome}", exc)
        if duplicados:
            telemetry.track(tipo_log, f"Dedup: {duplicados} átomo(s) já no banco, ignorados.")
        return salvos

    async def _ja_no_banco(self, corpo: str) -> bool:
        """True se um átomo quase idêntico já está indexado (distância < dedup_dist_max).

        Fail-open: sem embeddings/loja (testes) devolve False — dedup é uma trava de
        qualidade, não pode virar bloqueio de escrita. Barato: 1 embedding + 1 vizinho."""
        store = self.ctx.vectorstore
        if store is None or getattr(store, "_store", None) is None or not corpo.strip():
            return False
        try:
            res = await asyncio.to_thread(store._store.similarity_search_with_score, corpo, 1)
        except Exception as exc:
            telemetry.warn("DEDUP", f"Falha ao checar duplicata (seguindo com o save): {exc}")
            return False
        if not res:
            return False
        _doc, dist = res[0]
        return dist < settings.dedup_dist_max

    async def process_queue(self, itens: List[Tuple[str, str]]) -> None:
        if not itens:
            return
        telemetry.track("ETL_POST_CHAT", f"Sintetizando {len(itens)} pesquisas da sessão.")
        total = 0
        # Fila, não `for`: um item preemptado volta pro topo e é RETENTADO — a síntese
        # dele foi jogada fora no meio, e descartar o item silenciosamente seria perder
        # exatamente o conhecimento que este processo existe para reter.
        pendentes = list(itens)
        while pendentes:
            # Cede a vez ANTES de cada tentativa: com o usuário falando, isto bloqueia
            # aqui (barato) em vez de começar uma síntese que será cortada. É também o
            # que impede o retry de virar spin — só reacorda quando a GPU está livre.
            await self._esperar_idle()
            tema, dados = pendentes[0]
            try:
                conteudo = await self.ctx.llama.collect(
                    prompts.prompt_sintese(tema, dados),
                    max_tokens=settings.max_tokens_sintese,
                    system_prompt=prompts.SYS_SINTESE,
                    preemptible=True,   # background: a pergunta do usuário passa na frente
                )
            except InferenciaPreemptada:
                telemetry.track("ETL_POST_CHAT", f"'{tema}' cedeu a GPU — será retomado no idle.")
                continue                      # item continua em pendentes[0]
            except Exception as exc:
                telemetry.error("ETL_POST_CHAT", f"Falha ao sintetizar '{tema}'", exc)
                pendentes.pop(0)              # falha real: não retenta em loop
                continue
            pendentes.pop(0)
            try:
                total += await self._salvar_atomos(conteudo, "Sintese", "ETL_POST_CHAT")
            except Exception as exc:
                telemetry.error("ETL_POST_CHAT", f"Falha ao salvar átomos de '{tema}'", exc)

        await self.ctx.vectorstore.sync()
        telemetry.track("ETL_POST_CHAT", f"Banco Vetorial atualizado ({total} átomos).")

    async def summarize_dump(self) -> None:
        """Destila o histórico BRUTO da conversa (texto+voz) em NOTAS ATÔMICAS
        Zettelkasten — mesma regra da base — em vez do antigo 'Resumo_Sessao'
        estruturado. Cada ideia trocada vira um átomo recuperável, nascendo como
        #conhecimento_novo (consolida quando usado). O dump só é limpo se a síntese
        for salva com sucesso — senão a conversa fica pra próxima passada (nada se perde)."""
        path = settings.arquivo_chat_dump
        if not os.path.exists(path):
            return
        try:
            conteudo = await asyncio.to_thread(
                lambda: open(path, "r", encoding="utf-8").read().strip()
            )
        except OSError as exc:
            telemetry.error("IDLE", "Erro ao ler dump", exc)
            return
        if len(conteudo) < 50:
            return

        await self._esperar_idle()
        telemetry.track("IDLE", "Atomizando histórico da conversa (Zettelkasten)...")
        try:
            atomos = await self.ctx.llama.collect(
                prompts.prompt_sintese_conversa(conteudo),
                max_tokens=settings.max_tokens_resumo,
                system_prompt=prompts.SYS_SINTESE_CONVERSA,
                preemptible=True,   # background: a pergunta do usuário passa na frente
            )
        except InferenciaPreemptada:
            # Sair aqui é seguro E é o ponto: o dump só é limpo mais abaixo, depois de
            # salvar. Cedemos a GPU sem tocar em nada, e a conversa inteira continua no
            # arquivo para a próxima passada de idle.
            telemetry.track("IDLE", "Atomização cedeu a GPU — dump preservado p/ a próxima.")
            return
        atomos = atomos.strip()

        async def _limpar_dump() -> None:
            try:
                await asyncio.to_thread(lambda: open(path, "w").close())
            except OSError as exc:
                telemetry.error("IDLE", "Erro ao limpar dump", exc)

        # O prompt manda responder só 'NADA' quando não há conhecimento a reter
        # (conversa de small talk). Nesse caso não cria nota — mas limpa o dump.
        if not atomos or atomos.upper().strip(".!\n ") == "NADA":
            await _limpar_dump()
            telemetry.track("IDLE", "Conversa sem conhecimento novo a reter.")
            return

        # Um arquivo por átomo (mesma regra da fila web). O dump só é limpo se ALGO
        # foi retido — senão a conversa fica pra próxima passada (nada se perde).
        salvos = await self._salvar_atomos(atomos, "Conversa", "IDLE_CONVERSA")
        if salvos:
            await _limpar_dump()
            telemetry.track("IDLE", f"Conversa atomizada: {salvos} átomo(s).")
            await self.ctx.vectorstore.sync()
        else:
            telemetry.warn("IDLE", "Nenhum átomo salvo da conversa — dump preservado p/ retry.")

    async def pesquisa_proativa(self) -> None:
        """No idle, busca na web as maiores LACUNAS (perguntas que a RAM E o banco não
        responderam), atomiza e insere — para a próxima vez já achar local. É o que faz
        o app "sempre ter algo novo pronto" sobre as dúvidas reais do usuário.

        Anti-duplicação em DOIS níveis:
        1. ALVO: se o banco JÁ cobre a lacuna (relevante) — porque outra passada de idle
           já a trouxe, ou a base cresceu — não re-pesquisa; só marca como resolvida.
        2. ÁTOMO: `_salvar_atomos` descarta o que já está indexado (dedup_dist_max).

        Preempção: cada síntese é `preemptible`. Se o usuário volta, InferenciaPreemptada
        encerra a pesquisa — o idle acabou, e as lacunas não-tocadas ficam para a próxima."""
        if not settings.idle_pesquisa_proativa:
            return
        lacunas = await asyncio.to_thread(db.get_lacunas, settings.idle_pesquisa_max * 4)
        if not lacunas:
            return
        feitas = 0
        for lac in lacunas:
            if feitas >= settings.idle_pesquisa_max:
                break
            termos = lac["termos"]
            chave = textutils.normaliza(termos)
            # Backstop contra lacuna inútil já na tabela (legada, de antes do filtro na
            # escalada): 'ok' (trivial) e 'dolar 542' (sem núcleo) nunca viram pesquisa.
            if not lacuna_pesquisavel(termos):
                await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
                continue
            await self._esperar_idle()
            # Nível 1 — o banco já cobre? (cresceu desde que a lacuna foi vista)
            local = await self.ctx.vectorstore.search(termos, texto_busca=termos)
            if local.relevante:
                await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
                continue
            dados = await self.ctx.web.search(termos, consulta=termos)
            if not dados or dados == NENHUM:
                await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
                continue
            try:
                conteudo = await self.ctx.llama.collect(
                    prompts.prompt_sintese(termos, dados),
                    max_tokens=settings.max_tokens_sintese,
                    system_prompt=prompts.SYS_SINTESE,
                    preemptible=True,
                )
            except InferenciaPreemptada:
                telemetry.track("ETL_PROATIVO", "Usuário voltou — pesquisa proativa adiada.")
                return
            except Exception as exc:
                telemetry.error("ETL_PROATIVO", f"Falha ao sintetizar lacuna '{termos}'", exc)
                await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
                continue
            # Nível 2 (dedup por átomo) acontece dentro de _salvar_atomos.
            salvos = await self._salvar_atomos(conteudo, "Proativa", "ETL_PROATIVO")
            await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
            if salvos:
                feitas += 1
                telemetry.track("ETL_PROATIVO", f"Lacuna '{termos}': {salvos} átomo(s) novos.")
        if feitas:
            await self.ctx.vectorstore.sync()
            telemetry.track("ETL_PROATIVO", f"Pesquisa proativa: {feitas} lacuna(s) trazida(s) ao banco.")

    async def run_idle(self, itens: List[Tuple[str, str]]) -> None:
        """Orquestra o idle: 1) atomiza as pesquisas da fila, 2) atomiza a conversa,
        3) PESQUISA PROATIVA das lacunas, 4) DESCARREGA o modelo, liberando a VRAM (o
        pilar pedido: a GPU volta pra outros trabalhos quando o chat para).

        A ordem importa e foi pedida assim: o ETL PRECISA do modelo, então o unload é o
        ÚLTIMO passo. E só descarrega se ninguém voltou a interagir no meio-tempo —
        `interactive_idle` está SETADO quando não há inferência interativa em voo; se o
        usuário mandou algo, o pipeline o limpou e o unload é pulado (o próprio pipeline
        religou/manteve o modelo). Se descarregar e a mensagem chegar logo depois,
        `ensure_loaded` (no stream) religa: seguro nas duas direções."""
        await self.process_queue(itens)
        await self.summarize_dump()
        await self.pesquisa_proativa()

        if not settings.idle_descarregar_modelo:
            return
        if not self.ctx.interactive_idle.is_set():
            telemetry.track("ETL_POST_CHAT", "Interação retomada no idle — modelo mantido.")
            return
        await self.ctx.llama.unload()
