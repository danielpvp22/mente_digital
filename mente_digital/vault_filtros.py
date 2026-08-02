"""
Filtros do navegador do vault — pasta, origem e data. PURO/testável.

Pedido do dono (2026-08-02). O desenho abaixo saiu de MEDIR o vault dele, não de
supor, e a medição mudou duas decisões:

1. **A data útil NÃO é o `mtime`.** Medido: das 24.838 notas, ZERO tinham mtime
   acima de 30 dias — o vault inteiro foi reescrito por passadas de manutenção
   (malha, links, correção de átomos), então o `mtime` responde "quando um script
   passou por aqui", não "quando esta nota nasceu". O frontmatter tem
   `colhido_em` em **24.834 das 24.838** (100,0%), e é essa a data que o dono
   quer dizer com "de quando é isto".

2. **`origem` não é um campo do índice.** O Chroma guarda só `source`/`mtime`
   (rag.py:273); a origem vive no frontmatter do `.md` (atomos.py:310). E há
   **191 valores distintos** — inútil como lista suspensa. Mas eles caem em
   FAMÍLIAS que cobrem 100% do vault, medido:

       importado  12.668   (um .json por conversa exportada do Gemini)
       livro      10.763   (Livro '...' — a Cannabis Encyclopedia)
       sintese       815   (o ETL destilando a conversa)
       proativa      180   (a pesquisa de lacuna)
       conversa      161
       outro         128   (consolidações)
       temaquente    119   (a re-pesquisa do que o dono mais reusa)
       sem_origem      4   (Inbox de captura e afins, sem frontmatter)

   É a família que responde a pergunta real do dono — "isto veio de livro, de
   conversa minha, ou o assistente foi buscar sozinho?".
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

# A ordem é a da tela: as duas famílias grandes primeiro, depois o que o próprio
# assistente colheu (que é o que o dono mais audita), e o resto no fim.
FAMILIAS: tuple[str, ...] = (
    "livro", "importado", "sintese", "conversa", "proativa", "temaquente",
    "outro", "sem_origem",
)

ROTULOS: dict[str, str] = {
    "livro": "Livro",
    "importado": "Importado",
    "sintese": "Síntese da conversa",
    "conversa": "Conversa",
    "proativa": "Pesquisa proativa",
    "temaquente": "Tema quente",
    "outro": "Outro",
    "sem_origem": "Sem origem",
}

RAIZ = "(raiz)"                 # rótulo da nota solta na raiz do vault

# Só o cabeçalho interessa. 24 linhas cobrem com folga o maior frontmatter medido
# (figura de livro: 4 campos) e impede que uma nota gigante seja lida inteira.
_MAX_LINHAS_CABECALHO = 24
_CAMPOS = ("origem", "colhido_em")


def familia_de_origem(origem: str) -> str:
    """A família da nota a partir do texto cru de `origem:`. Pura.

    A ordem dos testes importa: "Livro '...'" vem antes do sufixo `.json` porque
    um título de livro poderia, em tese, terminar em `.json` — e o contrário não
    acontece.
    """
    valor = (origem or "").strip()
    if not valor:
        return "sem_origem"
    if valor.startswith("Livro"):
        return "livro"
    if valor.lower().endswith(".json"):
        return "importado"
    canonico = valor.lower()
    if canonico in ("sintese", "síntese"):
        return "sintese"
    if canonico == "proativa":
        return "proativa"
    if canonico == "conversa":
        return "conversa"
    if canonico == "temaquente":
        return "temaquente"
    return "outro"


def pasta_de(caminho_relativo: str) -> str:
    """A pasta de 1º nível de um caminho relativo ao vault. Pura.

    De 1º nível de propósito: o vault do dono tem CINCO (Importado_Gemini,
    Conhecimento_Novo, Figuras, Listas e a raiz) contra centenas de subpastas de
    obra. Cinco é uma lista suspensa; centenas é outro problema de busca.
    """
    partes = [p for p in caminho_relativo.replace("\\", "/").split("/") if p]
    return partes[0] if len(partes) > 1 else RAIZ


def ler_cabecalho(caminho: Path) -> dict:
    """`{"origem": ..., "colhido_em": ...}` lidos do frontmatter. Só o cabeçalho.

    Nunca levanta: nota ilegível (permissão, encoding, arquivo sumindo entre o
    `rglob` e o `open`) devolve os campos vazios e cai na família `sem_origem` —
    um filtro de navegação não pode derrubar o painel.
    """
    achados = {campo: "" for campo in _CAMPOS}
    try:
        with caminho.open("r", encoding="utf-8", errors="ignore") as fh:
            primeira = fh.readline()
            if primeira.strip() != "---":       # nota sem frontmatter
                return achados
            for _ in range(_MAX_LINHAS_CABECALHO):
                linha = fh.readline()
                if not linha or linha.strip() == "---":
                    break
                chave, sep, valor = linha.partition(":")
                if sep and chave.strip() in achados:
                    achados[chave.strip()] = valor.strip()
    except OSError:
        pass
    return achados


def passa(meta: dict, pasta: str = "", familia: str = "", desde: str = "") -> bool:
    """A nota sobrevive aos filtros ativos? Pura.

    `desde` é uma data ISO (`YYYY-MM-DD`) comparada LEXICALMENTE — em ISO isso é
    idêntico a comparar as datas, e evita parsear 24 mil strings para descartar
    quase todas.

    ⚠ Com filtro de data ligado, nota SEM `colhido_em` fica de fora. É a escolha
    honesta: a alternativa (deixar passar) faria "colhido nos últimos 7 dias"
    devolver as 4 notas cuja data ninguém conhece, e o dono não teria como saber.
    Quem quer ver essas escolhe a família `sem_origem`, que não filtra por data.
    """
    if pasta and meta.get("pasta") != pasta:
        return False
    if familia and meta.get("familia") != familia:
        return False
    if desde:
        colhido = (meta.get("colhido_em") or "")[:10]
        if len(colhido) != 10 or colhido < desde:
            return False
    return True


def descrever(caminho_relativo: str, cabecalho: dict) -> dict:
    """Junta caminho + cabeçalho no dicionário que `passa` e a tela consomem."""
    return {
        "pasta": pasta_de(caminho_relativo),
        "familia": familia_de_origem(cabecalho.get("origem", "")),
        "colhido_em": (cabecalho.get("colhido_em") or "")[:10],
        "origem": cabecalho.get("origem", ""),
    }


def contar_facetas(metas: Iterable[dict]) -> dict:
    """Contagem por pasta e por família — o que a tela usa para montar as listas.

    As opções vêm dos DADOS, não de uma constante: uma pasta nova no vault
    aparece sozinha, e uma família sem nenhuma nota não fica oferecida numa lista
    suspensa que devolveria zero resultados.
    """
    pastas: dict[str, int] = {}
    familias: dict[str, int] = {}
    for meta in metas:
        pastas[meta["pasta"]] = pastas.get(meta["pasta"], 0) + 1
        familias[meta["familia"]] = familias.get(meta["familia"], 0) + 1
    return {
        "pastas": [{"valor": k, "n": v}
                   for k, v in sorted(pastas.items(), key=lambda kv: -kv[1])],
        "familias": [{"valor": k, "rotulo": ROTULOS.get(k, k), "n": familias[k]}
                     for k in FAMILIAS if k in familias],
        "total": sum(pastas.values()),
    }
