"""
Descobridor de Conexões (G8, Onda 3) — PONTES sobre a malha de conceitos do vault.

Puro/testável: recebe os mapas da `MalhaIndex` (conceito->átomos e átomo->conceitos) e
não tem estado, I/O nem aleatoriedade. Uma nota é PONTE quando liga dois conceitos
ESTABELECIDOS (cada um presente em vários átomos) que quase nunca co-ocorrem — exatamente
o "sua nota X liga os temas Y e Z". Sem detecção de comunidades (não é necessária para
isto): a co-ocorrência dá o "eles vivem separados", e o compartilhamento no átomo dá a
ponte. Entrega é SOB DEMANDA (comando-mestre), nunca push.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Set


@dataclass
class Ponte:
    source: str          # átomo que faz a ponte (a nota)
    conceito_a: str      # os dois temas ligados (normalizados, como na malha)
    conceito_b: str
    surpresa: float      # 1 - sobreposição das VIZINHANÇAS de conceito (domínios disjuntos = 1.0)
    tamanho: int         # min(df_a, df_b) — desempate: entre igualmente surpreendentes, o maior tema


def _vizinhanca(por_conceito, conceitos_de, cache, c):
    """Conceitos que CO-OCORREM com `c` em algum átomo (o "domínio" dele na malha).
    Cacheado: um mesmo conceito participa de várias pontes candidatas."""
    v = cache.get(c)
    if v is None:
        v = set()
        for src in por_conceito.get(c, ()):
            v.update(conceitos_de.get(src, ()))
        v.discard(c)
        cache[c] = v
    return v


def descobrir_pontes(
    por_conceito: Dict[str, Set[str]],
    conceitos_de: Dict[str, List[str]],
    df_min: int,
    coocorrencia_max: int,
    limite: int,
) -> List[Ponte]:
    """As `limite` pontes mais SURPREENDENTES do vault.

    PONTE = uma nota liga dois conceitos com df >= `df_min` (temas ESTABELECIDOS, não
    menção solta) que co-ocorrem em <= `coocorrencia_max` átomos (quase nunca juntos).

    O ranking é por SURPRESA = 1 - Jaccard das vizinhanças de conceito dos dois temas.
    Vizinhanças DISJUNTAS (domínios diferentes: 'bateria' e 'tensorflow') = surpresa 1.0;
    vizinhanças que se sobrepõem (mesmo domínio: 'python' e 'vram') = surpresa baixa. Isto
    corrige o ranking ingênuo `min(df)/coocorrência`, que só surfava PARES DE TEMAS GRANDES
    (python↔vram) — estatisticamente triviais, não "temas que você mantém separados".
    Desempate por tamanho (temas maiores primeiro), depois nomes/source. Determinístico;
    dedup por PAR de conceitos (a mesma ponte pode nascer em 2 átomos quando coocorrência==2)."""
    cache: dict = {}
    candidatas: List[Ponte] = []
    for src, conceitos in conceitos_de.items():
        temas = sorted({c for c in conceitos if len(por_conceito.get(c, ())) >= df_min})
        for a, b in combinations(temas, 2):
            pa = por_conceito.get(a, set())
            pb = por_conceito.get(b, set())
            coocor = len(pa & pb)
            if coocor < 1 or coocor > coocorrencia_max:
                continue
            va = _vizinhanca(por_conceito, conceitos_de, cache, a)
            vb = _vizinhanca(por_conceito, conceitos_de, cache, b)
            uniao = len(va | vb)
            overlap = len(va & vb) / uniao if uniao else 0.0
            candidatas.append(Ponte(src, a, b, 1.0 - overlap, min(len(pa), len(pb))))

    candidatas.sort(key=lambda p: (-p.surpresa, -p.tamanho, p.conceito_a, p.conceito_b, p.source))
    vistos: set = set()
    out: List[Ponte] = []
    for p in candidatas:
        par = (p.conceito_a, p.conceito_b)
        if par in vistos:
            continue
        vistos.add(par)
        out.append(p)
        if len(out) >= limite:
            break
    return out
