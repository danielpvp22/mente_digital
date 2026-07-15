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


# --- Filler (mascara latência da busca web) ----------------------------------
SYS_FILLER = "Confirme rápido e sem interrogatórios."


def prompt_filler(texto_usuario: str) -> str:
    return (
        f"O usuário perguntou sobre: '{texto_usuario}'. "
        "Diga APENAS que vai pesquisar os arquivos sobre isso. Sem fazer perguntas."
    )


# --- Resposta principal (anti-alucinação, brutalmente concisa) ---------------
SYS_RESPOSTA = """Você é um Engenheiro de Dados Sênior.
REGRA 1: Baseie-se APENAS nos dados fornecidos. Se não estiver lá, diga 'Não tenho informações suficientes'. NUNCA invente.
REGRA 2: Seja BRUTALMENTE CONCISO. Resuma a resposta em no máximo 3 ou 4 frases curtas e diretas.
REGRA 3: Vá direto ao ponto, sem introduções polidas."""


def prompt_resposta_web(dados_web: str, texto_usuario: str) -> str:
    return (
        f"Dados da Web: {dados_web}\n"
        f"Usuário: '{texto_usuario}'. "
        "Responda direto e estruturado baseado estritamente na Web."
    )


def prompt_resposta_cache(contexto_combinado: str, texto_usuario: str) -> str:
    return (
        f"Contexto Local e RAM: {contexto_combinado}\n"
        f"Usuário: '{texto_usuario}'. "
        "Responda baseado ESTRITAMENTE nos dados."
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


# --- ETL / Idle (síntese em background) --------------------------------------
SYS_SINTESE = "Você é um documentarista analítico."


def prompt_sintese(tema: str, dados: str) -> str:
    return (
        f"Sintetize os dados brutos a seguir em um documento técnico e estruturado "
        f"sobre '{tema}'. Dados: {dados}"
    )


SYS_RESUMO = "Organize a conversa em um Markdown impecável."


def prompt_resumo_sessao(conteudo: str) -> str:
    return (
        "Analise este registro de conversa entre Usuário e IA. Crie um resumo bem "
        "estruturado com Título em H1, Principais Tópicos (em bullet points) e "
        f"Conclusão.\n\nREGISTRO BRUTO:\n{conteudo}"
    )
