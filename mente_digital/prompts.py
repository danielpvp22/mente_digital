"""
Prompts centralizados. Todos seguem a regra de conduta do projeto: comandos
diretivos, curtos e proibindo a IA de ser educada/prolixa.
"""
from __future__ import annotations

import re

# --- Preâmbulo comum (consultoria TTFT #10 — investigação) -------------------
# Prefixo IDÊNTICO para TODO system prompt quando MENTE_PROMPT_PREAMBULO_COMUM=true
# (composição única em llm.montar_system). Prefixo byte-igual entre extrator/roteador/
# resposta => o llama.cpp reaproveita o KV do maior prefixo comum entre chamadas
# consecutivas (prefill economizado). Curto e NEUTRO de propósito: não pode mudar o
# comportamento de nenhuma tarefa, só alinhar o começo do prompt.
PREAMBULO_COMUM = (
    "Você é a Mente Digital, assistente local do usuário. "
    "Siga estritamente a tarefa definida a seguir."
)

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
#
# FILLER CONTÍNUO: a 1ª ponte é o _msg_web (dinâmico, nomeia a query). Enquanto o
# deep-fetch (3-12s) não volta, o _responder_web fala estas pontes curtas ADICIONAIS
# a cada intervalo — todas templates, sem LLM, então não pesam no TTFA. Rotacionadas
# pelo índice p/ não repetir a mesma frase num fetch longo.
PONTES_WEB = [
    "Só um instante, tô abrindo as fontes.",
    "Ainda buscando, quase lá.",
    "Deixa eu conferir mais uma fonte.",
    "Um segundo, reunindo o material.",
]


def ponte_continuacao(i: int) -> str:
    """i-ésima ponte de continuação do filler contínuo (rotaciona a lista)."""
    return PONTES_WEB[i % len(PONTES_WEB)]


# --- Resposta principal (anti-alucinação, brutalmente concisa) ---------------
SYS_RESPOSTA = """Você é um Engenheiro de Dados Sênior.
REGRA 1: Baseie-se APENAS nos dados fornecidos. Se não estiver lá, responda EXATAMENTE 'Não tenho informações suficientes' — essa frase SOZINHA, sem reformular nem completar. NUNCA invente.
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
REGRA 2: Só se os dados realmente NÃO contiverem a resposta, responda EXATAMENTE 'Não tenho informações suficientes' — a frase sozinha, sem reformular. NUNCA invente dado que não está nos textos.
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
REGRA 1: Baseie-se APENAS nos átomos fornecidos. Se a resposta não estiver neles, responda EXATAMENTE 'Não tenho informações suficientes' — essa frase SOZINHA, sem reformular nem completar com ressalvas. NUNCA invente.
REGRA 2: A base é Zettelkasten — vários átomos de 1 ideia. INTEGRE os relevantes numa resposta coerente; não liste, não repita, ignore em silêncio os irrelevantes.
REGRA 3: Se um trecho está marcado como 'WEB — informação GENÉRICA', NÃO o apresente como dado específico do usuário. Deixe claro que é genérico ('de forma geral, na web...') e não afirme que é o projeto/lista/configuração DELE.
REGRA 4: UM parágrafo, direto, sem introdução polida."""


def prompt_resposta_atomos(atomos: str, texto_usuario: str) -> str:
    return (
        f"Átomos de conhecimento:\n{atomos}\n\n"
        f"Pergunta: '{texto_usuario}'.\n"
        "Escreva UM parágrafo integrando só os átomos que realmente tratam da pergunta. "
        "Se nenhum tratar, responda EXATAMENTE 'Não tenho informações suficientes' (a frase sozinha)."
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
        "Relate APENAS o que está nos resultados — NÃO invente ações, fatos ou números "
        "que não estejam neles. Sem introduções polidas."
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
# Sentinela anti-alucinação (normalizado) — REGRA 1 do system prompt de resposta.
# Vive aqui (camada de linguagem) porque agent.py E respostas.py o consomem; o texto
# TEM que casar com o que o system prompt manda o modelo dizer.
SENTINELA_INSUF = "nao tenho informacoes suficientes"

# --- Detecção FUZZY do sentinela (teste real 2026-07-21) ----------------------
# O Qwen3-4B-2507 PARAFRASEIA o contrato ("não há átomos que confirmem...",
# "não encontrei dados sobre...") e a checagem por frase exata deixava a dúvida
# ser FALADA em vez de escalar pra web. A regex cobre a família "não <verbo de
# posse/busca> ... <recurso de informação>" sobre texto JÁ normalizado
# (textutils.normaliza: minúsculo, sem acento). O {0,2} no meio é de propósito:
# {0,3} casaria "não tenho dúvida de que [os] dados..." — exatamente a resposta
# REAL do teste do guard (falso positivo).
_SENTINELA_VARIANTES = re.compile(
    r"nao\s+(?:tenho|ha|possuo|encontrei|encontro|localizei|achei|consta|constam|existe|existem)\s+"
    r"(?:\w+\s+){0,2}?"
    r"(?:informacao|informacoes|dados?|registros?|notas?|atomos?|anotacao|anotacoes|evidencias?|contexto)\b"
)

# Aberturas que ainda PODEM virar um sentinela parafraseado — o guard só retém o
# stream nesses começos (resposta que abre de outro jeito libera no 1º token,
# preservando o TTFA). Janela curta: passou dela sem casar, é resposta real.
_ABERTURAS_SENTINELA = (
    "nao ", "infelizmente", "desculpe", "desculpa", "ainda nao ", "no momento", "por enquanto",
)
JANELA_SENTINELA = 72   # ~18 tokens ≈ 140ms a 130 tok/s — só pago em abertura suspeita


def parece_sentinela(texto_normalizado: str) -> bool:
    """O texto (já normalizado) declara insuficiência — exato OU parafraseado? Puro."""
    return (
        SENTINELA_INSUF in texto_normalizado
        or bool(_SENTINELA_VARIANTES.search(texto_normalizado))
    )


def abre_como_sentinela(texto_normalizado: str) -> bool:
    """O começo do stream ainda pode ser um sentinela (exato ou paráfrase)? Enquanto
    True o guard retém áudio/texto; False = pode liberar. Puro."""
    if len(texto_normalizado) >= JANELA_SENTINELA:
        return False
    return any(
        texto_normalizado.startswith(a) or a.startswith(texto_normalizado)
        for a in _ABERTURAS_SENTINELA
    )


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


def prompt_atomizar_livro(livro: str, capitulo: str, trecho: str) -> str:
    """Ingestão de livro (Fase 1): mesma gramática do prompt_sintese, mas com a
    ordem de FIDELIDADE — o átomo de livro registra o que o AUTOR diz, não o que o
    modelo acha; é o que evita o vault 'opinar' por cima da fonte."""
    return (
        f"O trecho abaixo é do livro '{livro}' ({capitulo}). Extraia as ideias-chave "
        "como NOTAS ATÔMICAS FIÉIS AO TEXTO — registre o que o autor afirma, sem "
        "opinião sua e sem conhecimento externo.\n"
        # FONTE EM OUTRO IDIOMA: o átomo SEMPRE sai em PT-BR. Não é preferência de
        # estilo — o gate de relevância exige aterramento LÉXICO (interseção exata de
        # tokens entre pergunta e chunk, textutils.contem_alguma). Um átomo em inglês
        # não casa NENHUM token de uma pergunta em português, então perderia metade
        # do critério e dependeria só do limiar semântico (0.16, rígido). Manter o
        # termo técnico original entre parênteses dá aterramento nos DOIS idiomas.
        "Escreva as notas SEMPRE em português do Brasil, mesmo que o trecho esteja em "
        "outro idioma; ao traduzir um termo técnico, mantenha o original entre "
        "parênteses na primeira menção — ex.: 'fotossíntese (photosynthesis)'.\n"
        "Use EXATAMENTE este formato para cada nota, uma ideia por nota, sem repetir:\n\n"
        "## <título curto da ideia>\n"
        "<a ideia em 1-2 frases afirmativas>\n"
        "**Malha Neural:** [[Conceito relacionado]]\n"
        f"{TAG_ATOMO} {TAG_NOVO}\n\n"
        "Separe as notas por uma linha em branco. Nada de introdução ou conclusão.\n\n"
        f"TRECHO DO LIVRO:\n{trecho}"
    )


def prompt_fundir_atomos(textos: str) -> str:
    """Consolidação (Fase 2): N átomos quase-idênticos viram UM canônico. A ordem
    dura é PRESERVAR os fatos distintos — fundir não é resumir; nuance descartada
    aqui seria perda permanente (por isso os originais vão pro arquivo, não pro lixo)."""
    return (
        "As notas abaixo tratam da MESMA ideia, com variações. Funda-as em UMA única "
        "nota canônica que preserve TODOS os fatos e nuances distintos (datas, números, "
        "condições, exceções) — sem inventar nada e sem opinar. Use EXATAMENTE o formato:\n\n"
        "## <título curto da ideia>\n"
        "<a ideia consolidada em até 4 frases afirmativas>\n"
        "**Malha Neural:** [[Conceito relacionado]]\n"
        f"{TAG_ATOMO}\n\n"
        f"NOTAS A FUNDIR:\n{textos}"
    )


def prompt_sintese_capitulo(livro: str, capitulo: str, atomos: str) -> str:
    """O 'reduce' da ingestão hierárquica: a atomização fragmenta o argumento longo;
    esta síntese preserva a TESE do capítulo numa nota única."""
    return (
        f"Abaixo estão notas atômicas extraídas de '{capitulo}' do livro '{livro}'. "
        "Escreva UMA síntese corrida (um parágrafo, 4 a 7 frases) que capture a TESE "
        "do capítulo — o argumento que LIGA as notas, não uma lista delas. Seja fiel "
        "ao material; sem opinião externa.\n\n"
        f"NOTAS:\n{atomos}"
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
# A REGRA 1 aqui não é enfeite: é a mesma disciplina do SYS_RESPOSTA ("Baseie-se APENAS
# nos dados fornecidos... NUNCA invente"), que na 1ª versão deste prompt eu esqueci de
# trazer — e o resultado foi medido, átomo por átomo, contra a fonte:
#   - conversa "520 rpm num quadro 700, quantos km/h?" (cuja resposta foi TRUNCADA na
#     origem): o modelo inventou "a circunferência da roda de 700c é ~2,1 metros" e
#     "o fator de velocidade é a rotação vezes a circunferência". Nada disso existe na
#     fonte.
#   - janela que TERMINA na pergunta "qual o tempo de liquidez de cada pool?": o modelo
#     respondeu "1 a 7 dias" — número que não está em lugar nenhum do texto — e ignorou
#     todo o conteúdo real da janela (Stratum, Equihash, shares).
# Ou seja: pedir "extraia as ideias sobre X" o modelo lê como "escreva notas sobre X",
# e ele preenche a lista com o que sabe. Um átomo inventado é pior que nenhum: ele entra
# no vault com a autoridade do histórico do próprio usuário.
SYS_SINTESE_IMPORT = (
    "Você EXTRAI notas de um texto que recebe. Você NÃO escreve sobre o assunto e NÃO "
    "usa o seu próprio conhecimento: você reporta apenas o que o texto AFIRMA. "
    "Português direto, sem nada fora do formato pedido."
)


def prompt_sintese_import(tema: str, trecho: str) -> str:
    return (
        "Extraia NOTAS ATÔMICAS do texto no fim desta mensagem.\n\n"
        "REGRA 1 — FIDELIDADE (a mais importante de todas): cada nota tem que ser "
        "sustentada por uma afirmação LITERAL do texto. NUNCA acrescente número, data, "
        "medida, unidade ou fato que não esteja escrito ali. Se o texto FAZ uma pergunta "
        "e não a responde, IGNORE a pergunta — não a responda com o que você sabe. Se um "
        "cálculo é anunciado mas não concluído, não conclua. Na dúvida, OMITA a nota. "
        "Escrever menos notas é sempre melhor que escrever uma nota inventada.\n\n"
        "REGRA 2 — SÓ FATOS: uma nota só vale se carrega um fato concreto do texto. "
        "É PROIBIDO escrever nota vazia como 'X é necessário para calcular Y', 'a "
        "velocidade é calculada considerando A, B e C' ou 'há informações sobre Z'. "
        "Se o trecho não afirma nenhum fato, responda apenas 'NADA'.\n\n"
        f"CONTEXTO — o texto trata de '{tema}'. Isto serve para você escrever títulos "
        "auto-contidos; NÃO é um filtro. Extraia TODAS as ideias do trecho, inclusive as "
        "que fogem desse assunto.\n\n"
        "REGRA 3 — AUTO-CONTENÇÃO: cada nota será lida ISOLADA, meses depois, sem este "
        "texto ao lado. Título e corpo precisam dizer de que se trata, nomeando a "
        "entidade concreta. Títulos genéricos como 'Economia necessária', 'Gemini' ou "
        "'Resultado final' são inúteis e proibidos: escreva 'Economia de munição "
        "necessária no Tarkov'. Nunca use pronome sem antecedente ('ele', 'isso', 'esse "
        "valor', 'o script') — repita o nome.\n\n"
        "REGRA 4 — TÍTULOS DISTINTOS: cada título tem que ser DIFERENTE dos outros e "
        f"nomear a ideia ESPECÍFICA daquela nota. NÃO repita '{tema}' como título de "
        "várias notas. Se duas notas teriam o mesmo título, ou são a mesma nota (escreva "
        "UMA), ou os títulos estão genéricos demais (diferencie-os).\n\n"
        # A regra da Malha é MEDIDA, não estética. A linha é INDEXADA junto com o corpo
        # (só o frontmatter sai), então cada link acrescenta tokens ao vetor do átomo.
        # Testado sobre um átomo de Equihash, comparando 1 link contra 6:
        #   - links que REPETEM palavras do corpo ([[Equihash]], [[Zcash]], [[Prova de
        #     Trabalho]]) não ganham nada e chegam a PIORAR (+0.008): mesma informação,
        #     mais tokens, o vetor dilui.
        #   - um link com conceito AUSENTE do corpo ([[ASIC]]) abriu um caminho novo:
        #     "minerar Zcash com ASIC funciona?" foi de 0.512 para 0.463.
        # Daí "conceitos que NÃO aparecem no texto da nota": é o que transforma uma
        # ideia em várias portas de entrada, que é o ponto do Zettelkasten. A
        # atomicidade nunca limitou os links — ela existe para PERMITI-los.
        "REGRA 5 — MALHA NEURAL: liste de 2 a 4 conceitos relacionados, e prefira os "
        "que NÃO aparecem escritos no corpo da nota. Um link que repete palavra já "
        "presente no texto não acrescenta nada; um que traz um conceito novo (a "
        "tecnologia adjacente, o problema que resolve, a categoria a que pertence) faz "
        "a nota ser encontrada por quem pergunta de outro ângulo.\n\n"
        "IGNORE: saudações, small talk, correções de rumo, pedidos de desculpa, e "
        "qualquer dado perecível (cotação, clima, hora, preço de hoje).\n\n"
        # NÃO peça a linha de tags aqui: `agent.normalizar_atomo` a impõe depois, no
        # Python. Medido, o LLM gastava 7 dos 62 tokens de cada átomo emitindo
        # "#zettelkasten_atomico" — 11% do decode, ~29 min do lote — para o Python
        # reescrever a linha em seguida. Pedir ao modelo o que o código já garante é
        # pagar duas vezes pela mesma coisa.
        "Formato EXATO:\n\n"
        "## <título auto-contido, com a entidade concreta>\n"
        "<o fato, em 1-3 frases afirmativas, tudo sustentado pelo texto>\n"
        "**Malha Neural:** [[Conceito A]] [[Conceito B]] [[Conceito C]]\n\n"
        "NÃO escreva tags (#algo): são adicionadas depois, automaticamente.\n"
        "Separe as notas por uma linha em branco. Sem introdução e sem conclusão.\n\n"
        f"TEXTO:\n{trecho}"
    )


# --- Watcher ("me avise quando"): a condição foi satisfeita pelos dados da web? -----
# Roda em background no SchedulerService. Saída MÁQUINA: 1ª palavra SIM/NAO (o código
# só olha isso), seguida de uma frase curta para falar ao usuário quando SIM. Conservador
# como o anti-alucinação: só diz SIM se os dados AFIRMAM a condição — um watcher que
# dispara à toa ("o dólar passou de 5" quando não passou) é pior que checar de novo depois.
SYS_WATCHER = (
    "Você verifica se uma CONDIÇÃO já é verdadeira com base em DADOS da web. "
    "Responda começando com SIM ou NAO (exatamente, sem acento). Se SIM, complete com "
    "UMA frase curta dizendo o fato que confirma. Se os dados não bastam, responda NAO. "
    "Nunca invente número que não esteja nos dados."
)


def prompt_watcher(condicao: str, dados_web: str) -> str:
    return (
        f"CONDIÇÃO a verificar: {condicao}\n\n"
        f"DADOS DA WEB (agora):\n{dados_web}\n\n"
        "A condição já é verdadeira segundo os dados? Comece com SIM ou NAO."
    )


# --- Diapasão (#36): perfil de COMO o dono prefere conversar ------------------------
SYS_DIAPASAO = (
    "Você observa uma conversa e descreve, em 1-2 frases, COMO este usuário prefere "
    "ser RESPONDIDO — não o tom dele, mas o que serve a ele: tamanho (curto/detalhado), "
    "se gosta de exemplos/analogias, formal/informal, nível técnico, se quer o ponto "
    "direto. Diretriz de ESTILO, nunca conteúdo. Se não há preferência clara, responda "
    "apenas NADA (sem acento)."
)


def prompt_perfil_conversa(historico: str, perfil_atual: str = "") -> str:
    base = (
        "Perfil atual do usuário (refine-o se a conversa mostrar algo novo; mantenha se "
        f"nada mudou): {perfil_atual}\n\n" if perfil_atual else ""
    )
    return (
        f"{base}CONVERSA:\n{historico}\n\n"
        "Descreva em 1-2 frases como responder melhor a este usuário, ou NADA."
    )


# --- Detector de Contradição (#24): dois átomos do mesmo tema se contradizem? -------
SYS_CONTRADICAO = (
    "Você compara DUAS notas de conhecimento sobre um tema parecido e diz se elas se "
    "CONTRADIZEM (afirmam fatos incompatíveis — números, datas ou conclusões opostas). "
    "Diferença de foco, detalhe ou complemento NÃO é contradição. Responda começando "
    "com SIM ou NAO (exatamente, sem acento). Se SIM, complete com UMA frase curta "
    "dizendo qual é o conflito. Na dúvida, responda NAO."
)


def prompt_contradicao(nota_a: str, nota_b: str) -> str:
    return (
        f"NOTA A:\n{nota_a}\n\n"
        f"NOTA B:\n{nota_b}\n\n"
        "As duas se contradizem? Comece com SIM ou NAO."
    )


# --- Briefing diário (o "flash briefing"): fala curta com os temas do usuário --------
SYS_BRIEFING = (
    "Você prepara um briefing falado, curto e caloroso, em português. Máximo 4 frases. "
    "Sem markdown, sem listas, sem títulos — é para ser OUVIDO. Vá direto ao ponto."
)


def prompt_briefing(data_hoje: str, temas: str, contexto: str) -> str:
    return (
        f"Hoje é {data_hoje}. Prepare um briefing falado de bom dia.\n"
        f"Temas que o usuário vem explorando: {temas or 'nenhum em especial'}.\n"
        f"Informação recente para mencionar (se houver): {contexto or 'nenhuma'}.\n\n"
        "Escreva 2 a 4 frases naturais, como se estivesse falando com ele. "
        "Comece com uma saudação breve."
    )


# --- Síntese sob Demanda (#23): "o que eu sei sobre X" via map-reduce ---------------
# Fluxo SEPARADO do pipeline normal: em vez de empilhar dezenas de átomos num prompt só
# (estouraria o n_ctx e enviesaria o corte), resume em LOTES (map) e depois combina os
# resumos parciais (reduce). Cada chamada cabe no contexto.
SYS_SINTESE_TEMA = (
    "Você resume o que a pessoa JÁ registrou sobre um tema, com base SÓ nos átomos "
    "fornecidos. Nunca invente nem complemente com conhecimento externo. Português direto."
)


def prompt_sintese_tema_map(tema: str, atomos: str) -> str:
    return (
        f"Tema: '{tema}'.\n\nÁtomos de conhecimento:\n{atomos}\n\n"
        "Liste em tópicos curtos os FATOS que estes átomos trazem sobre o tema. "
        "Só o que está escrito acima; ignore o que não for do tema. Sem introdução."
    )


def prompt_sintese_tema_reduce(tema: str, parciais: str) -> str:
    return (
        f"Tema: '{tema}'.\n\nResumos parciais do que a pessoa já sabe:\n{parciais}\n\n"
        "Combine numa síntese FALADA, coerente e sem repetição, de 3 a 5 frases, do que "
        "ela sabe sobre o tema. Comece direto (ex.: 'Sobre X, você tem anotado que...'). "
        "Sem listas, sem markdown."
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
