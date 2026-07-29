"""Precedência entre OBRAS do vault (2026-07-27). Puro/testável, sem IO.

Por que existe: o mesmo autor publicou o mesmo assunto duas vezes, em épocas
diferentes. O vault já tem o Cervantes antigo ("Marijuana Horticulture");
a edição web de *The Cannabis Encyclopedia* é a atual, e a ordem do dono é
preferir SEMPRE a nova. Sem uma noção de precedência, o dedup do ETL faz o
OPOSTO do pedido — `_ja_no_banco` descarta o átomo que CHEGA quando já existe
um quase idêntico, e quem chega é justamente a edição nova. A obra nova seria
importada e silenciosamente jogada fora, fato a fato.

DELIBERADAMENTE ESTREITO: a precedência só é consultada onde duas notas JÁ
foram julgadas quase-duplicatas (dedup na escrita e dedup na leitura). Não é
peso de ranking global — isso mudaria a distância de toda pergunta do sistema
e faria uma obra preferida vencer um átomo melhor que fala de outra coisa.
Empate entre clones é o único lugar em que "qual edição?" é a pergunta certa.
"""
from __future__ import annotations

from typing import Sequence, Tuple


def marcas(config: str) -> Tuple[str, ...]:
    """`"A|B"` -> `("A", "B")`, na ordem de preferência (a 1ª vence).

    Formato de string separada por '|' porque é o que atravessa o `.env` sem
    cerimônia — o resto do projeto configura listas do mesmo jeito."""
    return tuple(p.strip() for p in (config or "").split("|") if p.strip())


def precedencia(origem: str, preferidas: Sequence[str]) -> int:
    """Quanto esta obra vale num EMPATE. 0 = obra sem preferência declarada.

    `origem` é a proveniência do frontmatter (`Livro 'X' — cap (p. 3-3)`), que
    o indexador já grava como metadado — então casar por SUBSTRING do título da
    obra dispensa cadastrar id de livro em lugar nenhum.

    A primeira marca da lista vale mais que a segunda: assim "prefira a edição
    web; na falta dela, a tradução revisada" é expressável sem código novo."""
    texto = (origem or "").casefold()
    total = len(preferidas)
    for i, marca in enumerate(preferidas):
        m = (marca or "").strip().casefold()
        if m and m in texto:
            return total - i
    return 0


def substitui(origem_nova: str, origem_antiga: str,
              preferidas: Sequence[str], substituiveis: Sequence[str]) -> bool:
    """A nota NOVA deve tomar o lugar da já conhecida, sendo as duas quase iguais?

    DUAS condições, e a segunda é o conserto de um defeito que rodou de verdade
    (2026-07-27): a 1ª versão perguntava apenas "a obra que chega é preferida?".
    Isso deixava QUALQUER nota a menos de `obras_dedup_dist_max` do átomo novo
    perder o lugar — e, das 532 aposentadorias da primeira passada, 39 não eram
    a edição velha: 35 do Raven (Biologia Vegetal), 3 do Amabis e uma nota de
    consolidação. Um átomo sobre estômatos cedeu vaga para uma enciclopédia de
    cannabis porque os dois falam de planta a 0,07 de distância.

    Substituir é uma relação DECLARADA entre duas obras, nunca inferida da
    distância: a que sai tem de estar na lista de obras SUPERADAS. Fora dessa
    relação nada é aposentado — vale o dedup estrito, que só descarta.

    Empate de precedência (inclusive 0 x 0) mantém o comportamento histórico:
    fica quem chegou primeiro."""
    if not preferidas or not substituiveis:
        return False
    if not casa_alguma(origem_antiga, substituiveis):
        return False
    return precedencia(origem_nova, preferidas) > precedencia(origem_antiga, preferidas)


def casa_alguma(origem: str, marcas_: Sequence[str]) -> bool:
    """A proveniência menciona alguma das marcas? (substring, sem caixa)"""
    texto = (origem or "").casefold()
    return any(m.strip().casefold() in texto for m in marcas_ if m and m.strip())
