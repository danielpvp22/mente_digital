"""
Atomização de arquivos (um .md por ideia — Zettelkasten puro).

Funções PURAS que impõem a ESTRUTURA do átomo em vez de pedi-la ao LLM — o A/B
(eval/ab_modelos.py) provou que nenhum modelo entrega o formato de forma confiável,
e cada um falha de um jeito oposto. O LLM entrega a IDEIA; o Python entrega a
SINTAXE. Extraído do agent.py na modularização (o módulo não conhece Agent, GPU
nem WebSocket — só texto entra, texto sai; datas são injetadas).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Tuple

import prompts
import textutils
from rag import strip_frontmatter

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
