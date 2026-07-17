"""
Prompts centralizados. Todos seguem a regra de conduta do projeto: comandos
diretivos, curtos e proibindo a IA de ser educada/prolixa.
"""
from __future__ import annotations

# --- Extrator de query (resolve pronomes cruzados) ---------------------------
SYS_EXTRATOR = (
    "Você é um gerador de queries de busca. "
    "Retorne APENAS a query final, sem aspas e sem explicações."
)


def prompt_extrator(contexto_conversa: str, pergunta: str) -> str:
    return f"""[HISTÓRICO RECENTE]
{contexto_conversa}

[TAREFA]
Nova frase do usuário: '{pergunta}'
Reescreva a frase do usuário como uma pesquisa do Google (máximo 5 palavras).
REGRA CRÍTICA: Se o usuário usou pronomes (ele, esse, disso) ou apenas continuou o assunto, SUBSTITUA o pronome pelo NOME DO ASSUNTO EXATO que está no histórico recente.

QUERY DE BUSCA:"""


# --- HyDE Zettelkasten: atomiza a PERGUNTA no formato da base (busca vetorial) -
# A base é Zettelkasten ATÔMICA. Para o embedding da pergunta cair no MESMO espaço
# das notas, geramos uma "sonda": uma nota atômica hipotética (1 ideia) que conteria
# a resposta. NÃO precisa estar correta — é só isca de embedding para casar com o
# átomo real. É o mesmo formato usado na ingestão (prompt_sintese) → match fiel.
SYS_HYDE = (
    "Você escreve UMA nota atômica Zettelkasten (1 ideia, 1-2 frases afirmativas) que "
    "conteria a resposta à pergunta. Use os termos-chave exatos do assunto. "
    "Sem saudação, sem 'não sei', sem meta, sem título."
)


def prompt_hyde(pergunta: str) -> str:
    return (
        f"Pergunta: '{pergunta}'\n"
        "Escreva a nota atômica (1-2 frases) que conteria a resposta, "
        "usando os conceitos-chave. Só o texto do átomo."
    )


# O filler que mascara a latência da web agora é um TEMPLATE específico em
# agent.Agent._msg_web ("procurando X na web…") — sem chamada extra ao LLM, então
# não pesa no TTFA e diz de fato o que está sendo feito.


# --- Resposta principal (anti-alucinação, brutalmente concisa) ---------------
SYS_RESPOSTA = """Você é um Engenheiro de Dados Sênior.
REGRA 1: Baseie-se APENAS nos dados fornecidos. Se não estiver lá, diga 'Não tenho informações suficientes'. NUNCA invente.
REGRA 2: Seja BRUTALMENTE CONCISO. Resuma a resposta em no máximo 3 ou 4 frases curtas e diretas.
REGRA 3: Vá direto ao ponto, sem introduções polidas."""


# System da resposta WEB — SEPARADO do SYS_RESPOSTA (local). O local é conservador
# ("se não estiver lá, diga que não tem") para não alucinar sobre as notas. Mas na WEB
# isso rejeitava dado que ESTAVA no snippet (ex.: preço do bitcoin cravado na fonte,
# e mesmo assim respondia o sentinela). Aqui o modelo é instruído a USAR o dado; só
# solta o sentinela EXATO quando o dado realmente não contém a resposta (o guard
# anti-sentinela em _responder_web depende dessa frase idêntica).
SYS_RESPOSTA_WEB = """Você responde com base nos DADOS DA WEB fornecidos.
REGRA 1: Os dados da web são sua fonte — USE-OS. Extraia números, preços, datas e fatos que estiverem nos dados e responda direto. Não seja excessivamente cauteloso: se está nos dados, entregue.
REGRA 2: Só se os dados realmente NÃO contiverem a resposta, responda EXATAMENTE 'Não tenho informações suficientes'. NUNCA invente dado que não está nos textos.
REGRA 3: Seja conciso: 2 a 4 frases diretas, sem introdução polida."""


def prompt_resposta_web(dados_web: str, texto_usuario: str) -> str:
    return (
        f"Dados da Web:\n{dados_web}\n\n"
        f"Pergunta do usuário: '{texto_usuario}'.\n"
        "Responda direto usando os dados acima. Se um número/valor aparece nos dados, cite-o."
    )


def prompt_resposta_cache(contexto_combinado: str, texto_usuario: str) -> str:
    return (
        f"Contexto Local e RAM: {contexto_combinado}\n"
        f"Usuário: '{texto_usuario}'. "
        "Responda baseado ESTRITAMENTE nos dados."
    )


# --- Resposta por FUSÃO de átomos (cascata: 1 parágrafo por fonte) ------------
# A base Zettelkasten devolve DEZENAS de átomos (1 ideia cada). Aqui o LLM os
# INTEGRA num parágrafo coerente por fonte (RAM / Banco / Web). A REGRA 1 (sentinela)
# é mantida idêntica: sem base → 'Não tenho informações suficientes' → o pipeline
# escala para a próxima fonte sem "falar" o sentinela.
SYS_FUSAO = """Você é um Engenheiro de Dados Sênior.
REGRA 1: Baseie-se APENAS nos átomos fornecidos. Se a resposta não estiver neles, responda EXATAMENTE 'Não tenho informações suficientes'. NUNCA invente.
REGRA 2: A base é Zettelkasten — vários átomos de 1 ideia. INTEGRE os relevantes numa resposta coerente; não liste, não repita, ignore em silêncio os irrelevantes.
REGRA 3: UM parágrafo, direto, sem introdução polida."""


def prompt_resposta_atomos(atomos: str, texto_usuario: str) -> str:
    return (
        f"Átomos de conhecimento:\n{atomos}\n\n"
        f"Pergunta: '{texto_usuario}'.\n"
        "Escreva UM parágrafo integrando só os átomos que realmente tratam da pergunta. "
        "Se nenhum tratar, responda 'Não tenho informações suficientes'."
    )


# --- Roteador de ferramentas (function calling aditivo) ----------------------
SYS_ROUTER = (
    "Você é um ROTEADOR de intenção. Dada a mensagem do usuário, escolha UMA ferramenta. "
    "Responda SOMENTE com um objeto JSON válido em UMA linha, sem texto antes ou depois, "
    "sem markdown e sem explicação. Se for uma PERGUNTA de conhecimento a responder "
    'normalmente, use {"tool":"responder","args":{}}.'
)


def prompt_router(menu: str, texto_usuario: str, observacoes: str = "") -> str:
    obs = f"\n[RESULTADOS DE FERRAMENTAS JÁ EXECUTADAS]\n{observacoes}\n" if observacoes else ""
    return (
        f"Ferramentas disponíveis (escolha UMA):\n{menu}\n{obs}\n"
        f"Mensagem do usuário: {texto_usuario}\n"
        "Responda só o JSON:"
    )


# --- Resposta final após rodar ferramentas -----------------------------------
def prompt_resposta_ferramentas(resultados: str, texto_usuario: str) -> str:
    return (
        f"Resultados das ferramentas:\n{resultados}\n\n"
        f"Usuário: '{texto_usuario}'. "
        "Responda ao usuário de forma direta e natural, baseado nos resultados acima. "
        "Sem introduções polidas."
    )


# --- ETL / Idle (síntese em background → NOTAS ATÔMICAS) ---------------------
# Antes gerava um "documentão" multi-seção — o OPOSTO de Zettelkasten, e ainda por
# cima indexado, poluindo a base atômica. Agora destila em átomos no MESMO formato
# das notas do vault: cada '##' vira um chunk próprio na indexação (split por
# cabeçalho), então cada ideia fica recuperável isolada — como a query atomizada.
SYS_SINTESE = (
    "Você destila conhecimento em NOTAS ATÔMICAS Zettelkasten: cada nota é UMA ideia "
    "auto-contida. Português direto, sem enrolação e sem texto fora do formato pedido."
)


# A tag #conhecimento_novo marca o ESTADO de maturidade do átomo: recém-colhido da
# curiosidade (web/conversa), ainda NÃO "consolidado" pelo uso. Quando o átomo é de
# fato recuperado e usado numa resposta local, o pipeline REMOVE essa tag (promoção) —
# ver Agent._consolidar_fontes. O #zettelkasten_atomico permanece; só a maturidade muda.
TAG_ATOMO = "#zettelkasten_atomico"
TAG_NOVO = "#conhecimento_novo"


def prompt_sintese(tema: str, dados: str) -> str:
    return (
        f"A partir dos dados brutos sobre '{tema}', extraia as ideias-chave como NOTAS "
        "ATÔMICAS. Use EXATAMENTE este formato para cada nota, uma ideia por nota, sem repetir:\n\n"
        "## <título curto da ideia>\n"
        "<a ideia em 1-2 frases afirmativas>\n"
        "**Malha Neural:** [[Conceito relacionado]]\n"
        f"{TAG_ATOMO} {TAG_NOVO}\n\n"
        "Separe as notas por uma linha em branco. Nada de introdução ou conclusão.\n\n"
        f"DADOS BRUTOS:\n{dados}"
    )


# --- Síntese ATÔMICA da conversa (histórico de chat vira Zettelkasten) --------
# Antes o idle gerava um 'Resumo_Sessao' estruturado (H1 + bullets + conclusão) — o
# OPOSTO de Zettelkasten. Agora o histórico (texto OU voz) é destilado em NOTAS
# ATÔMICAS, no MESMO formato da base, então cada ideia trocada na conversa fica
# recuperável isolada — e nasce como #conhecimento_novo (consolida ao ser usada).
SYS_SINTESE_CONVERSA = (
    "Você destila uma CONVERSA em NOTAS ATÔMICAS Zettelkasten: cada nota é UMA ideia "
    "de conhecimento auto-contida que valha a pena reter. Ignore saudações, small talk "
    "e meta-conversa. Português direto, sem texto fora do formato pedido."
)


# --- Import de histórico externo (JSONs do Gemini) ---------------------------
# ESTÁGIO 1 — descobrir o ASSUNTO REAL do trecho.
# Por que não usar o nome do arquivo: uma conversa MUDA de tema no meio. Medido no
# acervo real, metade de "Otimizando-Munição-no-Tarkov-Arena.json" é sobre a economia
# de um jogo NFT (Bomb Crypto). Injetar "Tarkov" como assunto ali fazia o modelo
# escrever sobre um assunto que não era o do texto — e ele então abandonava a regra de
# auto-contenção, produzindo "## Pressão de Oferta / No fundo, a pressão de oferta é
# alta" (de quê?). O assunto tem que sair do TRECHO, não do nome do arquivo.
SYS_ASSUNTO = (
    "Você identifica o assunto concreto de um trecho de conversa. Responda APENAS com "
    "o assunto em até 8 palavras, nomeando as entidades próprias (produtos, jogos, "
    "tecnologias, lugares). Sem introdução, sem aspas, sem explicação."
)


def prompt_assunto(trecho: str) -> str:
    return (
        "Qual é o ASSUNTO concreto do trecho abaixo? Nomeie as entidades próprias "
        "(ex.: 'Economia do jogo NFT Bomb Crypto', 'Munição do Escape from Tarkov', "
        "'Preço de GPUs na AWS'). Máximo 8 palavras, só o assunto:\n\n"
        f"{trecho[:3000]}"
    )


# Diferente do prompt_sintese_conversa: aqui o TEMA da conversa é conhecido (vem do
# nome do arquivo) e é INJETADO, porque o defeito do import antigo foi exatamente a
# perda de contexto. Ele cortou as conversas em fragmentos 'Pt<N>' com títulos
# derivados mecanicamente, e produziu coisas como:
#   "# Economia necessária / Precisamos economizar pelo menos 166,2."  (166,2 de quê?)
#   "# Gemini"  numa nota cujo corpo fala de preço de instância g5.xlarge na AWS.
# Como split_markdown indexa o título junto com o corpo (strip_headers=False), esses
# títulos genéricos ENVENENAM a recuperação: medido, a nota do Tarkov acima entrou no
# contexto de uma pergunta sobre a economia da máquina de lavar louça, casada só pela
# palavra "economia". Daí a REGRA CRÍTICA de auto-contenção abaixo.
SYS_SINTESE_IMPORT = (
    "Você destila uma conversa em NOTAS ATÔMICAS Zettelkasten: cada nota é UMA ideia "
    "auto-contida, que faz sentido sozinha. Português direto, sem texto fora do formato."
)


def prompt_sintese_import(tema: str, trecho: str) -> str:
    return (
        f"A conversa abaixo é sobre: **{tema}**.\n\n"
        "Extraia dela as ideias de conhecimento DURÁVEL como NOTAS ATÔMICAS.\n\n"
        "REGRA CRÍTICA — AUTO-CONTENÇÃO: cada nota será lida ISOLADA, meses depois, sem "
        "esta conversa ao lado. O título E o corpo precisam dizer de que assunto se trata, "
        f"nomeando '{tema}' ou a entidade concreta envolvida. Títulos genéricos como "
        "'Economia necessária', 'Gemini' ou 'Configuração' são INÚTEIS e proibidos: "
        "escreva 'Economia de munição necessária no Tarkov'. Nunca use pronome sem "
        "antecedente ('ele', 'isso', 'esse valor', 'o script') — repita o nome.\n\n"
        "REGRA — RECUPERAÇÃO: escreva cada nota como a RESPOSTA a uma pergunta que "
        "alguém faria meses depois, usando as palavras que essa pessoa usaria ao "
        "perguntar. Números e nomes próprios devem vir acompanhados do que medem.\n\n"
        "REGRA — TÍTULOS DISTINTOS: cada nota trata de UMA ideia, então cada título "
        "tem que ser DIFERENTE dos outros e nomear a ideia ESPECÍFICA daquela nota. "
        f"NÃO repita '{tema}' como título de várias notas: ele é o contexto, não a "
        "ideia. Se duas notas teriam o mesmo título, ou são a mesma nota (escreva UMA) "
        "ou os títulos estão genéricos demais (diferencie-os).\n\n"
        "REGRA — UMA IDEIA POR NOTA: não junte dois fatos numa nota nem repita o mesmo "
        "fato em notas diferentes. Quantas notas? Quantas ideias houver — nem mais, nem "
        "menos.\n\n"
        "IGNORE: saudações, small talk, correções de rumo, pedidos de desculpa do "
        "assistente, e QUALQUER dado perecível (cotação, clima, hora, preço de hoje).\n\n"
        "Formato EXATO, uma ideia por nota, sem repetir:\n\n"
        "## <título auto-contido, com o assunto dentro>\n"
        "<a ideia em 1-3 frases afirmativas e completas>\n"
        "**Malha Neural:** [[Conceito relacionado]]\n"
        f"{TAG_ATOMO}\n\n"
        "Separe as notas por uma linha em branco. Se não houver NADA durável a reter "
        "neste trecho, responda apenas 'NADA'. Sem introdução e sem conclusão.\n\n"
        f"CONVERSA:\n{trecho}"
    )


def prompt_sintese_conversa(conteudo: str) -> str:
    return (
        "Extraia da conversa abaixo (entre Usuário e IA) as ideias de conhecimento "
        "que valham reter, como NOTAS ATÔMICAS. Capture também os TEMAS de interesse/"
        "curiosidade do usuário. Use EXATAMENTE este formato, uma ideia por nota, sem repetir:\n\n"
        "## <título curto da ideia>\n"
        "<a ideia em 1-2 frases afirmativas>\n"
        "**Malha Neural:** [[Conceito relacionado]]\n"
        f"{TAG_ATOMO} {TAG_NOVO}\n\n"
        "Separe as notas por uma linha em branco. Se não houver nada que valha reter, "
        "responda apenas 'NADA'. Nada de introdução ou conclusão.\n\n"
        f"CONVERSA:\n{conteudo}"
    )
