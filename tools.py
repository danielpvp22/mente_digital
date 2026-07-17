"""
Ferramentas do agente (function calling ADITIVO).

Filosofia (decidida com o dono do projeto): pergunta de conhecimento continua
indo pelo pipeline afinado — TTFA, gate de cache, buffer anti-sentinela e filler
intactos. SÓ mensagens que "pedem uma AÇÃO" passam pelo roteador de ferramentas,
e o loop agêntico (multi-passo) é CAPADO (`max_tool_steps`) para a latência não
explodir. Ferramentas "terminais" (calcular, hora, salvar) já saem no 1º passo.

Sem tool-calling nativo do llama.cpp (parser instável): o roteador é um prompt
que devolve JSON `{"tool": ..., "args": {...}}`, validado em 7/7 no Qwen local.
"""
from __future__ import annotations

import ast
import asyncio
import glob
import json
import operator
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional

import textutils
from config import settings
from telemetry import telemetry

if TYPE_CHECKING:  # evita import circular em runtime
    from state import AppContext

Executor = Callable[[dict, "AppContext"], Awaitable[str]]


# ==========================================================================
# Decisão do roteador (JSON)
# ==========================================================================
@dataclass
class Decisao:
    tool: str
    args: dict = field(default_factory=dict)


def _objetos_json(s: str):
    """Gera cada objeto JSON top-level de chaves BALANCEADAS em `s`.

    Ciente de strings (chaves dentro de aspas não contam), então lida com
    `{"expressao":"a}b"}`. Fora de um objeto, só o '{' importa — aspas na prosa
    não confundem. Um passe O(n) sobre a saída curta do roteador.
    """
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if depth == 0:
            if ch == "{":
                start, depth = i, 1
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                yield s[start : i + 1]


def parse_decisao(bruto: str) -> Optional[Decisao]:
    """Extrai o 1º objeto {"tool":..., "args":{...}} VÁLIDO do texto do LLM.

    Varre os objetos JSON balanceados (via `_objetos_json`) e devolve o primeiro
    com uma chave 'tool' string. Robusto a chave solta na prosa ('{sorriso}') e a
    mais de um objeto — o antigo 'do 1º "{" ao último "}"' quebrava nesses casos.
    None se nenhum objeto válido for achado.
    """
    if not bruto:
        return None
    for candidato in _objetos_json(bruto):
        try:
            obj = json.loads(candidato)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        tool = obj.get("tool")
        if not isinstance(tool, str) or not tool:
            continue
        args = obj.get("args")
        return Decisao(tool=tool, args=args if isinstance(args, dict) else {})
    return None


# ==========================================================================
# Gate lexical: a mensagem PARECE uma ação? (senão, nem chama o roteador)
# ==========================================================================
# Já em forma normalizada (sem acento) porque comparamos com textutils.normaliza.
_GATILHOS_ACAO = (
    "salva", "salvar", "guarda", "guardar", "anota", "anotar",
    "cria uma nota", "criar nota", "nova nota",
    "calcula", "calcular", "quanto e", "que horas", "que dia", "data de hoje",
    "abre a nota", "abrir nota", "le a nota", "leia a nota", "ler nota", "mostra a nota",
    "lista as notas", "listar notas", "quais notas", "minhas notas",
    "procura na web", "procure na web", "pesquisa na web", "pesquise na web",
    "busca na web", "busque na web",
)


def talvez_acao(texto: str) -> bool:
    """Pré-filtro barato: só mensagens com gatilho de ação acionam o roteador LLM.

    Assim uma PERGUNTA normal nunca paga a chamada extra do roteador (TTFA
    preservado). Falsos positivos são inofensivos: o roteador pode devolver
    'responder' e cair no pipeline normal mesmo assim.
    """
    t = textutils.normaliza(texto)
    return any(g in t for g in _GATILHOS_ACAO)


# Perguntas de DADO EM TEMPO REAL: o banco local é inútil e desatualizado (cotação,
# preço agora, notícias/clima de hoje). Lista curada e CONSERVADORA — só frases que
# quase sempre pedem web — para não pular o local de uma pergunta que o vault responde.
# Falso NEGATIVO é barato: cai na cascata normal (só ~1s mais lento). Falso POSITIVO
# custa uma resposta ruim, então preferimos ser restritos.
_GATILHOS_TEMPO = (
    "cotacao", "quanto esta o dolar", "quanto esta o euro", "quanto esta o bitcoin",
    "dolar hoje", "euro hoje", "bitcoin hoje", "preco do bitcoin", "preco do dolar",
    "preco do euro", "bolsa hoje", "ibovespa", "preco das acoes",
    "noticias de hoje", "noticias hoje", "ultimas noticias", "manchetes",
    "previsao do tempo", "clima hoje", "temperatura hoje", "vai chover",
)


def talvez_tempo_real(texto: str) -> bool:
    """True se a pergunta pede dado em tempo real → pular local e ir DIRETO pra web.

    Evita a passada local morta (recuperação larga + sentinela) em perguntas que o
    vault nunca responde bem: cotação/preço agora, notícias/clima de hoje.
    """
    t = textutils.normaliza(texto)
    return any(g in t for g in _GATILHOS_TEMPO)


# Gate de INGESTÃO — deliberadamente MAIS LARGO que o de roteamento acima.
#
# Por que dois gates e não um: os custos são ASSIMÉTRICOS e opostos.
#   - talvez_tempo_real decide PULAR O LOCAL. Falso positivo = resposta pior (a
#     pergunta ia ser respondida pelo vault e foi pra web). Caro -> conservador.
#   - e_efemero decide NÃO ETERNIZAR. Falso positivo = deixamos de aprender um fato.
#     Barato -> pode ser agressivo.
# Reusar o gate de roteamento aqui vazaria os casos reais: medido no vault, "como
# será o clima em lisboa para amanhã?" tem talvez_tempo_real=False (a lista tem
# "clima hoje", não "clima") e foi justamente essa pergunta que gerou os átomos
# Conversa_clima_em_lisboa_*. 48 dos 177 átomos auto-colhidos (27%) são previsão do
# tempo e cotação — fatos com validade de horas, eternizados num Zettelkasten e
# recuperados para sempre pelo rag_top_k=40.
_GATILHOS_EFEMERO = _GATILHOS_TEMPO + (
    "clima", "previsao do tempo", "temperatura em", "vai chover", "chove",
    "quanto esta o", "quanto custa", "preco do", "preco da", "preco atual",
    "bolsa de valores", "acoes da", "noticia", "manchete",
    "dolar", "euro", "bitcoin",
    # Leitura de relógio/calendário é a definição de efêmero. Vem pelo caminho de
    # FERRAMENTA (hora_atual), cujo turno também ia parar no dump e virava um átomo
    # "## Hora atual — são 14:32" no Zettelkasten.
    "que horas", "que dia e hoje", "data de hoje",
)


def e_efemero(texto: str) -> bool:
    """True se a resposta tem validade de horas → NÃO vira átomo permanente.

    Só governa a INGESTÃO (fila do ETL e pre-fetch). A resposta é dada normalmente e
    a memória de sessão (RAM) continua guardando o dado — ela morre com a sessão e é
    o que faz o follow-up ("e amanhã?") funcionar.
    """
    t = textutils.normaliza(texto)
    return any(g in t for g in _GATILHOS_EFEMERO)


# ==========================================================================
# Calculadora segura (sem eval)
# ==========================================================================
def _pow_limitado(base, exp):
    """Potência com expoente limitado. A calculadora roda SÍNCRONA no event loop;
    sem o teto, '9**9**9' computaria um inteiro gigante e congelaria o servidor
    inteiro (DoS). 1000 cobre qualquer uso real e mantém o cálculo instantâneo —
    sem pagar o hop de asyncio.to_thread em toda conta."""
    if isinstance(exp, (int, float)) and abs(exp) > 1000:
        raise ValueError("expoente muito grande")
    return operator.pow(base, exp)


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: _pow_limitado, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def calcular_seguro(expr: str) -> str:
    """Avalia uma expressão aritmética via AST (só números e +-*/%//**). Nunca eval."""
    def _ev(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ValueError("constante inválida")
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_ev(node.left), _ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_ev(node.operand))
        raise ValueError("expressão não permitida")

    try:
        arv = ast.parse(expr, mode="eval")
        val = _ev(arv.body)
        if isinstance(val, float) and val.is_integer():
            val = int(val)
        return str(val)
    except Exception:
        return "não consegui calcular essa expressão"


# ==========================================================================
# Registry
# ==========================================================================
@dataclass
class Tool:
    nome: str
    descricao: str          # linha do menu do roteador (com exemplo de args)
    executar: Executor
    terminal: bool = True   # True: após rodar vai direto pra resposta (sem re-rotear)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def registrar(self, tool: Tool) -> None:
        self._tools[tool.nome] = tool

    def get(self, nome: str) -> Optional[Tool]:
        return self._tools.get(nome)

    def nomes(self) -> List[str]:
        return list(self._tools)

    def menu(self) -> str:
        return "\n".join(f"- {t.descricao}" for t in self._tools.values())


# ==========================================================================
# Ferramentas concretas
# ==========================================================================
async def _t_calcular(args: dict, ctx) -> str:
    expr = str(args.get("expressao", "")).strip()
    return f"{expr} = {calcular_seguro(expr)}" if expr else "faltou a expressão"


async def _t_hora(args: dict, ctx) -> str:
    return datetime.now().strftime("Agora são %H:%M de %d/%m/%Y.")


async def _t_listar_notas(args: dict, ctx) -> str:
    def _ls() -> List[str]:
        arquivos = glob.glob(
            os.path.join(settings.caminho_obsidian, "**/*.md"), recursive=True
        )
        return sorted({os.path.splitext(os.path.basename(p))[0] for p in arquivos})

    nomes = await asyncio.to_thread(_ls)
    if not nomes:
        return "O vault está vazio."
    return "Notas no vault: " + ", ".join(nomes[:50])


async def _t_ler_nota(args: dict, ctx) -> str:
    titulo = str(args.get("titulo", "")).strip()
    if not titulo:
        return "faltou o título da nota"
    chaves = textutils.palavras_chave(titulo)

    def _achar_e_ler() -> Optional[str]:
        arquivos = glob.glob(
            os.path.join(settings.caminho_obsidian, "**/*.md"), recursive=True
        )
        melhor, melhor_score = None, 0
        for p in arquivos:
            nome = os.path.splitext(os.path.basename(p))[0]
            score = len(chaves & textutils.palavras_chave(nome))
            if score > melhor_score:
                melhor, melhor_score = p, score
        if melhor is None:
            return None
        with open(melhor, "r", encoding="utf-8") as f:
            return f.read()

    conteudo = await asyncio.to_thread(_achar_e_ler)
    if conteudo is None:
        return f"não encontrei uma nota parecida com '{titulo}'"
    return conteudo[:1500]


async def _t_salvar_nota(args: dict, ctx) -> str:
    titulo = str(args.get("titulo", "")).strip() or "Nota Rápida"
    conteudo = str(args.get("conteudo", "")).strip()
    if not conteudo:
        return "faltou o conteúdo da nota"
    seguro = "".join(c for c in titulo if c.isalnum() or c in " -_")[:40].strip() or "Nota"
    # Carimbo com microssegundos: duas notas no mesmo segundo não se sobrescrevem.
    nome = f"{seguro.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.md"
    caminho = os.path.join(settings.caminho_obsidian, nome)

    def _save() -> None:
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(f"# {titulo}\n\n{conteudo}")

    await asyncio.to_thread(_save)
    ctx.track_task(ctx.vectorstore.sync())  # reindexa em background (ref. retida)
    return f"nota '{titulo}' salva ({nome})"


async def _t_buscar_web(args: dict, ctx) -> str:
    termo = str(args.get("termo", "")).strip()
    if not termo:
        return "faltou o termo de busca"
    return await ctx.web.search(termo)


def criar_registry() -> ToolRegistry:
    """Monta o registry padrão de ferramentas do agente."""
    reg = ToolRegistry()
    reg.registrar(Tool(
        "calcular",
        'calcular(expressao): conta matemática. Ex.: {"tool":"calcular","args":{"expressao":"240*0.15"}}',
        _t_calcular, terminal=True,
    ))
    reg.registrar(Tool(
        "hora_atual",
        'hora_atual(): data/hora agora. Ex.: {"tool":"hora_atual","args":{}}',
        _t_hora, terminal=True,
    ))
    reg.registrar(Tool(
        "salvar_nota",
        'salvar_nota(titulo, conteudo): grava uma nota no vault. Ex.: {"tool":"salvar_nota","args":{"titulo":"Reunião","conteudo":"amanhã 10h"}}',
        _t_salvar_nota, terminal=True,
    ))
    reg.registrar(Tool(
        "listar_notas",
        'listar_notas(): lista os títulos das notas. Ex.: {"tool":"listar_notas","args":{}}',
        _t_listar_notas, terminal=False,
    ))
    reg.registrar(Tool(
        "ler_nota",
        'ler_nota(titulo): lê uma nota pelo título. Ex.: {"tool":"ler_nota","args":{"titulo":"Arquitetura"}}',
        _t_ler_nota, terminal=False,
    ))
    reg.registrar(Tool(
        "buscar_web",
        'buscar_web(termo): pesquisa na internet. Ex.: {"tool":"buscar_web","args":{"termo":"novidades python 3.13"}}',
        _t_buscar_web, terminal=False,
    ))
    return reg
