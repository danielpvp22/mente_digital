"""
Utilitários de texto para relevância léxica (anti Cache-Hit falso).

Usados para:
- aterrar a busca local (o trecho recuperado precisa MENCIONAR a entidade da query);
- filtrar a RAM da sessão por sobreposição de tema com a pergunta atual;
- limpar a query do extrator (tirar saudação/fillers, capar tamanho).
"""
from __future__ import annotations

import re
import unicodedata

# Palavras genéricas (saudações, pronomes, preposições, verbos de intenção...) que
# NÃO ajudam a identificar o assunto. Ficam de fora das keywords e da query de busca.
STOP = {
    "ola", "oi", "ei", "opa", "bom", "boa", "dia", "tarde", "noite",
    "gostaria", "quero", "queria", "preciso", "gostava", "poderia", "pode",
    "entender", "aprender", "saber", "conhecer", "ver", "falar", "fala",
    "explica", "explicar", "conta", "contar", "diga", "dizer", "me",
    "mais", "muito", "sobre", "como", "porque", "por", "que", "qual", "quais",
    "quando", "onde", "quem", "voce", "vc", "tem", "temos", "agora", "favor",
    "meu", "minha", "seu", "sua", "o", "a", "os", "as", "um", "uma", "uns", "umas",
    "de", "do", "da", "dos", "das", "no", "na", "nos", "nas", "e", "ou", "com",
    "sem", "para", "pra", "pro", "pelo", "pela", "em", "ao", "aos", "isso", "esse",
    "essa", "este", "esta", "isto", "funciona", "funcionamento", "modelo",
    "informacao", "informacoes", "recente", "recentes", "dados", "sim", "nao",
    "talvez", "ainda", "ja", "entao", "tambem",
}


# Caracteres CJK (Han). Um átomo com corpo em chinês nunca ajuda uma pergunta em PT,
# mas ocupa vaga no orçamento de contexto — a síntese saiu no idioma errado (medido:
# 30 na base). `fracao_cjk` mede a proporção para poder podar/rejeitar.
_CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿]")


def fracao_cjk(s: str) -> float:
    """Proporção de caracteres CJK entre os NÃO-brancos. ~0 para PT, alta p/ chinês.

    Robusta a átomo em PT que cita um termo estrangeiro isolado (proporção baixa) —
    só dispara quando o CORPO é substancialmente CJK."""
    sem_espaco = [c for c in s if not c.isspace()]
    if not sem_espaco:
        return 0.0
    return sum(1 for c in sem_espaco if _CJK_RE.match(c)) / len(sem_espaco)


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normaliza(s: str) -> str:
    return sem_acento(s).lower().strip()


def tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normaliza(s))


def palavras_chave(s: str, min_len: int = 3) -> set[str]:
    """Termos significativos da frase (sem stopwords, len>=min_len ou com dígito)."""
    out = set()
    for t in tokens(s):
        if t in STOP:
            continue
        if len(t) >= min_len or any(ch.isdigit() for ch in t):
            out.add(t)
    return out


def tem_sobreposicao(a: set[str], b: set[str]) -> bool:
    return bool(a & b)


def jaccard(a: set[str], b: set[str]) -> float:
    """Similaridade de Jaccard entre dois conjuntos de tokens: |∩| / |∪|.

    Usado no dedup near-duplicate do contexto (G6): dois átomos com quase os mesmos
    tokens são a MESMA ideia — manter os dois só gasta orçamento de contexto (mais
    prefill = TTFT maior). Insensível à ordem das palavras (é conjunto), então uma
    paráfrase reordenada ainda casa. Conjunto vazio devolve 0.0 (não é duplicata)."""
    if not a or not b:
        return 0.0
    uniao = len(a | b)
    return len(a & b) / uniao if uniao else 0.0


def contem_alguma(texto: str, chaves: set[str]) -> bool:
    """O texto menciona ao menos uma das keywords? (aterramento léxico)"""
    if not chaves:
        return False
    return bool(set(tokens(texto)) & chaves)


def remover_tag(conteudo: str, tag: str) -> str:
    """Remove uma tag Obsidian (#algo) do texto, sem tocar no resto. Puro/testável.

    Usado na PROMOÇÃO: quando um átomo #conhecimento_novo é de fato usado numa
    resposta, tiramos só essa tag (o #zettelkasten_atomico e a ideia permanecem).
    Casa a tag como palavra inteira (não pega '#conhecimento_novo_x') e limpa o
    espaço/linha que sobra, sem deixar linha em branco dupla.
    """
    tag_escapada = re.escape(tag.lstrip("#"))
    # tag precedida por espaços/tabs OU sozinha na linha; \b evita casar prefixo de outra tag.
    padrao = re.compile(rf"[ \t]*#{tag_escapada}\b")
    novo = padrao.sub("", conteudo)
    # se a remoção deixou uma linha só com espaços, colapsa para linha vazia limpa
    novo = re.sub(r"[ \t]+\n", "\n", novo)
    return novo


def limpar_query(s: str, max_palavras: int = 6) -> str:
    """
    Transforma a saída do extrator numa query enxuta: remove saudação/fillers,
    preserva ordem e caixa dos termos reais, e capa o tamanho. Evita que uma frase
    inteira ('Olá gostaria de entender...') vire query de busca.
    """
    saida = []
    for p in s.split():
        base = re.sub(r"[^\w]", "", normaliza(p))
        if not base or base in STOP or len(base) < 2:
            continue
        saida.append(p)
        if len(saida) >= max_palavras:
            break
    return " ".join(saida)
