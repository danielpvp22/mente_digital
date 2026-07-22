"""
Interpretação da pergunta — o que acontece ANTES de qualquer busca.

QueryOptimizer (extrator de query, resolve pronomes cruzados via LLM só quando
precisa) + as heurísticas PURAS que decidem como tratar a pergunta: ela referencia
o turno anterior? é um pedido de síntese ("o que eu sei sobre X")? cita uma
expressão? a lacuna vale uma pesquisa proativa? Extraído do agent.py na
modularização — nada aqui conhece Agent, WebSocket ou o pipeline.
"""
from __future__ import annotations

import re
from typing import Deque, Tuple

import prompts
import textutils
import tools
from config import settings
from llm import LlamaManager
from telemetry import telemetry

STOP_WORDS = {"não", "nao", "sim", "nada", "tudo", "pode falar", "continue", "isso", "nenhum"}

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


# AFIRMAÇÃO vs pergunta/pedido (conserto do caso "Falcão", teste real 2026-07-21):
# "O codinome do meu drone é Falcão." foi tratado como PERGUNTA, escalou pra web,
# achou o drone homônimo da Avibras e o fato ALHEIO contaminou a RAM e virou átomo
# permanente. Frase declarativa sem âncora local é REGISTRO, não lacuna a pesquisar.
# Heurística léxica pura: sem '?', e sem começar com interrogativo/verbo de pedido.
_ABERTURAS_NAO_DECLARATIVAS = (
    # interrogativos (pergunta sem '?': "qual o codinome...", "voce sabe onde...")
    "o que", "que ", "qual", "quais", "quem", "quando", "onde", "aonde", "como",
    "por que", "porque", "pra que", "para que", "quanto", "quanta", "quantos",
    "quantas", "cade", "sera que", "existe", "existem", "tem como", "da pra",
    "pode", "podem", "devo", "voce", "vc ", "sabe ", "sabes ",
    # pedidos/comandos (imperativo: o usuário QUER uma ação, não está afirmando).
    # "me " cobre a família inteira ("me fala/fale/diga/mostra/lembra/avisa...") —
    # a 1ª versão listava conjugações e "me FALE sobre..." escapou (pego pelo teste
    # do filler). Perde-se o raro "me formei ontem" declarativo, que então cai no
    # caminho antigo (web) — o erro conservador.
    "me ", "explica", "explique", "detalha",
    "detalhe", "mostra", "mostre", "lista", "liste", "fala", "fale", "diz",
    "diga", "resume", "resuma", "compara", "compare", "pesquisa", "pesquise",
    "busca", "busque", "procura", "procure", "calcula", "calcule", "salva",
    "salve", "anota", "anote", "cria", "crie", "adiciona", "adicione", "remove",
    "remova", "cancela", "cancele", "lembra", "lembre", "agenda", "agende",
    "abre", "abra", "toca", "toque", "manda", "mande", "envia", "envie",
    "define", "defina", "verifica", "verifique",
)


def e_declarativa(texto: str) -> bool:
    """A mensagem AFIRMA um fato (não pergunta nem pede ação)? Puro/testável."""
    if "?" in texto:
        return False
    n = textutils.normaliza(texto).strip()
    if not n:
        return False
    return not any(
        n == a.strip() or n.startswith(a if a.endswith(" ") else a + " ")
        for a in _ABERTURAS_NAO_DECLARATIVAS
    )


# BACKCHANNEL — acuse-recebido/preenchimento: fala CURTA que não é pergunta, comando
# nem fato. Medido no teste real 2507 (voz): "ok"/"aham"/"tchau"/"valeu" caíam no
# registro declarativo ("Entendido, registrei") virando átomo de memória, e ativavam o
# pipeline à toa. Aqui só a fala que É *exatamente* um desses (normalizada) — nunca uma
# frase que apenas COMEÇA com "ok" ("ok, agenda a reunião" segue sendo comando).
_BACKCHANNEL = {
    "aham", "uhum", "uhm", "hmm", "hm", "ah", "aha", "aa", "ha",
    "ok", "okay", "ta", "ta bom", "ta bem", "ta certo", "certo", "sei", "sei la",
    "entendi", "entendido", "blz", "beleza", "valeu", "vlw",
    "tchau", "tchau tchau", "ate logo", "ate mais", "falou",
    "isso", "isso ai", "isso mesmo", "pois e", "opa", "eita", "nossa", "uau",
}


def e_backchannel(texto: str) -> bool:
    """True se a fala é SÓ um acuse-recebido/preenchimento (ignorar). Puro/testável."""
    n = textutils.normaliza(texto).strip(" .,!?…-")
    return n in _BACKCHANNEL


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

    def pagaria_llm(self, pergunta: str, historico: Deque[Tuple[str, str]]) -> bool:
        """O optimize() abaixo vai chamar o LLM para ESTA pergunta? Espelho PURO do
        branching, sem efeito colateral — existe para a fase (b) (consultoria TTFT #9):
        o Agent só dispara a recuperação vetorial especulativa quando há de fato um
        decode do extrator para esconder. Se este espelho e o optimize divergirem um
        dia, o custo é uma especulação inútil (ou uma a menos) — nunca resposta errada,
        porque o search() usa o resultado especulado apenas quando ele existe."""
        limpa = pergunta.lower().strip().replace(".", "").replace(",", "")
        if limpa in STOP_WORDS or len(limpa) < 4:
            return False                        # caminho de continuação: sem LLM
        if not (historico and referencia_contexto(pergunta)):
            # contexto="NENHUM": com o gate ligado o LLM é poupado; desligado, roda.
            return not settings.optimizer_gate
        return True

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

        # GATE LÉXICO (painel 2026-07, fase a): pergunta AUTO-CONTIDA não paga o LLM.
        # A busca vetorial embedda a pergunta CRUA (texto_busca — ver _texto_busca);
        # `termos` só alimenta o aterramento léxico, o _ram_relevante e a query web —
        # e para esses limpar_query (que JÁ era o fallback abaixo) basta. Remove uma
        # decodificação inteira (~0,3-0,9s) do TTFT de quase toda pergunta. Quem
        # referencia o turno anterior segue no LLM (resolver pronome exige contexto).
        # Botão MENTE_OPTIMIZER_GATE; deriva monitorável pelo [LOCAL] relevante=.
        if settings.optimizer_gate and contexto == "NENHUM":
            termos = textutils.limpar_query(pergunta) or limpa
            telemetry.track("EXTRATOR", f"Gate léxico (sem LLM): [{termos}]")
            return termos

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
