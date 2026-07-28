"""Reparo de átomos QUEBRADOS a partir do job-fonte (PURO/testável).

O defeito (medido 2026-07-28 sobre a base inteira): o modelo de 4B estoura o
orçamento de saída no meio de um átomo e o arquivo fica com a frase pela metade
— 2.356 átomos de livro, 6,5% da base. A causa é o MODELO, não o lote (o 8B deu
0 truncados em 24 medições), então o conserto do FUTURO é atomizar com o 8B; o
que já está em disco precisa desta passada.

Duas regras que este módulo existe para impor:

- **Reparo é POR ÁTOMO, nunca re-atomização do capítulo.** O dedup de
  `_salvar_atomos` descartaria a versão nova como duplicata da pobre que já está
  no banco — re-atomizar seria trabalho de GPU para não mudar nada.
- **Só o CORPO é reescrito.** Título, Malha, tags e proveniência sobrevivem ao
  defeito (o truncamento come o FIM da saída) e são a identidade do átomo no
  grafo e no índice. Reescrevê-los trocaria um problema de corpo quebrado por um
  de identidade trocada — e a tag `#conhecimento_novo`, se já tiver sido
  promovida pelo uso, não pode voltar.

A triagem é tão importante quanto o reparo: dos 577 "quebrados" da Cannabis
Encyclopedia, 58 são notas de FIGURA (lista de imagens não termina em ponto — e
nunca esteve quebrada) e 60 já terminam certo depois de tirar o resíduo de
formatação. Mandar esses 118 para o LLM seria reescrever o que não está quebrado.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from mente_digital import idioma, textutils
from mente_digital.atomos import desembrulhar_angular

# --- categorias da triagem ---------------------------------------------------
FIGURA = "figura"        # nota de imagem: fora do reparo
OK = "ok"                # átomo íntegro
RESIDUO = "residuo"      # íntegro, mas com lixo de formatação — conserto SEM LLM
VAZIO = "vazio"          # corpo com <=3 palavras de conteúdo
TRUNCADO = "truncado"    # corpo termina no meio da frase
# As duas que pedem o LLM (as outras não pagam uma chamada de GPU).
PRECISAM_LLM = (VAZIO, TRUNCADO)

_FM_RE = re.compile(r"^---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_TITULO_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_TAG_LINHA_RE = re.compile(r"^\s*#[\w/\-]+(?:\s+#[\w/\-]+)*\s*$")
_TAGS_FIM_RE = re.compile(r"(?:\s+#[\w/\-]+)+\s*$")
# Cabeçalho SOLTO ('##' sem texto) e o '#' órfão que sobra de uma linha de tags
# cortada — resíduo do truncamento, não conteúdo.
_SO_CERQUILHA_RE = re.compile(r"^#{1,6}\s*$")
# '#' órfão no FIM da linha: sobra de uma linha de tags que o truncamento cortou
# ('... #zettelkasten_atomico #'). Sem tirá-lo antes, o _TAGS_FIM_RE não ancora e
# a linha inteira de tags fica no corpo.
_CERQUILHA_ORFA_FIM_RE = re.compile(r"\s+#+\s*$")
_MALHA_MARCA = "**Malha Neural:**"
_EMBED_IMAGEM = "![["
# Fim de frase LEGÍTIMO. ')' e '%' entram porque átomo de livro termina em
# unidade ou parêntese com frequência ("...(3,8 L)", "...umidade de 60%").
_FIM_DE_FRASE = (".", "!", "?", ":", ";", ")", "”", "\"", "%")
_MIN_PALAVRAS_CORPO = 4      # <=3 palavras de conteúdo = átomo vazio
_MIN_PALAVRAS_REPARO = 5     # saída do LLM menor que isto não é corpo, é ruído
_NADA = "nada"               # sentinela do prompt de reparo


@dataclass(frozen=True)
class Atomo:
    """As PARTES de um átomo, separadas para que só o corpo mude.

    `frontmatter` fica VERBATIM de propósito: além de `origem`/`colhido_em` ele
    às vezes carrega `titulo_original` (passada de tradução), e remontá-lo por
    `normalizar_atomo` apagaria o que aquela passada gravou.
    """
    frontmatter: str
    titulo: str
    corpo: str
    malha: str      # linha '**Malha Neural:** [[A]] [[B]]' (ou "")
    tags: str       # '#zettelkasten_atomico #conhecimento_novo' (ou "")


def separar(texto: str) -> Atomo:
    """Quebra a nota em (frontmatter, título, corpo, malha, tags). Puro/testável.

    Faz três limpezas que a triagem depende: descola a Malha grudada no fim de
    uma linha de corpo (58 átomos reais da Encyclopedia — a frase terminava
    certinho e o '**Malha Neural:**' na mesma linha fazia a régua achar que o
    corpo continuava), colhe as tags penduradas e joga fora cabeçalho vazio.
    """
    m = _FM_RE.match(texto)
    fm = m.group(0) if m else ""
    resto = texto[len(fm):]

    titulo = ""
    corpo: List[str] = []
    malha = ""
    tags: List[str] = []
    for ln in resto.splitlines():
        ln = ln.rstrip()
        s = ln.strip()
        if not s:
            if corpo and corpo[-1]:
                corpo.append("")
            continue
        # Malha: onde quer que esteja — linha própria ou colada no fim do corpo.
        if _MALHA_MARCA in ln:
            antes, _, depois = ln.partition(_MALHA_MARCA)
            if not malha:
                malha = f"{_MALHA_MARCA} {depois.strip()}".rstrip()
            ln, s = antes.rstrip(), antes.strip()
            if not s:
                continue
        ln = _CERQUILHA_ORFA_FIM_RE.sub("", ln)
        s = ln.strip()
        if not s:
            continue
        if _TAG_LINHA_RE.match(s):
            tags.extend(s.split())
            continue
        fim = _TAGS_FIM_RE.search(ln)
        if fim:
            tags.extend(fim.group().split())
            ln = ln[: fim.start()].rstrip()
            s = ln.strip()
            if not s:
                continue
        if _SO_CERQUILHA_RE.match(s):
            continue                      # '##' órfão: resíduo do truncamento
        if not titulo:
            mt = _TITULO_RE.match(s)
            if mt:
                titulo = mt.group(1).strip()
                continue
        corpo.append(desembrulhar_angular(ln))

    vistas: set = set()
    finais = [t for t in tags if not (t in vistas or vistas.add(t))]
    return Atomo(fm, titulo, "\n".join(corpo).strip(), malha, " ".join(finais))


def montar(atomo: Atomo, corpo: str) -> str:
    """Nota nova com o corpo trocado — tudo o mais intacto. Puro/testável."""
    partes = [atomo.frontmatter + f"## {atomo.titulo}"]
    corpo = corpo.strip()
    if corpo:
        partes.append(corpo)
    if atomo.malha:
        partes.append(atomo.malha)
    if atomo.tags:
        partes.append(atomo.tags)
    return "\n".join(partes) + "\n"


def palavras_de_conteudo(texto: str) -> int:
    """Palavras distintas com mais de 2 letras — a régua de átomo VAZIO.

    Distintas, e não o total, porque o corpo degenerado repete ('Enxofre em solo
    úmido' de um título quase igual). Régua de VAZIO, jamais de CURTO: 'A
    cobertura orgânica retém a umidade do solo' é um átomo correto de 6 palavras,
    e a régua antiga ('<12 palavras') marcava 33% da base como defeito.
    """
    return len({p for p in textutils.normaliza(texto).split() if len(p) > 2})


def classificar(atomo: Atomo) -> str:
    """FIGURA | VAZIO | TRUNCADO | OK — sobre o átomo JÁ separado."""
    corpo = atomo.corpo.strip()
    if _EMBED_IMAGEM in corpo or atomo.titulo.strip().lower().startswith("figura"):
        # Lista de figuras: '- ![[...webp]] — Nitrogen (N)' não termina em ponto
        # por natureza. Nunca esteve quebrada; mandá-la ao LLM seria inventar.
        return FIGURA
    if palavras_de_conteudo(corpo) < _MIN_PALAVRAS_CORPO:
        return VAZIO
    if corpo.endswith(_FIM_DE_FRASE):
        return OK
    return TRUNCADO


def tem_residuo(texto: str) -> bool:
    """O arquivo carrega LIXO de formatação (não conteúdo quebrado)?

    Marcas EXPLÍCITAS, e não um round-trip `montar(separar(t)) != t`: a versão
    por round-trip marcou 4.987 dos 5.900 átomos da Encyclopedia como resíduo,
    porque o arquivo em disco termina com uma linha em branco a mais (o
    `_salvar_atomos` escreve `bloco + "\\n"` sobre um bloco que já termina em
    "\\n"). Espaço em branco não é defeito, e reescrever 4.987 arquivos por causa
    dele é dano gratuito num vault sem git.
    """
    for ln in texto.splitlines():
        s = ln.strip()
        if not s:
            continue
        if _SO_CERQUILHA_RE.match(s) or _CERQUILHA_ORFA_FIM_RE.search(ln):
            return True                       # '##' órfão ou '#' pendurado
        if _MALHA_MARCA in ln and not s.startswith(_MALHA_MARCA):
            return True                       # Malha grudada no fim do corpo
        if s.startswith("<") and desembrulhar_angular(ln) != ln:
            return True                       # corpo ainda envolto em '<...>'
    return False


def triar(texto: str) -> Tuple[str, Atomo]:
    """Categoria + átomo separado. RESIDUO = íntegro, mas o arquivo tem lixo de
    formatação que `montar` já limpa (não paga chamada de LLM)."""
    atomo = separar(texto)
    cat = classificar(atomo)
    if cat == OK and tem_residuo(texto):
        return RESIDUO, atomo
    return cat, atomo


def consulta_do_atomo(atomo: Atomo) -> str:
    """A query que recupera o trecho-fonte do átomo.

    Título + o pedaço de corpo que sobrou: o corpo truncado é curto mas é o que
    tem os termos específicos da passagem (número, cultivar, composto). O
    embedding é multilíngue (e5), então uma consulta em PT recupera passagem em
    inglês — casamento léxico não serviria aqui, e medi-lo provou isso: a última
    palavra do átomo PT quase nunca aparece no texto EN do livro.
    """
    return f"{atomo.titulo}\n{atomo.corpo}".strip()


def juntar_trechos(rankeados: Sequence[Tuple[float, str]], orcamento: int) -> str:
    """Concatena os trechos mais similares até o teto de chars (protege o n_ctx).
    Sempre entrega ao menos o primeiro — um trecho grande demais é cortado, não
    descartado (sem trecho não há reparo)."""
    usados: List[str] = []
    resta = orcamento
    for _sim, trecho in rankeados:
        if not usados:
            usados.append(trecho[:orcamento])
            resta = orcamento - len(usados[0])
            continue
        if len(trecho) > resta:
            break
        usados.append(trecho)
        resta -= len(trecho)
    return "\n\n".join(usados)


def _sem_aspas(s: str) -> str:
    return s[1:-1].strip() if len(s) > 1 and s[0] in "\"“'" and s[-1] in "\"”'" else s


def limpar_saida(saida: str) -> str:
    """A resposta do LLM reduzida ao CORPO. Puro/testável.

    O prompt pede só o corpo, mas o modelo às vezes devolve o formato inteiro do
    átomo (é o que ele viu 5.900 vezes na ingestão). Descartar por formato seria
    jogar fora um reparo bom: aqui o Python impõe a forma, como em
    `normalizar_atomo`.
    """
    linhas: List[str] = []
    for ln in saida.strip().splitlines():
        s = ln.strip()
        if not s or _TAG_LINHA_RE.match(s) or _SO_CERQUILHA_RE.match(s):
            continue
        if _MALHA_MARCA in s:
            s = s.partition(_MALHA_MARCA)[0].strip()
            if not s:
                continue
        mt = _TITULO_RE.match(s)
        if mt:
            continue                       # '## Título' repetido: não é corpo
        fim = _TAGS_FIM_RE.search(s)
        if fim:
            s = s[: fim.start()].rstrip()
        # Aspas em volta da frase inteira: mesma família do '<...>' — marca de
        # citação que o modelo inventa ao copiar a fonte. Descascadas ANTES e
        # DEPOIS do angular porque as duas marcas se aninham nas duas ordens
        # ('"<frase>"' e '<"frase">').
        s = _sem_aspas(desembrulhar_angular(_sem_aspas(s)).strip())
        if s:
            linhas.append(s)
    return "\n".join(linhas).strip()


_FRASE_RE = re.compile(r"[^.!?;]+[.!?;]?")
# A régua POR FRASE, medida em 1.500 átomos PT reais da Encyclopedia: marca 62
# (4,1%), dos quais 17 de 18 amostrados são frase inglesa DE VERDADE dentro de um
# átomo que o detector por título não pega. Falso positivo real ≈ 0,2% da base, e
# custo NENHUM: recusar mantém o átomo original.
#
# Duas condições, porque a primeira sozinha não bastou: uma frase inglesa com um
# 'e' português no meio ("Top branches grow vertically taller where auxins are
# concentrated e inhibit lateral buds") tem port=1 e escapava do `port == 0`.
_MIN_PALAVRAS_FRASE = 5


def _tem_frase_sem_portugues(texto: str) -> bool:
    for f in _FRASE_RE.findall(texto):
        ing, port, n = idioma.contar_funcionais(f)
        if n >= _MIN_PALAVRAS_FRASE and (port == 0 or (ing >= 2 and ing > port)):
            return True
    return False


def sem_eco_do_titulo(corpo: str, titulo: str) -> str:
    """Tira a frase que só REPETE o título ('… Solução para deficiência de
    níquel.' no fim de um corpo sobre deficiência de níquel). Puro/testável.

    O modelo faz isso quando o trecho-fonte acaba antes de sustentar o título
    inteiro — ele fecha ecoando o enunciado. Não é fato novo, é ruído, e ruído
    entra no embedding igual a conteúdo.

    A PRIMEIRA frase é poupada mesmo quando ecoa: nela o eco costuma ser o
    sujeito da nota. Medido no dry-run — tirá-la do átomo 'Potassium hydroxide
    (KOH) é popular e eficaz para aumentar o pH' deixou o corpo começando em 'É
    também misturado com…', sem dizer o quê.
    """
    alvo = textutils.normaliza(titulo).strip(" .!?;:")
    if not alvo:
        return corpo
    frases = [f for f in _FRASE_RE.findall(corpo) if f.strip()]
    mantidas = [f for i, f in enumerate(frases)
                if i == 0 or textutils.normaliza(f).strip(" .!?;:") != alvo]
    return " ".join(f.strip() for f in mantidas).strip() or corpo


def em_pt_dominante(texto: str) -> bool:
    """O texto é português com termo técnico em inglês, ou é inglês disfarçado?

    Complementa `idioma.em_ingles`, que exige AUSÊNCIA de português e por isso
    deixa passar o corpo MISTO — visto no 1º dry-run com o 8B: "…muitas vezes
    softening cell walls. Top branches grow vertically…". A régua de lá é
    deliberadamente frouxa porque reescrever átomo bom é pior que deixar um ruim;
    aqui pode ser dura, porque recusar apenas MANTÉM o átomo original.

    Dominância, não presença: 'leaching', 'bud' e a sigla entre parênteses são
    ordem do dono (glossário do domínio) e não podem reprovar a tradução boa.
    """
    ing, port, _n = idioma.contar_funcionais(texto)
    if ing >= 2 and ing > port:
        return False
    # A dominância sozinha não basta: "Auxinas influenciam a divisão celular,
    # muitas vezes softening cell walls. Top branches grow vertically tall" tem
    # português de sobra no começo e escorrega para o inglês no fim. Quem pega
    # isso é a régua POR FRASE.
    return not _tem_frase_sem_portugues(texto)


def corpo_aceitavel(novo: str, atomo: Atomo) -> Tuple[bool, str]:
    """O corpo reparado pode substituir o quebrado? -> (ok, motivo da recusa).

    Na dúvida MANTÉM o original: um átomo pobre é menos dano que um átomo
    inventado, e este script escreve no vault do dono sem revisão humana por
    arquivo. Os motivos são devolvidos para o relatório do dry-run — recusa
    silenciosa esconderia justamente o caso que precisa de olho.
    """
    if not novo:
        return False, "saída vazia"
    if textutils.normaliza(novo).strip(".!?") == _NADA:
        return False, "modelo respondeu NADA (o trecho não sustenta o título)"
    if palavras_de_conteudo(novo) < _MIN_PALAVRAS_REPARO:
        return False, "curto demais para ser corpo"
    if textutils.fracao_cjk(novo) > 0.15:
        return False, "saiu em CJK"
    if idioma.em_ingles(novo):
        return False, "continuou em inglês"
    if not em_pt_dominante(novo):
        return False, "saída majoritariamente em inglês"
    if not novo.rstrip().endswith(_FIM_DE_FRASE):
        return False, "veio truncado de novo"
    # Só recusa o que não MUDA nada. Comparar sem a pontuação seria recusar o
    # reparo mais comum de todos: 74 dos alvos são frase completa a que só falta
    # o ponto final — devolver a mesma frase pontuada É o conserto, não um no-op.
    if novo.strip() == atomo.corpo.strip():
        return False, "idêntico ao original"
    return True, ""
