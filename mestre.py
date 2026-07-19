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
_GATILHO_STATUS = ("diagnostico", "status", "autoteste", "health", "checagem",
                   "esta funcionando", "voce esta bem", "esta tudo ok", "esta tudo certo")
_GATILHO_AUDITORIA = ("o que voce fez", "que voce fez hoje", "trilha de auditoria",
                      "suas acoes", "historico de acoes", "o que aconteceu hoje")
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


def modo_confidencial(comando: str) -> Optional[bool]:
    """Detecta o comando de LIGAR/DESLIGAR o modo confidencial (#5). Puro/testável.

    Devolve True (ligar), False (desligar) ou None (não é comando de modo). Precisa
    ser tratado no fluxo-mestre, não numa ferramenta, porque mexe no estado da SESSÃO
    (SessionMemory.confidencial), a que as ferramentas (só ctx) não têm acesso."""
    if not comando:
        return None
    n = textutils.normaliza(comando)
    ligar = ("modo sigiloso", "modo confidencial", "modo privado", "modo secreto",
             "modo anonimo", "off the record", "fica em sigilo", "ativar sigilo",
             "modo incognito", "nao registre isso", "nao salve isso")
    desligar = ("modo normal", "modo publico", "sair do sigilo", "fim do sigilo",
                "desligar sigilo", "desliga o sigilo", "pode registrar", "voltar ao normal",
                "sair do modo sigiloso", "encerrar sigilo")
    if any(g in n for g in desligar):
        return False
    if any(g in n for g in ligar):
        return True
    return None


# DESFAZER (#8): gatilhos de "reverte a última ação". Mantidos ESPECÍFICOS (frases,
# não o "cancela" cru) para não colidir com "cancela o lembrete 3" (que é uma ação
# regular do parse_rapido). Já normalizados (sem acento): "desfaça"->"desfaca".
_GATILHO_DESFAZER = (
    "desfaz", "desfazer", "desfaca", "desfez", "desfeito",
    "volta atras", "voltar atras", "anula", "anular",
    "cancela isso", "cancelar isso", "cancela a ultima", "cancela a acao",
    "esquece isso", "esquece o que eu disse", "esquece o ultimo",
)


def comando_desfazer(comando: str) -> bool:
    """True se a mensagem pede para DESFAZER a última ação (#8). Puro/testável.

    Tratado no fluxo-mestre (não numa ferramenta) porque a ação a reverter vive no
    estado da SESSÃO (SessionMemory.ultima_reversivel), a que as ferramentas (só ctx)
    não têm acesso — mesma razão do modo confidencial."""
    if not comando:
        return False
    n = textutils.normaliza(comando)
    return any(g in n for g in _GATILHO_DESFAZER)


# DESCOBRIDOR DE CONEXÕES (#22/G8): frases que pedem as PONTES do vault ("o que liga meus
# temas?"). É meta-comando de INSIGHT, não uma ação de agente — tratado no fluxo-mestre
# (lê a malha em ctx), como o desfazer lê o estado da sessão.
_GATILHO_CONEXAO = (
    "alguma conexao nova", "conexoes novas", "conexao nova", "alguma conexao",
    "que conexoes", "quais conexoes", "conexoes entre", "descobriu conexao",
    "achou conexao", "liga meus temas", "conectando", "pontes",
)


def comando_conexoes(comando: str) -> bool:
    """True se a mensagem pede as PONTES do vault (G8, descobridor de conexões). Puro."""
    if not comando:
        return False
    n = textutils.normaliza(comando)
    return any(g in n for g in _GATILHO_CONEXAO)


# DETECTOR DE CONTRADIÇÃO (#24): pede o RELATÓRIO das contradições que o idle achou.
# Insight sob demanda (lê a tabela), como o descobridor de conexões.
_GATILHO_CONTRADICAO = (
    "alguma contradicao", "contradicoes", "contradicao", "tem contradicao",
    "notas que se contradizem", "algo se contradiz", "conflito nas notas",
    "notas conflitantes", "achou contradicao",
)


def comando_contradicoes(comando: str) -> bool:
    """True se a mensagem pede o relatório de contradições do vault (#24). Puro."""
    if not comando:
        return False
    n = textutils.normaliza(comando)
    return any(g in n for g in _GATILHO_CONTRADICAO)


# SRS (#43): repetição espaçada, tudo meta-comando (mexe no estado da sessão / banco de
# cards). MARCAR ("revisa isso") vem ANTES de INICIAR ("revisão") no fluxo, porque a frase
# de marcar CONTÉM "revisa". Os SUB-comandos (mostra/acertei/errei/parar) só valem com uma
# revisão em andamento — fora dela, "acertei" é palavra comum e não deve ativar nada.
_GATILHO_SRS_MARCAR = (
    "revisa isso", "revise isso", "revisar isso", "marca pra revisar", "marca para revisar",
    "guarda pra revisao", "guarda para revisao", "adiciona a revisao", "coloca na revisao",
    "quero revisar isso", "guarda isso pra revisar",
)
_GATILHO_SRS_INICIAR = (
    "revisao", "revisar", "hora de revisar", "me faz revisar", "vamos revisar",
    "quero revisar", "bora revisar", "comecar a revisao", "iniciar revisao",
)
_GATILHO_SRS_MOSTRAR = (
    "mostra", "mostrar", "revela", "revelar", "qual a resposta", "qual e a resposta",
    "nao sei", "nao lembro", "me mostra", "abre",
)
_GATILHO_SRS_ACERTEI = ("acertei", "sabia", "lembrava", "acertou", "certo", "lembrei", "acerto")
_GATILHO_SRS_ERREI = ("errei", "nao sabia", "nao lembrava", "esqueci", "errou", "errado", "nao lembrei")
_GATILHO_SRS_PARAR = (
    "para a revisao", "parar a revisao", "parar revisao", "chega de revisao",
    "encerra a revisao", "encerrar revisao", "sair da revisao", "chega por hoje",
)


def _casa(comando: str, gatilhos) -> bool:
    if not comando:
        return False
    n = textutils.normaliza(comando)
    return any(g in n for g in gatilhos)


def comando_srs_marcar(comando: str) -> bool:
    return _casa(comando, _GATILHO_SRS_MARCAR)


def comando_srs_iniciar(comando: str) -> bool:
    return _casa(comando, _GATILHO_SRS_INICIAR)


def comando_srs_mostrar(comando: str) -> bool:
    return _casa(comando, _GATILHO_SRS_MOSTRAR)


def comando_srs_acertei(comando: str) -> bool:
    return _casa(comando, _GATILHO_SRS_ACERTEI)


def comando_srs_errei(comando: str) -> bool:
    return _casa(comando, _GATILHO_SRS_ERREI)


def comando_srs_parar(comando: str) -> bool:
    return _casa(comando, _GATILHO_SRS_PARAR)


def comando_srs_sub(comando: str) -> bool:
    """É um sub-comando de uma revisão EM ANDAMENTO (mostra/acertei/errei/parar)? Só deve
    ser consultado quando há revisão ativa — fora dela, essas palavras são comuns."""
    return (
        comando_srs_mostrar(comando) or comando_srs_acertei(comando)
        or comando_srs_errei(comando) or comando_srs_parar(comando)
    )


# LEITOR DE AGENDA (#40): "o que tenho hoje" / "minha agenda". Só LEITURA (não muta nada).
_GATILHO_AGENDA = (
    "o que tenho hoje", "o que tenho pra hoje", "o que tenho para hoje", "minha agenda",
    "meus compromissos", "compromissos de hoje", "agenda de hoje", "agenda hoje",
    "tenho algum compromisso", "reunioes de hoje", "o que tem na agenda", "meus horarios de hoje",
)


def comando_agenda(comando: str) -> bool:
    """True se o usuário pergunta os compromissos de hoje (#40, leitor .ics local). Puro."""
    return _casa(comando, _GATILHO_AGENDA)


# GATILHOS CONDICIONAIS INTERNOS (#11): "quando eu adicionar X na lista, <ação>". Reage a
# um EVENTO do app (v1: só lista_add) executando uma ação. Distinto do watcher (que reage à
# WEB e só avisa). Só ativa quando a condição casa um evento CONHECIDO — senão cai no fluxo
# normal (um "quando o dólar subir, me avise" segue pro roteador/watcher, não é recusado aqui).
def separar_gatilho(comando: str) -> Optional[tuple]:
    """'quando <condição>, <ação>' -> (condicao, acao). Divide na 1ª vírgula ou 'então'/
    'faça'. None se não começa por 'quando' ou não tem separador. Puro/testável."""
    if not comando or not textutils.normaliza(comando).startswith("quando"):
        return None
    corpo = re.sub(r"(?i)^\s*quando\s+", "", comando.strip())
    m = re.search(r",|\s+ent[aã]o\s+|\s+fa[cç]a\s+", corpo, re.IGNORECASE)
    if not m:
        return None
    condicao, acao = corpo[: m.start()].strip(), corpo[m.end():].strip()
    if not condicao or not acao:
        return None
    return condicao, acao


def evento_da_condicao(condicao: str, agora: datetime) -> Optional[tuple]:
    """Mapeia a condição para (evento, filtro). v1: só 'eu adicionar X na lista' ->
    ('lista_add', X). Reusa o parse_rapido para extrair item/lista. Puro."""
    c = re.sub(r"(?i)^\s*eu\s+", "", condicao.strip())
    decs = parse_rapido(c, agora)
    if decs and len(decs) == 1 and decs[0].tool == "adicionar_item":
        return "lista_add", str(decs[0].args.get("item", "")).strip()
    return None


def parse_gatilho(comando: str, agora: datetime) -> Optional[tuple]:
    """(evento, filtro, acao_texto) se é um gatilho RECONHECIDO, senão None (fluxo normal).
    A ação em si é parseada pelo Agent (pode precisar do roteador LLM). Puro."""
    sep = separar_gatilho(comando)
    if sep is None:
        return None
    ev = evento_da_condicao(sep[0], agora)
    if ev is None:
        return None
    return ev[0], ev[1], sep[1]


_GATILHO_LISTAR_GAT = (
    "quais gatilhos", "meus gatilhos", "listar gatilhos", "lista de gatilhos",
    "que gatilhos", "meus automatismos", "quais automatismos",
)


def comando_gatilhos_listar(comando: str) -> bool:
    return _casa(comando, _GATILHO_LISTAR_GAT)


def comando_gatilho_remover(comando: str) -> Optional[int]:
    """ID a remover em 'remove/apaga o gatilho N', ou None. Puro."""
    n = textutils.normaliza(comando or "")
    if "gatilho" not in n:
        return None
    if not any(v in n for v in ("remov", "apag", "exclu", "delet", "tira", "cancel")):
        return None
    m = re.search(r"\d+", n)
    return int(m.group()) if m else None


# REVISÃO DIÁRIA (#21): fechamento do dia (o que fiz + inbox + agenda/lembretes de amanhã).
# Gatilhos DISTINTOS do SRS ("revisão"/"revisar") de propósito — e por isso este é checado
# ANTES do SRS no fluxo, senão "revisão do dia" cairia na revisão de cards.
_GATILHO_REVISAO_DIA = (
    "resumo do dia", "revisao do dia", "fechamento do dia", "fechar o dia", "fecha o dia",
    "como foi meu dia", "resumo de hoje", "balanco do dia", "revisao diaria", "meu dia",
)


def comando_revisao_diaria(comando: str) -> bool:
    """True se o usuário pede o fechamento do dia (#21). Puro."""
    return _casa(comando, _GATILHO_REVISAO_DIA)


# DIÁRIO DE HÁBITOS (#37): marcar que cumpriu um hábito hoje. Formas CLARAS (verbo de
# conclusão ou "habito") para não capturar "marca reunião" à toa; exclui lista/lembrete.
def _limpa_habito(s: str) -> Optional[str]:
    nome = textutils.normaliza(s)
    while True:
        novo = re.sub(r"^(?:o|a|meu|minha|de)\s+", "", nome).strip()
        if novo == nome:
            break
        nome = novo
    nome = re.sub(r"\s+hoje$", "", nome).strip()
    return nome or None


def parse_habito_marcar(comando: str) -> Optional[str]:
    """Nome do hábito cumprido, ou None. 'fiz X' / 'completei X' / 'marca que <X>' /
    '...habito [de] X'. Exclui comandos de lista/lembrete. Puro/testável."""
    if not comando:
        return None
    n = textutils.normaliza(comando)
    if "lista" in n or "lembret" in n:
        return None
    m = re.search(r"(?i)h[aá]bito(?:\s+de)?\s+(.+)", comando)
    if m and any(v in n for v in ("marc", "registr", "fiz", "meu", "cumpri")):
        return _limpa_habito(m.group(1))
    m = re.match(r"(?i)\s*(?:fiz|completei|conclui|terminei|pratiquei|cumpri)\s+(.+)", comando)
    if m:
        return _limpa_habito(m.group(1))
    m = re.match(r"(?i)\s*marca\w*\s+que\s+(?:eu\s+)?(.+)", comando)
    if m:
        return _limpa_habito(m.group(1))
    return None


_GATILHO_HABITOS_LISTAR = (
    "meus habitos", "quais habitos", "como estao meus habitos", "como vao meus habitos",
    "minhas sequencias", "meus streaks", "resumo dos habitos", "status dos habitos",
)


def comando_habitos_listar(comando: str) -> bool:
    return _casa(comando, _GATILHO_HABITOS_LISTAR)


# TUTOR SOCRÁTICO (#44): liga/desliga o modo de sessão. Meta-comando (mexe no estado da
# sessão, como o modo confidencial). Gatilhos de OFF próprios (não usa "modo normal", que
# é do confidencial) para os dois modos serem independentes.
def comando_tutor(comando: str) -> Optional[bool]:
    """True (ligar), False (desligar) ou None (não é comando de tutor). Puro/testável."""
    if not comando:
        return None
    n = textutils.normaliza(comando)
    off = ("sai do tutor", "sair do tutor", "chega de tutor", "para de me ensinar",
           "fim do tutor", "desliga o tutor", "desligar tutor", "encerra o tutor")
    on = ("modo tutor", "modo socratico", "seja meu tutor", "vira tutor", "vira meu tutor",
          "ativa o tutor", "ativar tutor", "quero um tutor", "me ensine socraticamente")
    if any(g in n for g in off):
        return False
    if any(g in n for g in on):
        return True
    return None


def comando_economico(comando: str) -> Optional[bool]:
    """Liga/desliga o Modo Econômico (#30): True/False/None. Puro/testável. Como o
    confidencial/tutor, mexe no estado da SESSÃO — tratado no fluxo-mestre, não numa
    ferramenta. Desligar é checado ANTES de ligar (ambos contêm 'modo economico')."""
    if not comando:
        return None
    n = textutils.normaliza(comando)
    off = ("modo economico off", "sai do modo economico", "sair do economico",
           "desliga o economico", "desligar economico", "fim do modo economico",
           "modo normal de busca", "volta a usar a web", "pode usar a web")
    on = ("modo economico", "modo economia", "economiza web", "economizar web",
          "nao use a web", "sem web", "so local", "responde do que voce ja sabe")
    if any(g in n for g in off):
        return False
    if any(g in n for g in on):
        return True
    return None


# ROTINAS COMPOSTAS (#10): macro NOMEADA de comandos. Criar tem ':'/'=' separando nome e
# corpo; rodar é só "rotina <nome>" (expande para o corpo e o parse_composto executa).
def parse_rotina_criar(comando: str) -> Optional[tuple]:
    """'(cria/salva a) rotina <nome>: <comando>' ou 'rotina <nome> = <comando>' ->
    (nome_normalizado, comando). Puro/testável."""
    if not comando or "rotina" not in textutils.normaliza(comando):
        return None
    m = re.search(r"(?i)rotina\s+(?:de\s+|chamada\s+)?(.+?)\s*[:=]\s*(.+)", comando)
    if not m:
        return None
    nome, corpo = textutils.normaliza(m.group(1)).strip(), m.group(2).strip()
    return (nome, corpo) if nome and corpo else None


def parse_rotina_rodar(comando: str) -> Optional[str]:
    """Nome da rotina a EXECUTAR em 'rotina <nome>' / 'executa a rotina <nome>'. None se é
    criação (tem ':'/'=') ou listar/remover. Puro."""
    if not comando:
        return None
    n = textutils.normaliza(comando)
    if "rotina" not in n or ":" in comando or "=" in comando:
        return None
    if any(v in n for v in ("remov", "apag", "exclu", "delet", "quais", "quantas", "liste", "minhas rotinas")):
        return None
    m = re.search(r"(?i)rotina\s+(?:de\s+)?(.+)", comando)
    if not m:
        return None
    nome = textutils.normaliza(m.group(1)).strip()
    return nome or None


_GATILHO_ROTINAS_LISTAR = (
    "quais rotinas", "minhas rotinas", "listar rotinas", "que rotinas", "lista de rotinas",
    "quantas rotinas",
)


def comando_rotinas_listar(comando: str) -> bool:
    return _casa(comando, _GATILHO_ROTINAS_LISTAR)


def comando_rotina_remover(comando: str) -> Optional[str]:
    """Nome da rotina a remover em 'remove a rotina <nome>', ou None. Puro."""
    n = textutils.normaliza(comando or "")
    if "rotina" not in n or not any(v in n for v in ("remov", "apag", "exclu", "delet")):
        return None
    m = re.search(r"(?i)rotina\s+(?:de\s+)?(.+)", comando)
    return textutils.normaliza(m.group(1)).strip() if m and m.group(1).strip() else None


# POMODORO (#19): ciclo foco/pausa. PARAR vem antes de INICIAR (a frase de parar contém
# "pomodoro"). Gatilhos de parar próprios (sem "cancela"/"para" cru p/ não colidir).
_GATILHO_POMODORO_PARAR = (
    "para o pomodoro", "parar o pomodoro", "para pomodoro", "encerra o pomodoro",
    "encerrar pomodoro", "cancela o pomodoro", "chega de pomodoro", "para o foco",
)
_GATILHO_POMODORO_INICIAR = (
    "pomodoro", "modo foco", "temporizador de foco", "sessao de foco", "ciclo de foco",
)


def comando_pomodoro_parar(comando: str) -> bool:
    return _casa(comando, _GATILHO_POMODORO_PARAR)


def comando_pomodoro_iniciar(comando: str) -> bool:
    return _casa(comando, _GATILHO_POMODORO_INICIAR)


# AJUDA (/help falável): "mestre, ajuda" / "mestre /ajuda" / "o que você pode fazer".
# Descoberta de comandos — checada TARDE no fluxo (depois dos comandos específicos), pois
# "ajuda" é gatilho largo; um "me ajuda a somar" cai aqui, e tudo bem (lista as capacidades).
_GATILHO_AJUDA = (
    "ajuda", "/ajuda", "me ajuda", "o que voce pode fazer", "o que voce faz",
    "o que voce sabe fazer", "o que voce consegue fazer", "quais comandos",
    "seus comandos", "lista de comandos", "o que posso pedir", "help", "socorro",
    "o que da pra fazer",
)


def comando_ajuda(comando: str) -> bool:
    """True se o usuário pede a lista de capacidades (/help falável). Puro."""
    return _casa(comando, _GATILHO_AJUDA)


def reverter(executadas: List[tuple]) -> Optional[List[tools.Decisao]]:
    """Dadas as ações que rodaram — pares (Decisao, resultado_str) —, devolve as ações
    que as DESFAZEM, em ordem INVERSA à execução, ou None se nada é reversível.

    Puro/testável. Só reverte mutações com um inverso DETERMINÍSTICO e uma fonte
    confiável do que reverter: add/remove de lista (o inverso é a outra ferramenta) e
    lembrete (o inverso é cancelar o #id que a própria ferramenta reportou no resultado).
    O resultado é inspecionado para não "reverter" uma ação que falhou (ex.: remover um
    item que não existia). Captura/nota ficam de fora por ora (sem inverso registrado)."""
    revs: List[tools.Decisao] = []
    for dec, res in executadas:
        r = _reverter_uma(dec, res or "")
        if r is not None:
            revs.append(r)
    if not revs:
        return None
    revs.reverse()   # desfaz na ordem inversa da execução
    return revs


def _reverter_uma(dec: tools.Decisao, resultado: str) -> Optional[tools.Decisao]:
    """Inverso de UMA ação, ou None se não for reversível/não tiver sido efetivada."""
    args = dec.args or {}
    baixa = resultado.lower()
    if dec.tool == "adicionar_item" and baixa.startswith("adicionei"):
        item = str(args.get("item", "")).strip()
        if item:
            return tools.Decisao("remover_item", {"lista": args.get("lista") or "compras", "item": item})
    elif dec.tool == "remover_item" and baixa.startswith("removi"):
        item = str(args.get("item", "")).strip()
        if item:
            return tools.Decisao("adicionar_item", {"lista": args.get("lista") or "compras", "item": item})
    elif dec.tool == "criar_lembrete":
        # A ferramenta reporta "lembrete #<id> criado ..." no sucesso; cancelar esse id
        # desfaz. Numa falha ("não entendi o horário") não há '#<id>' -> None.
        m = re.search(r"#(\d+)", resultado)
        if m:
            return tools.Decisao("cancelar_lembrete", {"id": m.group(1)})
    return None


# CORTA-E-CORRIGE (#9): marcadores EXPLÍCITOS de correção (exige o marcador — não
# basta um "era" solto — para não interceptar um add legítimo). Normalizados.
# Radicais: "corrig" (corrige/corrigir/corrigido) NÃO cobre o imperativo "corrija"
# (corri-J-a) nem "correção" — daí os três radicais.
_GATILHO_CORRIGIR = ("corrig", "corrij", "correca", "na verdade", "quis dizer",
                     "me enganei", "errei", "me equivoquei")
# Marcador/preposição que ANTECEDE o valor certo, em ordem de prioridade.
_CORR_TAIL = (
    re.compile(r"\b(?:para|pra|por)\s+(.+)$"),        # corrige PARA leite / troca POR leite
    re.compile(r"\bquis dizer\s+(.+)$"),              # quis dizer leite
    re.compile(r"\bna verdade\s+(.+)$"),              # na verdade (era) leite
    re.compile(r"\b(?:corri[gj]\w*|correca\w*)[\s:,\-]+(.+)$"),   # corrige:/corrija: leite
    re.compile(r"\bera\s+(.+)$"),                     # (me enganei,) era leite
)
# Afirmativos/artigos que sobram na frente do valor após o corte.
_CORR_LIMPA = re.compile(r"^(?:era|eh|e|seria|foi|para|pra|por|o|a|os|as|um|uma)\s+", re.I)


def tem_correcao(comando: str) -> bool:
    """True se a mensagem é um comando de CORREÇÃO (#9). Puro/testável."""
    if not comando:
        return False
    n = textutils.normaliza(comando)
    return any(g in n for g in _GATILHO_CORRIGIR)


def parse_correcao(comando: str) -> Optional[str]:
    """Extrai o VALOR CERTO de um comando de correção (#9), ou None. Puro/testável.

    Cobre "corrige para X", "troca por X", "na verdade era X", "quis dizer X" e o
    "corrige: era X, não Y" — o trecho negado (o que o usuário REJEITA, após "não") é
    descartado. Casa posições sobre a forma SEM ACENTO (comprimento preservado) e fatia
    o ORIGINAL, então o valor mantém os acentos ("pão")."""
    if not tem_correcao(comando):
        return None
    orig = comando.strip()
    baixa = orig.lower()
    ms = textutils.sem_acento(baixa)
    if len(ms) != len(baixa):
        ms = baixa   # desalinhou (char exótico) -> usa o acentuado
    for pat in _CORR_TAIL:
        m = pat.search(ms)
        if not m:
            continue
        bruto = orig[m.start(1):m.end(1)]
        # o que vem depois de "não" é o valor REJEITADO -> fora.
        bruto = re.split(r"\bn[ãa]o\b", bruto, maxsplit=1, flags=re.I)[0]
        val = _CORR_LIMPA.sub("", bruto.strip()).strip(" ,.;:!?")
        if val:
            return val
    return None


# COFRE DE CONFIRMAÇÃO (#25). Gatilhos SÓ consultados quando há algo pendente, então
# um "confirma"/"não" solto não afeta nada fora do fluxo de confirmação.
_GATILHO_CONFIRMAR = ("confirma", "confirmo", "confirmado", "pode confirmar", "pode fazer",
                      "pode ir", "isso mesmo", "sim pode", "manda ver", "positivo")
# Sem "cancela"/"para": colidiriam com "cancela o lembrete 5" (uma ação legítima que o
# usuário pode emitir enquanto uma confirmação está pendente).
_GATILHO_ABORTAR = ("nao", "melhor nao", "deixa", "deixa pra la", "esquece",
                    "nao precisa", "nao confirmo", "nega", "negativo")


def comando_confirmar(comando: str) -> bool:
    """True se o usuário CONFIRMOU a ação pendente (#25). Puro/testável."""
    if not comando:
        return False
    n = textutils.normaliza(comando)
    return any(g in n for g in _GATILHO_CONFIRMAR)


def comando_abortar(comando: str) -> bool:
    """True se o usuário ABORTOU explicitamente a ação pendente (#25). Puro/testável."""
    if not comando:
        return False
    n = textutils.normaliza(comando)
    return any(g in n for g in _GATILHO_ABORTAR)


# ATALHO DE INTENÇÃO FREQUENTE (#2): "atalho <nome>" nomeia o último comando.
_ATALHO_RE = re.compile(r"\batalho\b\s*(?:chamado|chamada|de|como|:)?\s*(.+)$", re.I)
_ATALHO_LIMPA = re.compile(r"^(?:chamado|chamada|de|como|o|a|um|uma)\s+", re.I)


def parse_atalho(comando: str) -> Optional[str]:
    """Se o comando CRIA um atalho (#2), devolve o NOME (apelido); senão None.

    'mestre, atalho compras' -> 'compras'. Puro/testável. O nome é curto (≤3 palavras)
    — é um apelido, não uma frase; algo maior provavelmente não é um comando de atalho."""
    if not comando:
        return None
    if "atalho" not in textutils.normaliza(comando):
        return None
    m = _ATALHO_RE.search(comando.strip())
    if not m:
        return None
    nome = _ATALHO_LIMPA.sub("", m.group(1).strip()).strip(" .,:;\"'").lower()
    return nome if nome and len(nome.split()) <= 3 else None


def descrever_acao(dec: tools.Decisao) -> str:
    """Frase curta do que a ação FARÁ, para pedir confirmação (#25). Puro/testável."""
    args = dec.args or {}
    if dec.tool == "cancelar_lembrete":
        num = re.search(r"\d+", str(args.get("id", "")))
        return f"cancelar o lembrete {num.group()}" if num else "cancelar esse lembrete"
    if dec.tool == "remover_item":
        return f"remover '{args.get('item', '')}' da lista de {args.get('lista', 'compras')}"
    return f"executar {dec.tool}"


def refazer_com(forward: List[tools.Decisao], certo: str) -> Optional[List[tools.Decisao]]:
    """Refaz a última mutação de VALOR com o valor corrigido (#9). Puro/testável.

    Hoje cobre `adicionar_item` (o caso "era leite, não pão"): clona a ação original
    trocando só o item. Outras mutações (lembrete/captura) não são corrigíveis por ora."""
    certo = (certo or "").strip()
    if not certo:
        return None
    for dec in (forward or []):
        if dec.tool == "adicionar_item":
            args = dict(dec.args or {})
            args["item"] = certo
            return [tools.Decisao("adicionar_item", args)]
    return None


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

    # HEALTH-CHECK ("diagnóstico", "você está funcionando?"): autoteste dos serviços.
    if any(g in norm for g in _GATILHO_STATUS):
        return [tools.Decisao("status_sistema", {})]

    # TRILHA DE AUDITORIA ("o que você fez hoje?"): lista as ações do dia.
    if any(g in norm for g in _GATILHO_AUDITORIA):
        return [tools.Decisao("auditoria_hoje", {})]

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


# ENCADEAMENTO FALADO (#12): conectores que PODEM separar duas ações ("... e ...",
# "... depois ...", "; ..."). O corte só acontece se o lado DIREITO começar uma nova
# ação (senão o "e" é interno de lista: "leite, farinha e ovos" fica inteiro). "e depois"
# / "e também" antes do "e" cru (alternância é leftmost-first).
_CONECTOR_RE = re.compile(
    r"\s+(?:e\s+depois|e\s+tambem|e|depois|tambem)\s+|\s*;\s*", re.I
)
# Radicais que MARCAM o começo de uma nova ação reconhecível pelo parse_rapido.
_ACAO_START_RE = re.compile(
    r"^(?:me\s+lembr|lembr|adicion|acrescent|coloc|poe|poem|bot[ae]|inclu|remov|tir[ae]|"
    r"tirar|apag|exclu|delet|retir|cri[ae]|criar|marc|agend|program|anot|captur|cancel|"
    r"avis|alarme|despertador|timer|cronometro|temporizador|lembrete)\w*",
    re.I,
)


def dividir_comandos(comando: str) -> List[str]:
    """Fatia um comando composto nas fronteiras entre AÇÕES (#12). Puro/testável.

    Corta num conector só quando o texto seguinte começa uma nova ação — assim o "e"
    interno de uma lista ("leite, farinha e ovos") NÃO vira fronteira. Casa posições
    sobre a forma sem acento (comprimento preservado) e fatia o ORIGINAL."""
    orig = comando.strip()
    if not orig:
        return []
    baixa = orig.lower()
    ms = textutils.sem_acento(baixa)
    if len(ms) != len(baixa):
        ms = baixa
    cortes = []
    for m in _CONECTOR_RE.finditer(ms):
        if _ACAO_START_RE.match(ms[m.end():]):
            cortes.append((m.start(), m.end()))
    if not cortes:
        return [orig]
    segmentos, last = [], 0
    for ini, fim in cortes:
        segmentos.append(orig[last:ini].strip())
        last = fim
    segmentos.append(orig[last:].strip())
    return [s for s in segmentos if s]


def parse_composto(comando: str, agora: datetime) -> Optional[List[tools.Decisao]]:
    """Encadeamento falado (#12): resolve "faz X e faz Y" como VÁRIAS ações.

    Divide nos pontos de fronteira e roda `parse_rapido` em cada parte. Se QUALQUER
    parte não resolver deterministicamente, devolve None (defere o TODO ao LLM — nunca
    executa metade). Comando sem encadeamento cai direto no parse_rapido (sem regressão)."""
    if not comando or not comando.strip():
        return None
    partes = dividir_comandos(comando)
    if len(partes) <= 1:
        return parse_rapido(comando, agora)
    todas: List[tools.Decisao] = []
    for p in partes:
        acoes = parse_rapido(p, agora)
        if not acoes:
            return None   # uma parte falhou -> defere o todo (não faz só metade)
        todas.extend(acoes)
    return todas


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
