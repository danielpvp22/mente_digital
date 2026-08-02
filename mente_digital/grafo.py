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
from typing import Dict, List, Set, Tuple


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


def vizinhanca_local(
    por_conceito: Dict[str, Set[str]],
    conceitos_de: Dict[str, List[str]],
    idf,
    semente: str,
    saltos: int = 1,
    max_nos: int = 40,
    idf_min: float = 0.0,
    max_arestas: int = 0,
) -> dict:
    """O grafo LOCAL em volta de uma nota: nós e arestas a até `saltos` de distância.

    POR QUE LOCAL, e não "o grafo do vault": 24.850 notas não formam desenho legível
    em renderizador nenhum — vira mancha, em qualquer biblioteca. O que responde à
    pergunta do dono ("com o que esta nota conversa?") é a vizinhança de 1-2 saltos,
    que cabe na tela e se lê.

    ⚠ O CORTE POR IDF NÃO É ENFEITE, é o que separa grafo de novelo. Os conceitos da
    malha têm frequências absurdamente desiguais — [[IA]] aparece em milhares de
    átomos. Sem `idf_min`, o primeiro salto a partir de QUALQUER nota traria todo
    mundo que menciona o conceito-hub, e o desenho seria o mesmo para qualquer
    semente. É a mesma régua que `MalhaIndex.vizinhos` já aplica na expansão de
    contexto, e pela mesma razão: compartilhar um conceito RARO é evidência de
    vizinhança; compartilhar um genérico não é evidência de nada.

    Determinístico de propósito: `por_conceito` guarda `set`, cuja ordem varia entre
    execuções. A ordenação por `(-idf, source)` faz o mesmo vault desenhar sempre o
    mesmo grafo — sem isso o teste passaria só às vezes, e o dono veria a tela mudar
    sozinha ao reabrir.

    Devolve `{"nos": [...], "arestas": [...], "truncado": bool, "alcancados": int}`.
    `truncado` existe para a tela poder DIZER que cortou: um grafo que some com
    metade dos vizinhos calado é pior que um grafo que avisa.
    """
    if semente not in conceitos_de or max_nos <= 1:
        return {"nos": [], "arestas": [], "truncado": False, "alcancados": 0,
                "arestas_totais": 0}

    nos: Dict[str, int] = {semente: 0}          # source -> salto em que foi alcançado
    # (a, b) -> (peso, conceito). Duas notas podem compartilhar cinco conceitos; a
    # aresta é UMA, com o vínculo mais forte — cinco linhas paralelas não informam
    # mais e escondem o resto do desenho.
    arestas: Dict[Tuple[str, str], Tuple[float, str]] = {}
    fronteira = [semente]
    alcancados = 0
    truncado = False

    for salto in range(1, max(1, saltos) + 1):
        candidatos = []
        for origem in fronteira:
            for c in conceitos_de.get(origem, ()):
                peso = idf(c)
                if peso < idf_min or peso <= 0:
                    continue
                for destino in por_conceito.get(c, ()):
                    if destino == origem:
                        continue
                    candidatos.append((peso, c, origem, destino))
        # Mais forte primeiro; empate desfeito pelo nome, para ser reprodutível.
        candidatos.sort(key=lambda t: (-t[0], t[2], t[3], t[1]))
        alcancados += len({d for _, _, _, d in candidatos} - set(nos))
        nova_fronteira = []
        for peso, c, origem, destino in candidatos:
            if destino not in nos:
                if len(nos) >= max_nos:
                    truncado = True
                    continue
                nos[destino] = salto
                nova_fronteira.append(destino)
            par = (origem, destino) if origem < destino else (destino, origem)
            if peso > arestas.get(par, (0.0, ""))[0]:
                arestas[par] = (peso, c)
        fronteira = nova_fronteira
        if not fronteira:
            break

    # TETO DE ARESTAS — visto no navegador em 2026-08-02, e invisível em teste:
    # um tema denso (fotossíntese) traz 39 vizinhos que são TODOS ligados entre si,
    # ~700 arestas. O desenho vira um disco azul sólido. O número é verdadeiro; a
    # figura não informa nada. As do meio (as que tocam a SEMENTE) são preservadas
    # sempre — são a resposta à pergunta que abriu a tela.
    ordenadas = sorted(arestas.items(), key=lambda kv: (-kv[1][0], kv[0]))
    total_arestas = len(ordenadas)
    if 0 < max_arestas < total_arestas:
        da_semente = [e for e in ordenadas if semente in e[0]]
        resto = [e for e in ordenadas if semente not in e[0]]
        ordenadas = sorted((da_semente + resto)[:max_arestas],
                           key=lambda kv: (-kv[1][0], kv[0]))

    return {
        "nos": [{"id": s, "salto": n} for s, n in
                sorted(nos.items(), key=lambda kv: (kv[1], kv[0]))],
        "arestas": [{"de": a, "para": b, "conceito": c, "peso": round(p, 3)}
                    for (a, b), (p, c) in ordenadas],
        "truncado": truncado,
        "alcancados": alcancados,
        "arestas_totais": total_arestas,
    }


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
