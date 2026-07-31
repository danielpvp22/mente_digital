"""Ponte de vocabulário: a palavra do DONO → a palavra que está no VAULT.

Duas direções, e a segunda só apareceu no teste real:

1. **EN → PT** — ele digita o jargão inglês do cultivo ("topping") e o átomo está
   em português ("Dominância Apical", "poda", "meristema").
2. **PT → PT** — ele digita o português CORRETO ("oídio") e o vault escreve ERRADO
   ("mildio", que não é palavra) ou nem traduziu ("mildew", "botrytis", "aphid",
   "thrips", "pythium"). Eu supus que só a direção 1 existia; era suposição, não
   medição, e quebrou na primeira pergunta que não era sobre poda.


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

    # --- PT → PT: o dono escreve CERTO e o vault escreve ERRADO -----------------
    # Descoberto no teste real de 2026-07-30: "como controlar o oídio?" foi para a
    # web e voltou com agricultura genérica, enquanto o vault tinha "Controle com
    # Leite", "Controle com Serenade", "AQ10 como Biofungicida". A atomização
    # traduziu *powdery mildew* como "mildio" (que não é palavra) e muitas vezes
    # nem traduziu. `melhor_dist` deu 0,16137 contra o limiar 0,16 — perdeu por um fio.
    #
    # Eu supus que a ponte fosse só EN→PT. Foi suposição, não medição, e quebrou no
    # primeiro contato. Medindo 26 termos que um cultivador digita, OITO têm lacuna:
    # o termo correto é raro no vault e o assunto está lá com outro nome — quase
    # sempre o inglês cru que a atomização deixou passar.
    "oidio": ("mildio", "mildew"),                      # 4 chunks x 14 + 22
    "botrite": ("botrytis", "mofo cinza"),              # 0 x 32 + 16
    "mofo cinzento": ("botrytis", "mofo cinza"),        # 2 x 32 + 16
    "pulgao": ("aphid",),                               # 3 x 21
    "tripes": ("thrips",),                              # 9 x 27
    "podridao radicular": ("pythium", "podridao de raizes"),   # 2 x 23 + 4
    "mosca branca": ("whitefly",),                      # 4 x 6
    "pistilos": ("estigma", "estigmas"),                # 6 x 75 + 68
    "pistilo": ("estigma", "estigmas"),
    # "estiolamento" tem 0 chunks e 30 páginas inglesas falam de `stretch`; o único
    # alvo que a medição sustentou foi "alongamento" (8). Fraco, mas 8 > 0.
    "estiolamento": ("alongamento",),
    # NÃO entraram, e por medição: "hermafrodita" (o vault já tem 6 e nenhum
    # sinônimo mediu melhor — "hermafroditismo" deu 1), "ácaros" (113, já alcança),
    # "lagarta" (29), "muda" (250), "transplante" (152), "estaca" (78). Ponte onde
    # não há lacuna só suja o aterramento.
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


def equivalencias(termos: str) -> list:
    """[(jargao, sinonimos)] das entradas que casam em `termos`. Puro."""
    if not termos:
        return []
    baixo = _sem_acento(termos)
    return [(j, s) for j, s in PONTE.items() if _RE[j].search(baixo)]


def nota_de_equivalencia(termos: str) -> str:
    """A equivalência dita ao GERADOR — não só à busca. Puro.

    MEDIDO ao vivo em 2026-07-30, e o mecanismo é o oposto do que eu supus. Em
    "como controlar o oídio?" a recuperação foi ÓTIMA: `controle_com_leite` (0,129),
    `controle_com_serenade` (0,133), `controle_com_bicarbonato` (0,136) entraram no
    contexto, nas posições 3, 5 e 7 de 34. E a resposta usou APENAS o átomo da
    posição 8 — o único cujo texto contém a palavra "oídio" — e declarou que "não há
    outros métodos de controle nos átomos fornecidos".

    O gerador ancora no termo LITERAL da pergunta e trata como fora de assunto o
    átomo que não o contém. É o anti-alucinação funcionando bem demais: ele se recusa
    a assumir por conta própria que "mildew" é "oídio". A mesma pergunta sobre
    BOTRITE saiu completa porque ali o átomo se chama "mofo cinza (Botrytis
    cinerea)" — a equivalência está DENTRO do texto, e ele não precisou supor nada.

    Por isso a ponte tem três destinos, não dois: aterramento léxico, embedding e
    agora o PROMPT. Sem este, a busca entrega o material certo e o gerador o ignora.
    """
    eqs = equivalencias(termos)
    if not eqs:
        return ""
    partes = ["; ".join(
        f"o que você chama de '{j}' aparece no material como {', '.join(s)}"
        for j, s in eqs)]
    return ("IMPORTANTE: " + partes[0] + " — são o MESMO assunto. Trate esses "
            "trechos como respondendo à pergunta e use o que eles trazem.")


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
