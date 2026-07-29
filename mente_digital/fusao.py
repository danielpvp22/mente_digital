"""Fusão ENRIQUECEDORA de duas versões da mesma obra (PURO/testável).

Ordem do dono (2026-07-28), depois de três testes cegos em que a base antiga
venceu 17 a 10: em vez de escolher uma base e apagar a outra, a base NOVA é a
espinha dorsal e a ANTIGA entra DENTRO dela como enriquecimento — mesmo quando
um átomo antigo corresponde a dois ou três novos. Contradição entre as duas
resolve a favor da NOVA.

O que o teste cego mostrou que falta na base nova, e que este módulo persegue: o
NÚMERO. As respostas dela perderam "5 a 14 dias" de secagem, "±0,5 ponto" de
ajuste de pH, "3 a 5 dias" para a folha reverdecer, "6 a 9 semanas" até a
colheita. Ao fatiar mais fino, o modelo separou o dado duro do seu contexto ou o
descartou — daí a riqueza mediana de 12 palavras contra 15.

Por isso o gate de "vale enriquecer" é LÉXICO e barato: um átomo antigo cujos
números e unidades já estão nos átomos novos correspondentes não paga uma
chamada de GPU. Sem esse filtro seriam 5.907 chamadas; com ele, só as que podem
acrescentar algo.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Set, Tuple

from mente_digital import textutils

# Token com DADO DURO: número (com vírgula/ponto/hífen de faixa), porcentagem,
# grau, unidade colada ("5,5", "70°F", "12h", "4x4"). É o que o teste cego
# mostrou sumindo — e é o que não se reconstrói de memória.
_NUMERICO_RE = re.compile(r"\d")
_SO_PONTUACAO = re.compile(r"^[\W_]+$", re.UNICODE)
# Ruído numérico que não é FATO: referência de página/figura apareceria como
# "dado" e dispararia o enriquecimento à toa. Casa a EXPRESSÃO INTEIRA e não o
# token: "fig. 3" são dois tokens, e filtrar só o token deixava o "3" passar.
_RUIDO = re.compile(
    r"\b(p|pp|pág|pag|pagina|página|fig|figura|cap|capítulo|capitulo)s?\.?\s*"
    r"\d+(\s*[-–—]\s*\d+)?", re.IGNORECASE)


def tokens_de_dado(texto: str) -> Set[str]:
    """Tokens que carregam DADO DURO (têm dígito), normalizados.

    Normaliza pelo `textutils` para que "70°F" e "70 °F" não contem como fatos
    diferentes, e descarta o que é referência editorial (p. 12, fig. 3).
    """
    fora: Set[str] = set()
    limpo = _RUIDO.sub(" ", texto or "")
    for bruto in re.findall(r"[\wÀ-ÿ°%,\.\-/]+", limpo):
        if not _NUMERICO_RE.search(bruto):
            continue
        t = textutils.normaliza(bruto).strip(".,;:")
        if t and not _SO_PONTUACAO.match(t):
            fora.add(t)
    return fora


def fatos_ausentes(antigo: str, novos: Sequence[str]) -> Set[str]:
    """Dados duros que o átomo ANTIGO tem e os NOVOS correspondentes não têm."""
    tem = set()
    for n in novos:
        tem |= tokens_de_dado(n)
    return tokens_de_dado(antigo) - tem


def palavras_ausentes(antigo: str, novos: Sequence[str], min_len: int = 4) -> Set[str]:
    """Palavras de conteúdo do antigo que não aparecem em nenhum novo.

    Complementa `fatos_ausentes`: nem todo enriquecimento é numérico ("colher ao
    AMANHECER", "glândulas de resina TURVAS"). Régua mais frouxa e por isso
    usada só com um piso alto de quantidade — ver `vale_enriquecer`.
    """
    chaves = {p for p in textutils.palavras_chave(antigo) if len(p) >= min_len}
    for n in novos:
        chaves -= textutils.palavras_chave(n)
    return chaves


def vale_enriquecer(antigo: str, novos: Sequence[str],
                    min_palavras_novas: int = 4) -> Tuple[bool, str]:
    """-> (vale, motivo). Sem `novos` correspondentes, NÃO vale: um átomo antigo
    sem par na base nova é assunto do dono (pode ser ideia que a nova perdeu
    inteira), não de uma fusão automática que o enfiaria no átomo errado."""
    if not novos:
        return False, "sem par na base nova"
    dados = fatos_ausentes(antigo, novos)
    if dados:
        return True, f"dado duro ausente: {', '.join(sorted(dados)[:4])}"
    palavras = palavras_ausentes(antigo, novos)
    if len(palavras) >= min_palavras_novas:
        return True, f"{len(palavras)} palavras de conteúdo ausentes"
    return False, "o novo já cobre o antigo"


_FRASE_RE = re.compile(r"[^.!?;]+[.!?;]?")
MARCA_CONTRADICAO = "CONTRADICAO"
# A vizinhança do e5 aproxima ideias que COMPARTILHAM VOCABULÁRIO sem serem a
# mesma: "1 a 1,5 galão de solo por mês de vida da planta" caiu a 0,133 de
# "Limpeza do solo com solução nutricional", que é LIXIVIAÇÃO. Está dentro da
# faixa medida de paráfrase (0,042-0,128), então nenhum limiar separa os dois
# casos — quem separa é o julgamento, e o modelo já está sendo chamado.
MARCA_DIFERENTE = "OUTRAIDEIA"


def preserva_o_novo(corpo_novo: str, fundido: str, tolerancia: int = 1) -> bool:
    """O corpo fundido ainda contém os DADOS DUROS do átomo novo?

    A fusão pode acrescentar; não pode SUBTRAIR o que a base nova já dizia — o
    dono foi explícito em manter as ideias da nova. Tolerância de 1 token cobre
    reescrita legítima ("70°F (21°C)" virando "21 °C").
    """
    perdidos = tokens_de_dado(corpo_novo) - tokens_de_dado(fundido)
    return len(perdidos) <= tolerancia


def houve_contradicao(saida: str) -> bool:
    """O modelo sinalizou conflito entre as duas versões (fica só a NOVA)."""
    return MARCA_CONTRADICAO in (saida or "").upper()


def nao_e_a_mesma_ideia(saida: str) -> bool:
    """O modelo disse que o par nem trata do mesmo assunto (não funde nada)."""
    return MARCA_DIFERENTE in (saida or "").upper()


def limpar_fusao(saida: str) -> str:
    """Saída do LLM reduzida ao corpo, sem a marca de contradição e sem rótulo."""
    linhas: List[str] = []
    for ln in (saida or "").strip().splitlines():
        s = ln.strip()
        if not s or s.upper().startswith((MARCA_CONTRADICAO, MARCA_DIFERENTE)):
            continue
        s = re.sub(r"^(corpo|resultado|fundido)\s*:\s*", "", s, flags=re.IGNORECASE)
        linhas.append(s)
    return "\n".join(linhas).strip()


def resumir(itens: Iterable[str], limite: int = 3) -> str:
    """Amostra curta para log/relatório."""
    lista = list(itens)[:limite]
    return "; ".join(lista)
