"""Reorganiza o vault single-user na estrutura multiusuário: Acervo + Pessoal/<dono>.

    <vault>/Acervo/...            <- o TEXTO que veio de OBRA (comum, os quatro leem)
    <vault>/Pessoal/daniel/...    <- o que é do dono (conversa, web, listas, captura)
    <vault>/Figuras/...           <- TERCEIRA RAIZ: fica onde está (ver abaixo)

    python scripts/migrar_vault_multiusuario.py                 # dry-run (o DEFAULT)
    python scripts/migrar_vault_multiusuario.py --aplicar --fazer-backup

⚠ O CRITÉRIO É O `origem` DO FRONTMATTER, NUNCA A PASTA
-------------------------------------------------------
Medido em 2026-08-03: a regra de pasta erra nos DOIS sentidos, então nenhuma das
duas metades sobreviveria a ela.

  * `Figuras/` tem 1.868 notas, mas só 1.817 são de livro — as outras **51** são
    `Figuras/_web/`, imagens que o assistente baixou PARA O DONO. Pela pasta,
    elas iriam para o acervo comum, e cada usuário novo herdaria as buscas de
    imagem de outra pessoa.
  * O acervo por sua vez mora em DUAS pastas (`Conhecimento_Novo/` 8.904 +
    `Figuras/` 1.817). Classificar `Conhecimento_Novo/` inteiro como obra levaria
    junto as 1.769 notas de conversa/síntese do dono.

A classificação delega a `vault_filtros.familia_de_origem`, que já é o vocabulário
do projeto para isso (e já tem teste): família `livro` = acervo, o resto = pessoal.
Confirmado no vault real — a família `livro` dá 10.721, exatamente o total de notas
cujo `origem` começa por "Livro ".

⚠ `Figuras/` É UMA TERCEIRA RAIZ — NÃO ENTRA EM `Acervo/` NEM EM `Pessoal/`
---------------------------------------------------------------------------
Decisão do dono (2026-08-03), tomada em cima da medição abaixo. As notas do vault
embutem a imagem por CAMINHO A PARTIR DA RAIZ (`![[Figuras/<obra>/<arq>.webp]]`),
e é assim que o servidor a serve: `main._dentro_do_vault` resolve `raiz / caminho`
com `is_file()`. Medido: **3.714 embeds por caminho**, contra 79.905 links da Malha
que são por NOME. Mover `Figuras/` para dentro de `Acervo/` faria a rota
`/api/imagem/` devolver 404 em todos eles — a figura sumiria do chat.

As saídas eram reescrever 3.663 embeds (mexendo no conteúdo e, portanto, no mtime
de milhares de notas → reindexação) ou deixar a pasta na raiz. O dono escolheu
deixar: **zero link quebrado, zero mtime tocado, zero reindexação.**

Para efeito de INDEXAÇÃO, `Figuras/` é escopo do ACERVO (1.817 das 1.868 notas são
de obra). Quem consome isso é o `rag.py`, não este script.

⚠ O BURACO QUE ISSO ABRE, E POR QUE ELE É BARATO DE FECHAR
-----------------------------------------------------------
Com `Figuras/` compartilhada, as 51 notas de `Figuras/_web/` — imagens baixadas
PARA O DONO — ficariam visíveis aos outros três. Antes de decidir, medimos de onde
partem os embeds que apontam para lá:

    embeds por caminho que apontam para Figuras/_web/ ...... 51
    ...vindos de notas FORA de Figuras/_web/ ...............  0
    ...notas _web que embutem a PRÓPRIA imagem ............. 51 de 51

O subtree é AUTOCONTIDO: cada uma das 51 notas cita só a própria imagem, e nenhuma
outra nota do vault as referencia. Então mover `Figuras/_web/` para
`Pessoal/<dono>/Figuras_web/` e reescrever o embed custa mtime novo em **51 notas**,
não nas 3.663 — e cada nota reescreve a PRÓPRIA linha, que é verificável.

Os 2 `.webp` soltos de `_web` (nota já apagada) vão junto: `Figuras/_web` é
`settings.imagem_web_subpasta`, uma constante de configuração cujo conteúdo é, por
construção, imagem buscada para o dono. É a ÚNICA exceção à regra "não mova o que
não souber classificar" — e ela existe porque deixar imagem pessoal numa pasta
compartilhada é exatamente o vazamento que esta migração fecha.

⚠ MOVER, NUNCA COPIAR-E-APAGAR
------------------------------
`shutil.move` (rename dentro do mesmo volume) preserva o mtime. Isso não é
detalhe: o `VectorStore.sync` é incremental POR MTIME, e carimbar 25 mil notas com
data nova mandaria o Chroma reindexar o vault inteiro — horas de GPU para
reconstruir um índice que não mudou de conteúdo.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, NamedTuple, Set, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from mente_digital import backup, identidade, vault_filtros  # noqa: E402
from mente_digital.config import settings  # noqa: E402

BALDE_ACERVO = "acervo"
BALDE_PESSOAL = "pessoal"
BALDE_FIGURAS = "figuras (fica na raiz)"

# Onde as imagens que o assistente baixou da web para o dono passam a morar. Nome
# SEM barra de propósito: vira `Pessoal/<dono>/Figuras_web/`, uma pasta só, para o
# caminho reescrito ficar curto e legível dentro do embed.
DESTINO_WEB = "Figuras_web"

# `[[alvo]]` e `![[alvo]]`, parando no `|` (apelido) e no `#` (âncora), que não
# fazem parte do caminho resolvido. O grupo 1 guarda o `!` para a reescrita.
_RE_WIKILINK = re.compile(r"(!?)\[\[([^\]|#]+)")


class Layout(NamedTuple):
    """As pastas que definem o desenho. INJETADO (não lido de `settings` lá dentro)
    para que a lógica pura seja testável sem monkeypatch de configuração."""
    acervo: str          # "Acervo"
    pessoal: str         # "Pessoal"
    figuras: str         # "Figuras"      — a terceira raiz, fica onde está
    web: str             # "Figuras/_web" — o pedaço pessoal de dentro dela


def layout_atual() -> Layout:
    return Layout(settings.subpasta_acervo, settings.subpasta_pessoal,
                  settings.subpasta_figuras, settings.imagem_web_subpasta)


# --------------------------------------------------------------------------- #
# A lógica PURA: recebe texto/caminho, devolve o destino. Sem disco, sem settings.
# É o padrão do projeto (agenda.py, standby.py, atomos.py) — o que decide o
# destino de 25 mil notas do dono tem de ser testável sem um vault por perto.
# --------------------------------------------------------------------------- #
def classificar(origem: str) -> str:
    """`origem` do frontmatter -> balde. PURA.

    Delega a `vault_filtros.familia_de_origem` em vez de reimplementar o
    `startswith("Livro")`: duas cópias da mesma regra divergiriam no primeiro
    livro cuja `origem` mudasse de formato, e aí uma obra iria para o acervo na
    busca e para a pasta pessoal no disco.
    """
    return BALDE_ACERVO if vault_filtros.familia_de_origem(origem) == "livro" else BALDE_PESSOAL


def desembrulhar(relativo: str, lay: Layout) -> Tuple[str, str, str]:
    """Onde a nota está HOJE: `(balde, dono, caminho_interno)`. PURA.

    É o que torna o script idempotente. Sem desembrulhar, a 2ª execução leria o
    caminho já migrado `Pessoal/daniel/Listas/x.md` como se fosse um caminho de
    vault cru e proporia `Pessoal/daniel/Pessoal/daniel/Listas/x.md` — cada
    passada afundando o vault um nível.
    """
    partes = [p for p in relativo.replace("\\", "/").split("/") if p]
    if partes and partes[0] == lay.acervo:
        return BALDE_ACERVO, "", "/".join(partes[1:])
    # Exige TRÊS partes (`Pessoal/<dono>/<algo>`): com duas, `Pessoal/nota.md` seria
    # lido como dono="nota.md" e caminho interno VAZIO — a nota viraria um balde e
    # ficaria órfã para sempre. Nota solta em `Pessoal/` não é de ninguém e cai no
    # caminho de baixo, para ser migrada como qualquer outra.
    if len(partes) >= 3 and partes[0] == lay.pessoal:
        return BALDE_PESSOAL, partes[1], "/".join(partes[2:])
    return "", "", "/".join(partes)


def destino_de(relativo: str, origem: str, dono: str, lay: Layout) -> str:
    """Caminho relativo final da nota. PURA. Devolve o MESMO caminho se ela fica.

    A ordem dos três testes é a decisão inteira, e ela não é intercambiável:
    `Figuras/_web/` precisa ser testado ANTES de `Figuras/`, senão o pedaço
    pessoal seria absorvido pela regra "figura fica na raiz" e continuaria
    visível aos outros três usuários.
    """
    _balde, _dono_atual, interno = desembrulhar(relativo, lay)
    if interno.startswith(f"{lay.web}/"):
        return f"{lay.pessoal}/{dono}/{DESTINO_WEB}/{interno[len(lay.web) + 1:]}"
    if interno.startswith(f"{lay.figuras}/"):
        return interno                      # terceira raiz — ver o ⚠ do topo
    # O caminho INTERNO é preservado inteiro (`Conhecimento_Novo/obra/x.md` vira
    # `Acervo/Conhecimento_Novo/obra/x.md`): a migração muda de QUEM é a nota, não
    # como o vault é organizado por dentro — e um `mv` de prefixo é o que o dono
    # consegue desfazer na mão se mudar de ideia.
    if classificar(origem) == BALDE_ACERVO:
        return f"{lay.acervo}/{interno}"
    return f"{lay.pessoal}/{dono}/{interno}"


def link_quebra(alvo: str, movidos: Set[str]) -> bool:
    """Este wikilink deixa de resolver depois da migração? PURA.

    A régua sai de medir o vault, não de supor (2026-08-03):

    * **Sem barra** (79.905 links, a Malha inteira): o Obsidian resolve pelo NOME
      dentro do vault, e os 25.213 nomes de nota são ÚNICOS — medido, zero
      colisões. Um link por nome não pode ficar ambíguo por mudar de pasta,
      porque continua havendo exatamente um arquivo com aquele nome no mesmo
      vault. Mover é NEUTRO para eles.
    * **Com barra** (3.714): é caminho a partir da RAIZ do vault — é assim que
      `main._dentro_do_vault` resolve o que a rota `/api/imagem/` serve
      (`raiz / caminho`, com `is_file()`). Se o arquivo apontado se moveu, o
      caminho antigo some e o link quebra. Com `Figuras/` ficando na raiz, os
      únicos que se movem são os 51 de `_web` — e esses são reescritos.
    """
    if "/" not in alvo:
        return False
    return alvo.replace("\\", "/").lstrip("./") in movidos


def reescrever_embeds(texto: str, de: str, para: str) -> Tuple[str, int]:
    """Troca o prefixo `de` por `para` DENTRO dos wikilinks. PURA.

    Só dentro do `[[...]]`: o mesmo caminho aparece na prosa da nota e um
    busca-e-substitui cego reescreveria o corpo do átomo, não o link. Devolve
    `(texto_novo, quantos)` — o contador é o que prova, no relatório, que a
    reescrita fez o que disse ter feito.
    """
    trocas = 0

    def _troca(m: "re.Match[str]") -> str:
        nonlocal trocas
        alvo = m.group(2)
        if not alvo.startswith(de):
            return m.group(0)
        trocas += 1
        return f"{m.group(1)}[[{para}{alvo[len(de):]}"

    return _RE_WIKILINK.sub(_troca, texto), trocas


# --------------------------------------------------------------------------- #
# O lado SUJO: disco.
# --------------------------------------------------------------------------- #
class Nota(NamedTuple):
    caminho: Path
    relativo: str
    destino: str
    balde: str
    ja_no_lugar: bool
    irmaos: Tuple[Path, ...]   # .webp e afins que viajam junto (ver `indexar_irmaos`)


def _confirmar_sem_origem(caminho: Path) -> str:
    """"" se a nota REALMENTE não tem `origem`; a mensagem do erro se foi ilegível.

    `vault_filtros.ler_cabecalho` engole `OSError` e devolve os campos vazios —
    correto para um filtro de navegação, perigoso aqui: como "sem origem" é
    PESSOAL, uma nota ilegível viraria nota pessoal em SILÊNCIO, que é
    exatamente o modo de falha que o requisito "não classificados = zero" existe
    para pegar. Só as notas de origem vazia pagam esta 2ª leitura (4 no vault
    real), então a varredura das 25 mil continua com uma passada só.
    """
    try:
        with caminho.open("r", encoding="utf-8", errors="ignore") as fh:
            fh.readline()
        return ""
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def indexar_irmaos(vault: Path) -> Dict[Tuple[Path, str], List[Path]]:
    """Índice `(pasta, nome_sem_extensão) -> arquivos não-.md`, numa passada só.

    São os arquivos que viajam junto com a nota — hoje o `.webp` das notas de
    figura (medido: pareamento exato, 1 para 1). A nota é quem é indexada; o
    `.webp` é quem aparece na tela. Mover só um dos dois deixaria uma figura sem
    imagem ou uma imagem invisível.

    ⚠ Índice, e não um `glob` por nota, por DOIS motivos:
      * `Importado_Gemini/` tem 12.668 notas numa pasta só, e um glob por nota
        relista a pasta inteira toda vez — O(n²), medido em 312s de varredura
        contra 3,8s deste índice.
      * nome de átomo vem de título traduzido pelo LLM e contém `[`, `]`, `?` de
        verdade; via glob esses viram curinga e o `.webp` irmão não é encontrado
        — a figura ficaria para trás SEM ERRO NENHUM. Comparando strings o
        problema deixa de existir.
    """
    indice: Dict[Tuple[Path, str], List[Path]] = {}
    for p in vault.rglob("*"):
        if not p.is_file() or ".obsidian" in p.parts or p.suffix.lower() == ".md":
            continue
        indice.setdefault((p.parent, p.stem), []).append(p)
    return indice


def levantar(vault: Path, dono: str, lay: Layout
             ) -> Tuple[List[Nota], Counter, List[Tuple[str, str]]]:
    """Varre o vault e monta o plano. Devolve `(notas, censo, ilegiveis)`."""
    irmaos = indexar_irmaos(vault)
    notas: List[Nota] = []
    censo: Counter = Counter()
    ilegiveis: List[Tuple[str, str]] = []
    for md in sorted(vault.rglob("*.md")):
        if ".obsidian" in md.parts:
            continue
        relativo = md.relative_to(vault).as_posix()
        origem = (vault_filtros.ler_cabecalho(md).get("origem") or "").strip()
        if not origem:
            erro = _confirmar_sem_origem(md)
            if erro:
                ilegiveis.append((relativo, erro))
                continue
        destino = destino_de(relativo, origem, dono, lay)
        # O balde do CENSO é ONDE A NOTA VAI PARAR, não o que a `origem` diz: as
        # figuras de obra ficam na raiz, e contá-las como "acervo" faria o
        # relatório prometer um `Acervo/` que ninguém vai encontrar no disco.
        balde = BALDE_FIGURAS if destino.startswith(f"{lay.figuras}/") else classificar(origem)
        censo[balde] += 1
        acompanham = tuple(sorted(irmaos.get((md.parent, md.stem), ())))
        notas.append(Nota(md, relativo, destino, balde, destino == relativo, acompanham))
    return notas, censo, ilegiveis


def assets_web_soltos(vault: Path, notas: List[Nota], lay: Layout) -> List[Path]:
    """Os `.webp` de `Figuras/_web/` cuja nota já não existe — ver o ⚠ do topo.

    É a ÚNICA exceção à regra "não mova o que não souber classificar", e ela é
    deliberada: sem `origem` para ler, o único sinal é a pasta — mas aqui a pasta
    é `settings.imagem_web_subpasta`, uma constante cujo conteúdo é, por
    construção, imagem baixada para o dono. Deixá-las numa pasta compartilhada
    manteria aberto exatamente o vazamento que esta migração fecha.
    """
    raiz_web = vault / lay.web
    if not raiz_web.is_dir():
        return []
    acompanhados = {p for n in notas for p in n.irmaos}
    return sorted(p for p in raiz_web.rglob("*")
                  if p.is_file() and p.suffix.lower() != ".md" and p not in acompanhados)


def orfaos(vault: Path, notas: List[Nota], lay: Layout) -> List[Path]:
    """Arquivos que não são `.md` e não acompanham nenhuma nota.

    No vault real são 2.743 `.webp` de três obras já aposentadas por
    `arquivar_obra.py` (as notas saíram, as imagens ficaram) e 64 `.json` do
    export do Gemini. Eles NÃO são movidos: sem `origem` para ler, classificá-los
    seria chutar — e chutar aqui é publicar arquivo do dono no acervo comum, ou
    esconder figura de obra numa pasta pessoal. Ficam onde estão e são relatados.
    """
    acompanhados = {p for n in notas for p in n.irmaos}
    acompanhados.update(assets_web_soltos(vault, notas, lay))
    return sorted(p for arquivos in indexar_irmaos(vault).values()
                  for p in arquivos if p not in acompanhados)


def verificar_links(vault: Path, notas: List[Nota], lay: Layout
                    ) -> Tuple[Dict[str, int], List[Nota]]:
    """`(contagem, notas_a_reescrever)`. Ver `link_quebra` para a régua.

    Roda ANTES de mover, sobre o plano — é o que dá ao dono a chance de decidir
    com o vault ainda intacto. A lista de reescrita sai da MEDIÇÃO do texto, não
    da suposição de que só as 51 notas de `_web` se citam: se um dia uma nota de
    conversa embutir uma imagem `_web`, ela entra aqui sozinha.
    """
    movidos: Set[str] = set()
    for n in notas:
        if n.ja_no_lugar:
            continue
        movidos.add(n.relativo)
        for irmao in n.irmaos:
            movidos.add(irmao.relative_to(vault).as_posix())
    for solto in assets_web_soltos(vault, notas, lay):
        movidos.add(solto.relative_to(vault).as_posix())

    contas: Counter = Counter()
    reescrever: List[Nota] = []
    for n in notas:
        try:
            texto = n.caminho.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            contas["notas_ilegiveis"] += 1
            print(f"  ! não consegui ler para checar links: {n.relativo} ({exc})")
            continue
        cita_web = False
        for m in _RE_WIKILINK.finditer(texto):
            alvo = m.group(2).strip()
            contas["total"] += 1
            if "/" not in alvo:
                contas["por_nome_neutros"] += 1
            elif not link_quebra(alvo, movidos):
                contas["por_caminho_ok"] += 1
            elif alvo.replace("\\", "/").startswith(f"{lay.web}/"):
                contas["quebrados_web"] += 1     # reescrito logo abaixo
                cita_web = True
            else:
                contas["quebrados"] += 1         # sem conserto: o dono decide
        if cita_web:
            reescrever.append(n)
    return dict(contas), reescrever


def aplicar(vault: Path, notas: List[Nota], lay: Layout, dono: str,
            reescrever: List[Nota]) -> Dict[str, int]:
    """Move as notas (e seus irmãos) e reescreve os embeds de `_web`.

    `shutil.move` preserva o mtime — ver o ⚠ do topo. Erro de IO PROPAGA: uma
    migração que continua depois de falhar no meio deixa o vault em dois estados
    ao mesmo tempo, e é pior de consertar do que uma que parou e disse onde.
    """
    conta: Counter = Counter()
    a_reescrever = {n.relativo for n in reescrever}
    for n in notas:
        if n.ja_no_lugar:
            conta["puladas"] += 1
            continue
        for origem_arq in (n.caminho, *n.irmaos):
            sufixo = origem_arq.name[len(n.caminho.stem):]
            alvo = vault / (n.destino[: -len(".md")] + sufixo)
            alvo.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(origem_arq), str(alvo))
        conta["movidas"] += 1
        # A reescrita vem DEPOIS do move e mira o DESTINO: o arquivo de origem já
        # não existe. Só estas notas ganham mtime novo (51 no vault real, contra
        # as 3.663 que a outra estratégia custaria).
        if n.relativo in a_reescrever:
            destino = vault / n.destino
            texto, trocas = reescrever_embeds(
                destino.read_text(encoding="utf-8"),
                f"{lay.web}/", f"{lay.pessoal}/{dono}/{DESTINO_WEB}/")
            if trocas:
                destino.write_text(texto, encoding="utf-8")
                conta["reescritas"] += 1
                conta["embeds_reescritos"] += trocas

    for solto in assets_web_soltos(vault, notas, lay):
        alvo = vault / lay.pessoal / dono / DESTINO_WEB / solto.name
        alvo.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(solto), str(alvo))
        conta["assets_soltos_web"] += 1
    return dict(conta)


def _exigir_backup(fazer: bool) -> bool:
    """O vault é a ÚNICA cópia do conhecimento destilado (o dump bruto morre na
    atomização). Sem backup de hoje, este script NÃO mexe em nada."""
    destino = Path(settings.backup_dir)
    agora = datetime.now()
    zip_hoje = backup.caminho_do_dia(destino, agora)
    if zip_hoje.exists():
        print(f"backup de hoje: OK ({zip_hoje})")
        return True
    if not fazer:
        print(f"\nRECUSADO: não há backup de hoje em {destino}.")
        print("Rode de novo com --fazer-backup (ou gere o backup por fora).")
        return False
    print(f"gerando backup em {zip_hoje} ...")
    feito = backup.executar(
        vault_dir=Path(settings.caminho_obsidian), db_path=Path(settings.db_telemetria),
        env_path=RAIZ / ".env", destino_dir=destino, agora=agora,
        retencao=settings.backup_retencao)
    print(f"backup pronto: {feito} ({feito.stat().st_size / 1e6:.0f} MB)")
    return True


def _relatar_links(contas: Dict[str, int], reescrever: List[Nota], lay: Layout) -> int:
    """Imprime o bloco de wikilinks e devolve quantos ficam quebrados SEM conserto."""
    print("\nwikilinks:")
    print(f"  {contas.get('total', 0):6d}  total")
    print(f"  {contas.get('por_nome_neutros', 0):6d}  por NOME — mover é neutro "
          "(nomes de nota são únicos no vault)")
    print(f"  {contas.get('por_caminho_ok', 0):6d}  por CAMINHO, alvo NÃO se move "
          f"(inclui os embeds de `{lay.figuras}/`, que fica na raiz)")
    print(f"  {contas.get('quebrados_web', 0):6d}  por CAMINHO para `{lay.web}/` — "
          f"REESCRITOS em {len(reescrever)} nota(s)")
    residual = contas.get("quebrados", 0)
    print(f"  {residual:6d}  quebrados SEM conserto")
    return residual


def main() -> int:
    # O console do Windows é cp1252 e o vault tem acento em quase todo nome de
    # arquivo. Sem isto o relatório morre com UnicodeEncodeError no meio.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dono", default=identidade.DONO_PADRAO,
                    help=f"dono das notas pessoais (padrão: {identidade.DONO_PADRAO})")
    ap.add_argument("--dry-run", action="store_true",
                    help="explícito, mas é o DEFAULT — sem --aplicar nada é movido")
    ap.add_argument("--aplicar", action="store_true", help="move de verdade")
    ap.add_argument("--fazer-backup", action="store_true",
                    help="gera o backup do dia se ele ainda não existir")
    ap.add_argument("--aceitar-links-quebrados", action="store_true",
                    help="aplica mesmo com wikilinks quebrando (veja o relatório antes)")
    args = ap.parse_args()

    dono = identidade.normalizar(args.dono)
    lay = layout_atual()
    vault = Path(settings.caminho_obsidian)
    if not vault.is_dir():
        print(f"vault não encontrado: {vault}")
        return 1

    t0 = time.perf_counter()
    print(f"vault: {vault}\ndono:  {dono}\n")
    notas, censo, ilegiveis = levantar(vault, dono, lay)

    print(f"{len(notas)} nota(s) .md classificadas em {time.perf_counter() - t0:.1f}s")
    print(f"  {BALDE_ACERVO:22s} -> {lay.acervo}/            {censo[BALDE_ACERVO]:6d}")
    print(f"  {BALDE_PESSOAL:22s} -> {lay.pessoal}/{dono}/   {censo[BALDE_PESSOAL]:6d}")
    print(f"  {BALDE_FIGURAS:22s} -> {lay.figuras}/           {censo[BALDE_FIGURAS]:6d}")

    # Requisito do dono: isto TEM de ser zero. Se não for, o script para em vez de
    # adivinhar de quem é a nota que ele não conseguiu ler.
    if ilegiveis:
        print(f"\nPARADO: {len(ilegiveis)} nota(s) que não consegui LER — não vou chutar "
              "o dono delas:")
        for rel, erro in ilegiveis[:20]:
            print(f"  {rel}  ({erro})")
        return 2

    mover = [n for n in notas if not n.ja_no_lugar]
    print(f"\na mover: {len(mover)}   já no lugar (puladas): {len(notas) - len(mover)}")
    print(f"  destas, {sum(1 for n in mover if n.irmaos)} levam arquivo irmão junto "
          f"({sum(len(n.irmaos) for n in mover)} arquivo(s), .webp de figura)")
    soltos_web = assets_web_soltos(vault, notas, lay)
    if soltos_web:
        print(f"  + {len(soltos_web)} .webp solto(s) de `{lay.web}/` (nota já apagada) "
              f"-> `{lay.pessoal}/{dono}/{DESTINO_WEB}/`")

    fica = orfaos(vault, notas, lay)
    if fica:
        print(f"\n{len(fica)} arquivo(s) sem nota .md correspondente — FICAM onde estão:")
        for pasta, n in Counter(
                p.relative_to(vault).as_posix().rsplit("/", 1)[0] for p in fica
        ).most_common(5):
            print(f"  {n:6d}  {pasta}")

    contas, reescrever = verificar_links(vault, notas, lay)
    residual = _relatar_links(contas, reescrever, lay)

    if not args.aplicar:
        for n in mover[:3]:
            print(f"\n  {n.relativo}\n    -> {n.destino}")
        print("\n(dry-run: NADA foi movido — use --aplicar)")
        return 0

    if residual and not args.aceitar_links_quebrados:
        print(f"\nRECUSADO: {residual} wikilink(s) quebrariam sem conserto. "
              "Para aplicar assim mesmo, passe --aceitar-links-quebrados.")
        return 3
    if not _exigir_backup(args.fazer_backup):
        return 4

    conta = aplicar(vault, notas, lay, dono, reescrever)
    print(f"\n{conta.get('movidas', 0)} nota(s) movidas, {conta.get('puladas', 0)} pulada(s), "
          f"{conta.get('reescritas', 0)} nota(s) com embed reescrito "
          f"({conta.get('embeds_reescritos', 0)} embed(s)), "
          f"{conta.get('assets_soltos_web', 0)} .webp solto(s) de _web movido(s). "
          f"{time.perf_counter() - t0:.1f}s no total.")
    print("Rode `python scripts/reindexar.py` para o índice acompanhar os caminhos novos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
