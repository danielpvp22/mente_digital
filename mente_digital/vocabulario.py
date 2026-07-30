"""Ponte de vocabulário: o jargão INGLÊS do dono → os termos PT que o vault usa.

O defeito que este módulo existe para corrigir (teste real de 2026-07-29): o dono
perguntou *"o que é topping"* e recebeu resposta sobre **cobertura de pizza**. A
autópsia (log + índice, zero GPU) mostrou que o vault COBRE o assunto com fartura
— "Auxinas - Dominância Apical", "Poda Redireciona Hormônios", "Esta planta curta
foi podada uma vez ao remover o meristema" — mas sob nomes que não compartilham
NENHUM token com a palavra que ele digitou. O aterramento léxico é um casamento de
keyword: sem token em comum ele não aterra nada, e os dois únicos chunks que
casaram "topping" vieram de notas de YOLO/Ollama (a palavra inglesa só existe nos
baldes auto-colhidos). Contexto de outro domínio → o portão definicional escalou
para a web, certíssimo, e a web sem âncora de domínio respondeu sobre pizza.

Os termos daqui vão aos DOIS lados da busca: ao aterramento léxico (e à query da web)
e ao EMBEDDING. Eu tentei primeiro só o léxico, para não perturbar a fase (b) do
extrator, e MEDI que não bastava — "o que é topping" ia de 0 para 2 átomos, e 2 fica
abaixo de `definicional_min_atomos`, então a pergunta escapava para a web do mesmo
jeito. Com os termos no embedding são 11 átomos e a distância cai de 0,182 para
0,146, abaixo do `rag_score_confident`.

A razão é estrutural e vale para qualquer ideia futura de "melhorar o aterramento":
**o aterramento é um FILTRO sobre o que o embedding já recuperou, não um canal de
busca próprio.** Ampliar só as keywords nunca alcança o átomo que a recuperação
vetorial não trouxe.

⚠ `idioma.GLOSSARIO` NÃO serve para isto e não deve ser reusado: ele é tabela de
TRADUÇÃO e mantém o jargão de propósito (`"topping": "topping"`), porque o dono quer
ler "bud" e não "broto". Aqui o alvo é o oposto — sair da palavra dele e chegar na
palavra do átomo.

REGRA PARA CRESCER ESTA TABELA (não adivinhar sinônimo):
  1. o job de ingestão guarda o texto ORIGINAL EM INGLÊS da página; ache as páginas
     cujo inglês contém o jargão;
  2. leia os TÍTULOS dos átomos PT dessas mesmas páginas (âncora `origem`, a do
     `livro.origem_do_job`) — ali está o termo que a atomização escolheu;
  3. só entra o sinônimo com chunks de LIVRO medidos.
Medições que reprovaram candidatos plausíveis: "poda apical" 0 chunks, "desponte" 0,
"despontar" 0, "beliscar" 0, "run to waste" 0 (só balaio). Sinônimo bonito que o
vault não usa é peso morto no aterramento.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Tuple

# Cada entrada foi MEDIDA no índice (2026-07-30, 11.220 chunks de página de livro).
# O número é quantos chunks de LIVRO contêm o termo — sinônimo com 0 não entra.
PONTE: Dict[str, Tuple[str, ...]] = {
    # topping = cortar o ápice para quebrar a dominância apical. O livro trata o
    # assunto inteiro sem nunca usar a palavra inglesa:
    #   poda 41 | ramificação 28 | auxinas 19 | meristema 11 | apical 2
    # "ramificação" ficou de FORA: dos 28, boa parte é sexagem, UV e inibidores de
    # crescimento — aterrar por ela devolveria o "Cache Hit falso" que o gate custou
    # caro para fechar. Os quatro abaixo apontam para o mecanismo, não para o tema
    # genérico.
    "topping": ("poda", "apical", "meristema", "auxinas"),
    # A técnica FIM é o topping parcial; o vault a registra como "FIM Técnica" e nas
    # mesmas páginas de poda/meristema.
    "fimming": ("poda", "apical", "meristema"),
    # "Super-Cropping Technique" está no vault com o título em inglês E hifenizado —
    # quem digita "supercropping" (uma palavra) não casa "Super-Cropping" (duas).
    "supercropping": ("super", "cropping", "poda"),
}

# Palavra inteira, sem acento, minúsculo — o mesmo casamento da medição que montou
# a tabela. Compilado uma vez: este gate roda em TODA pergunta.
_RE = {
    j: re.compile(rf"\b{re.escape(j)}\b")
    for j in PONTE
}


def _sem_acento(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", (texto or "").lower())
        if unicodedata.category(c) != "Mn"
    )


def expandir(termos: str) -> str:
    """`termos` acrescido dos termos PT do vault quando há jargão inglês. Puro.

    Devolve o ORIGINAL quando não há jargão (o caso comum): pergunta em português
    não paga nada além de um punhado de regexes sobre uma tabela pequena.

    Não repete o que já está lá — "topping poda" não vira "topping poda poda", que
    só inflaria a query enxuta que o extrator trabalhou para deixar em 5 palavras.
    """
    if not termos:
        return termos
    baixo = _sem_acento(termos)
    novos = []
    for jargao, sinonimos in PONTE.items():
        if not _RE[jargao].search(baixo):
            continue
        for s in sinonimos:
            if s not in novos and not re.search(rf"\b{re.escape(s)}\b", baixo):
                novos.append(s)
    return f"{termos} {' '.join(novos)}" if novos else termos
