"""
Palavra-mestre: um fluxo ISOLADO e determinístico para acionar os agentes.

Ideia (decidida com o dono do projeto): quando a mensagem COMEÇA pela palavra-mestre
(default "mestre"), ela é tratada como um COMANDO de agente, não como pergunta de
conhecimento. Isso separa os dois mundos sem tocar no pipeline de hoje — sem a
palavra-mestre, nada muda.

Dois níveis, nesta ordem (o 2º só se o 1º falhar):
1. `parse_rapido` — reconhece os comandos MAIS REGULARES por regex e devolve a(s)
   `tools.Decisao` diretamente, SEM chamar o LLM (o pedido: "não pela LLM, só se
   necessário"). Cobre listas (add/ler/remover) e lembretes (listar/cancelar, alarme
   só-horário). Puro/testável — o instante de referência é injetado (`agora`).
2. Se `parse_rapido` devolve None, o chamador cai no roteador LLM (que extrai a
   mensagem de um "me lembra de ligar pro dentista amanhã", coisa que regex não faz
   bem). Só o que nem o roteador reconhece é RECUSADO e registrado para revisão.

Comandos compostos ("adicione X na lista E me lembre de Y") são deliberadamente
devolvidos como None aqui: uma frase = uma ação no roteador. Ficam registrados como
melhoria a revisar quando nem o roteador dá conta.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

import agenda
import textutils
import tools

# Frase da lista: "na lista", "à minha lista", "lista de compras"... O nome opcional
# vem depois de "de". Palavras estruturais são ASCII, então casam em `.lower()`
# (que preserva o comprimento — importante para fatiar o texto ORIGINAL por índice).
_LISTA_RE = re.compile(
    r"(?:\b(?:na|no|nas|nos|da|das|do|dos|a|à|em)\s+)?(?:minha\s+)?lista(?:\s+de\s+([a-zà-ÿ]+))?"
)
_ADD_RE = re.compile(
    r"\b(?:por\s+favor|favor|adicion\w*|acrescent\w*|coloc\w*|poe|poem|bot[ae]\w*|"
    r"inclu\w*|quero|preciso|me)\b"
)
_REM_RE = re.compile(r"\b(?:remov\w*|tir[ae]\w*|tirar|exclu\w*|apag\w*|delet\w*|retir\w*)\b")
_READ_RE = re.compile(r"\b(?:mostr\w*|ler|le|leia|ver|quais|que\s+tem|o\s+que\s+tem|liste?)\b")
_SEP_ITENS_RE = re.compile(r"\s*,\s*|\s+e\s+")

_GATILHO_LEMBRETE = ("lembr", "alarme", "despertador", "timer", "cronometro",
                     "temporizador", "acorda", "acordar")
_GATILHO_CAPTURA = ("anota", "anotar", "captur", "inbox", "nota rapida")
# Palavras da MOLDURA da captura (não fazem parte do texto anotado) — consumidas só do
# COMEÇO (o while abaixo), então um 'que'/'na' legítimo no meio do texto é preservado.
_CAPTURA_MOLDURA_RE = re.compile(
    r"\b(?:anota\w*|anotar|captur\w*|nota\s+rapida|joga\w*|poe|bota\w*|coloca\w*|manda\w*|"
    r"guarda\w*|na|no|minha|meu|inbox|rapido|isso|ai|o\s+seguinte|que)\b"
)


def separar(texto: str, palavra: str) -> Optional[str]:
    """Se a 1ª palavra da frase é a palavra-mestre, devolve o RESTO (sem ela); senão None.

    "" (string vazia) significa que o usuário só disse a palavra-mestre. Puro/testável.
    Casa por forma normalizada (sem acento, minúsculo), então "Mestre," e "mestre"
    são o mesmo. Só a PRIMEIRA palavra conta — "mestrado é legal" não ativa.
    """
    if not palavra or not texto or not texto.strip():
        return None
    alvo = textutils.normaliza(palavra)
    m = re.match(r"\s*([^\s,:.!?]+)(?:[\s,:.!?]+(.*))?$", texto.strip(), re.DOTALL)
    if not m:
        return None
    if textutils.normaliza(m.group(1)) != alvo:
        return None
    return (m.group(2) or "").strip()


def parse_rapido(comando: str, agora: datetime) -> Optional[List[tools.Decisao]]:
    """Tenta resolver o comando SEM LLM. Devolve a lista de ações ou None (defere ao LLM)."""
    if not comando or not comando.strip():
        return None
    orig = comando.strip()
    low = orig.lower()          # preserva comprimento (fatiar por índice é seguro)
    norm = textutils.normaliza(orig)

    tem_lembrete_cmd = any(g in norm for g in _GATILHO_LEMBRETE)
    tem_lista = "lista" in norm

    # COMPOSTO (lista + lembrete na mesma frase): uma ação por vez — defere ao LLM.
    if tem_lembrete_cmd and tem_lista:
        return None

    # WATCHER ("me avise quando X"): extrair condição/termos é fuzzy — defere ao LLM.
    if "avis" in norm and "quando" in norm:
        return None

    # CAPTURA RÁPIDA ("anota rápido: X", "captura isso: X"): jogar na inbox sem processar.
    # Vem antes das listas: um gatilho de captura explícito sempre vence.
    if any(g in norm for g in _GATILHO_CAPTURA):
        texto = _texto_captura(orig)
        return [tools.Decisao("capturar_nota", {"texto": texto})] if texto else None

    # -- lembretes: cancelar / listar / criar (alarme só-horário) --------------
    if tem_lembrete_cmd:
        if re.search(r"\bcancel\w*", norm) and "lembrete" in norm:
            num = re.search(r"\d+", norm)
            return [tools.Decisao("cancelar_lembrete", {"id": num.group()})] if num else None
        if re.search(r"\b(meus|minhas|quais|liste?|listar|mostr\w*|ver)\b", norm) and (
            "lembrete" in norm or "aviso" in norm
        ):
            return [tools.Decisao("listar_lembretes", {})]
        # Criar: só determinístico quando é alarme PURO (sem mensagem). Com mensagem
        # ("lembra de ligar pro dentista"), a extração é melhor no LLM -> None.
        dt, _rec = agenda.parse_quando(orig, agora)
        if dt is None:
            return None
        if _tem_mensagem(norm):
            return None
        titulo = "Alarme" if ("alarme" in norm or "despertador" in norm) else "Lembrete"
        # `quando`=orig: o próprio tool re-parseia o horário (parse_quando ignora prosa).
        return [tools.Decisao("criar_lembrete", {"quando": orig, "mensagem": titulo})]

    # -- listas (compras / tarefas) --------------------------------------------
    if tem_lista:
        nome = _nome_lista(low)
        tem_add = bool(_ADD_RE.search(low)) and not _REM_RE.search(low)
        tem_rem = bool(_REM_RE.search(low))
        if tem_rem:
            item = _texto_sem(orig, low, _REM_RE).strip(" ,.;:")
            return [tools.Decisao("remover_item", {"lista": nome, "item": item})] if item else None
        if tem_add:
            itens = _itens_para_lista(orig, low)
            if not itens:
                return None
            return [tools.Decisao("adicionar_item", {"lista": nome, "item": it}) for it in itens]
        # Sem verbo de add/remove + "lista" presente (com ou sem verbo de leitura) = ler.
        return [tools.Decisao("ler_lista", {"lista": nome})]

    return None


def _texto_captura(orig: str) -> str:
    """Tira a moldura ('anota rápido:', 'captura isso') e devolve só o que foi anotado.

    Remove os termos de moldura só do COMEÇO (para não apagar um 'que'/'na' legítimo no
    meio do texto), depois limpa pontuação inicial. O casamento é feito sobre uma versão
    SEM ACENTO e minúscula (a moldura 'rápido'/'à' não bate com regex ascii), preservando
    o comprimento para fatiar o texto ORIGINAL pelos mesmos índices."""
    baixa = orig.lower()
    ascii_baixa = textutils.sem_acento(baixa)
    # sem_acento é 1:1 para os acentos do PT (á->a, ç->c...); se algum caractere exótico
    # quebrar o alinhamento de comprimento, cai para o texto acentuado (sem o ganho ascii).
    ms = ascii_baixa if len(ascii_baixa) == len(baixa) else baixa
    o = orig
    while True:
        stripped = ms.lstrip()
        m = _CAPTURA_MOLDURA_RE.match(stripped)
        if not m:
            break
        desloc = len(ms) - len(stripped)
        corte = desloc + m.end()
        o, ms = o[corte:], ms[corte:]
    return re.sub(r"^[\s:,.\-–]+", "", o).strip()


def _nome_lista(low: str) -> str:
    """Nome da lista após 'lista de X'; default 'compras'."""
    m = _LISTA_RE.search(low)
    if m and m.group(1):
        return m.group(1).strip()
    return "compras"


def _texto_sem(orig: str, low: str, verbo_re: re.Pattern) -> str:
    """Remove do ORIGINAL a frase da lista e as palavras de verbo — sobra o texto útil."""
    o, l = _del(orig, low, _LISTA_RE)
    o, _ = _del(o, l, verbo_re)
    # tira conectores iniciais soltos ('de', 'os', 'as') que sobram após o corte
    return re.sub(r"^\s*(?:de|do|da|os|as|o|a)\s+", "", o.strip(), flags=re.IGNORECASE).strip()


def _itens_para_lista(orig: str, low: str) -> List[str]:
    """Extrai os itens de um comando de adição (aceita vários: 'leite, farinha e ovos')."""
    texto = _texto_sem(orig, low, _ADD_RE)
    itens = []
    for parte in _SEP_ITENS_RE.split(texto):
        it = parte.strip(" .,:;")
        # Descarta ruído: vazio, ou uma "frase" longa (provável cauda de comando composto
        # que escapou do guard) — item de lista tem no máximo umas poucas palavras.
        if it and len(it.split()) <= 4 and any(c.isalnum() for c in it):
            itens.append(it)
    return itens


def _tem_mensagem(norm: str) -> bool:
    """Sobra algum ASSUNTO depois de tirar gatilho + tempo + fillers? (então há mensagem)."""
    resto = norm
    fillers = (
        "lembrete", "lembra", "lembre", "lembrar", "alarme", "despertador", "timer",
        "cronometro", "temporizador", "acorda", "acordar", "me", "de", "para", "pra",
        "daqui", "em", "depois", "dentro", "as", "ao", "hoje", "amanha", "toda", "todo",
        "todos", "todas", "semana", "dia", "meio", "noite", "meia", "coloca", "poe",
    )
    for w in fillers:
        resto = re.sub(rf"\b{w}\b", " ", resto)
    # Verbos de comando (por radical) e artigos — não são "mensagem".
    resto = re.sub(
        r"\b(?:adicion\w*|cri[ae]\w*|criar|coloc\w*|marc\w*|agend\w*|program\w*|"
        r"bot[ae]\w*|quero|preciso|favor|por|um|uma|uns|umas)\b",
        " ", resto,
    )
    resto = re.sub(r"\d+", " ", resto)
    for u in agenda._UNIDADES:
        resto = re.sub(rf"\b{u}\b", " ", resto)
    for d in agenda._DIAS_SEMANA:
        resto = re.sub(rf"\b{re.escape(d)}\b", " ", resto)
    return bool(textutils.palavras_chave(resto))


def _del(orig: str, low: str, pattern: re.Pattern) -> tuple[str, str]:
    """Remove de `orig` (e do `low` paralelo) TODAS as ocorrências de `pattern`.

    `low` é `orig.lower()` — mesmo comprimento —, então os índices casam e podemos
    fatiar o original (com acentos preservados) usando as posições achadas no low."""
    out_o, out_l, last = [], [], 0
    for m in pattern.finditer(low):
        out_o.append(orig[last:m.start()])
        out_l.append(low[last:m.start()])
        last = m.end()
    out_o.append(orig[last:])
    out_l.append(low[last:])
    return "".join(out_o), "".join(out_l)
