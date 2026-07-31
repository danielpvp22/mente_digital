"""ACERVO de imagem da web: a Malha sai do TERMO BUSCADO, não do título da página.

O defeito (2026-07-31, print do Obsidian do dono). Uma foto de **camaleão** colhida
da web estava arquivada assim::

    origem: Web — busca de imagem por 'camaleão?'
    ## Características, tipos, cuidados e muito mais do camaleão
    **Malha Neural:** [[Características]] [[Cuidados]] [[Tipos]] [[Cultivo]]

São dois defeitos somados, e nenhum é cosmético:

**(A) A Malha não descreve a imagem.** `Características`, `Cuidados` e `Tipos` são
as palavras do TÍTULO da página — isca de clique escrita por um estranho para vender
o clique, não para descrever a foto. E `[[Cultivo]]` é contaminação do vault, que é
dominado por cultivo de cannabis. O risco é de RECUPERAÇÃO: a nota entra no índice
vetorial com `#conhecimento_novo`, e um camaleão marcado `[[Cultivo]]` vira
candidato a ser anexado como FIGURA numa pergunta de cultivo — a mesma família da
foto de aranha que recebeu `[[Detecção de Objetos]]`.

Causa medida (não suposta), nas 40 notas de `Figuras/_web/` conferidas contra o
backup `dados/backups/links_20260731_034923/`: quem escreveu essa Malha NÃO foi o
gerador — foi a passada de `scripts/melhorar_links.py`, que reescreveu as 40. O
original de todas era `[[<Termo>]] [[Imagem da Web]]`. Duas engrenagens do script,
somadas, produzem o estrago por construção:
  1. `escolher_candidatos` oferece `idx.top_global` a TODA nota (Cannabis, Cultivo,
     YOLO, Controle, Detecção de Objetos, ...), e como o corpo da nota é só a isca,
     nada casa lexicalmente e o hub sobra no topo — 23 das 40 saíram com um hub;
  2. `mesclar` descarta link atual com frequência 1, e o termo buscado é solteiro
     por natureza (só esta nota fala de camaleão) — 26 das 40 perderam o termo.

**(B) A pontuação viajou até o buscador.** `busca de imagem por 'camaleão?'` — o '?'
da pergunta foi ao Bing e depois virou o nó `[[Camaleão?]]` no grafo. Junto dele iam
os verbos de ORDEM ("mostra girafa", "busca mofo branco"): procurar a palavra
'mostra' na web fez o gate léxico casar "Timelapse MOSTRA tempestade de neve" pelo
VERBO, não pelo tema.

Nada aqui toca rede, GPU ou o vault real — é tudo função pura sobre texto.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mente_digital import imagem_web, rag, reparo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import melhorar_links as ml  # noqa: E402

# O candidato REAL que o dono viu na tela (título de isca incluído).
CANDIDATO_CAMALEAO = {
    "title": "Características, tipos, cuidados e muito mais do camaleão",
    "url": "https://pt.postposmo.com/camaleão-2/",
    "source": "Bing",
}


# --- (B) a limpeza do pedido -------------------------------------------------

@pytest.mark.parametrize("bruto, limpo", [
    ("camaleão?", "camaleão"),                       # o caso do print do dono
    ("capivara?", "capivara"),
    ("dragão-de-komodo?", "dragão-de-komodo"),       # o hífen é do termo, fica
    ("urso-polar?", "urso-polar"),
    ("placa RTX 3080?", "placa RTX 3080"),
    ("mostra girafa", "girafa"),                     # verbo de ordem: some
    ("busca mofo branco", "mofo branco"),
    ("procura tricoma", "tricoma"),
    ("pesquisa tricoma âmbar", "tricoma âmbar"),
    ("mostra uma foto de polvo", "foto de polvo"),   # verbo + artigo órfão
    ("hidroponia", "hidroponia"),                    # já limpo: não mexe
    ("pH solo", "pH solo"),
])
def test_limpar_pedido_tira_o_que_e_do_PEDIDO_e_nao_do_ASSUNTO(bruto, limpo):
    """Casos colhidos das 40 notas já gravadas, não inventados.

    O '?' e o verbo de ordem pertencem à FRASE do dono, não ao tema da foto. Sem
    esta limpeza os dois viajavam até o buscador e depois viravam nó do grafo
    (`[[Camaleão?]]`, `[[Mostra Girafa]]`) — conceitos que nenhuma outra nota do
    vault escreveria jamais, logo conceitos que não conectam nada.
    """
    assert imagem_web.limpar_pedido(bruto) == limpo


def test_limpar_pedido_e_idempotente():
    """Roda no chamador E dentro da `nota_do_acervo` (defesa em profundidade);
    passar duas vezes não pode comer mais uma palavra a cada passada."""
    uma = imagem_web.limpar_pedido("mostra pinguim-imperador?")
    assert uma == "pinguim-imperador"
    assert imagem_web.limpar_pedido(uma) == uma


def test_limpar_pedido_nunca_devolve_vazio():
    """Guarda deliberada, mesma cautela do `casa_com_o_pedido`: sem termo o gate
    léxico deixa passar QUALQUER imagem. Limpar demais é pior que não limpar."""
    assert imagem_web.limpar_pedido("?") == "?"
    assert imagem_web.limpar_pedido("mostra") == "mostra"     # verbo sozinho: fica
    assert imagem_web.limpar_pedido("   ") == ""


# --- (A) o conceito nasce do termo, não do título ----------------------------

@pytest.mark.parametrize("termo, conceito", [
    ("camaleão?", "Camaleão"),
    ("mostra girafa", "Girafa"),
    ("tricoma âmbar", "Tricoma âmbar"),
    ("pH solo", "pH solo"),                     # `.title()` fazia 'Ph Solo'
    ("lâmpada LED cultivo", "Lâmpada LED cultivo"),   # ...e 'Lâmpada Led Cultivo'
    ("RTX 3080", "RTX 3080"),
    ("", "Imagem"),
])
def test_conceito_do_termo_nao_estraga_sigla_nem_inicial_minuscula(termo, conceito):
    """`.title()` criava nó NOVO ao lado do que o vault já escreve certo: 'pH solo'
    virava `[[Ph Solo]]` enquanto `[[pH]]` já existia na base. Maiúscula só na
    inicial, e só quando a primeira palavra é toda minúscula."""
    assert imagem_web.conceito_do_termo(termo) == conceito


def test_a_malha_da_nota_vem_do_termo_e_NAO_do_titulo_de_isca():
    """A regressão do print do dono, byte a byte.

    O título da página traz 'Características, tipos, cuidados' — três palavras que
    o gerador antigo não usava, mas que o `melhorar_links` transformava em conceito.
    Aqui se prova o lado do GERADOR: a Malha que ele escreve fala do camaleão e de
    mais nada.
    """
    nota = imagem_web.nota_do_acervo(
        "Figuras/_web/861198b2.webp", CANDIDATO_CAMALEAO, "camaleão?", "2026-07-31")
    malha = next(ln for ln in nota.splitlines() if ln.startswith("**Malha Neural:**"))

    assert malha == "**Malha Neural:** [[Camaleão]] [[Imagem da Web]]"
    # O que o dono viu na tela e não pode voltar:
    for veneno in ("[[Características]]", "[[Cuidados]]", "[[Tipos]]", "[[Cultivo]]"):
        assert veneno not in nota
    # E o '?' não sobrevive em lugar nenhum da nota (nem na proveniência).
    assert "camaleão?" not in nota
    assert "origem: Web — busca de imagem por 'camaleão'" in nota


def test_o_titulo_de_isca_continua_visivel_como_TITULO():
    """Não se apaga a informação — só se recusa a promovê-la a conceito. O título
    da página é proveniência útil (o dono sabe de onde veio); o que ele não pode
    ser é a etiqueta sob a qual a foto é arquivada e recuperada."""
    nota = imagem_web.nota_do_acervo(
        "Figuras/_web/861198b2.webp", CANDIDATO_CAMALEAO, "camaleão?", "2026-07-31")

    assert "## Características, tipos, cuidados e muito mais do camaleão" in nota
    assert "origem_site: Bing" in nota


# --- (A) o script não passa o rolo nessa nota --------------------------------

def _nota_do_texto(texto: str, rel: str, e_figura: bool = True) -> ml.Nota:
    """Constrói a `ml.Nota` pelo MESMO caminho do `varrer` (frontmatter + separar).

    De propósito: assim o teste exercita o contrato REAL entre os dois módulos, e
    não uma string que eu escrevi à mão para casar comigo mesmo.
    """
    origem = (rag.parse_frontmatter(texto).get("origem") or "").strip()
    atomo = reparo.separar(texto)
    corpo = ml.corpo_de_figura(atomo.corpo) if e_figura else atomo.corpo
    return ml.Nota(Path(rel), rel, origem, atomo.titulo, corpo, atomo.malha,
                   ml._conceitos_da_malha(atomo.malha), e_figura,
                   ml.dominio_da_origem(origem))


def _args(**kw) -> SimpleNamespace:
    base = dict(so_figuras=True, incluir_figuras=True, so_sem_malha=False,
                so_desconectadas=False, limite=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_isca_da_web_reconhece_a_nota_que_o_gerador_escreve():
    """ANTI-DERIVA: o reconhecimento é pelo `origem:` que a `nota_do_acervo` grava,
    e a string mora num lugar só (`imagem_web.ORIGEM_PREFIXO`). Se alguém mudar o
    texto da proveniência lá, este teste cai aqui — em vez de o script voltar a
    passar o rolo em silêncio, que é como o defeito nasceu."""
    texto = imagem_web.nota_do_acervo(
        "Figuras/_web/861198b2.webp", CANDIDATO_CAMALEAO, "camaleão?", "2026-07-31")

    assert ml._isca_da_web(_nota_do_texto(texto, "Figuras/_web/861198b2.md"))


def test_a_nota_de_imagem_da_web_NAO_vai_ao_modelo():
    """O coração da correção (A).

    Sem esta exclusão a nota é escolhida, o prompt recebe só o título de isca, e
    `escolher_candidatos` oferece os hubs globais do vault — foi assim que um
    camaleão ganhou `[[Cultivo]]`. Removendo o filtro do `_selecionar`, este teste
    falha: a nota volta para a lista de alvos.
    """
    texto = imagem_web.nota_do_acervo(
        "Figuras/_web/861198b2.webp", CANDIDATO_CAMALEAO, "camaleão?", "2026-07-31")
    nota = _nota_do_texto(texto, "Figuras/_web/861198b2.md")

    assert ml._selecionar([nota], set(), _args(), set()) == []


def test_a_figura_do_LIVRO_continua_entrando(monkeypatch):
    """O contrapeso: a exclusão tem de ser CIRÚRGICA.

    A passada das figuras de livro é trabalho validado (3,70 conceitos
    compartilhados por nota) e não pode morrer junto. Uma figura da Encyclopedia,
    com legenda de verdade, segue sendo alvo.
    """
    texto = (
        "---\n"
        "origem: The Cannabis Encyclopedia (Jorge Cervantes) — página 55\n"
        "tipo: figura\n"
        "---\n"
        "## Figura 6 — tricomas maduros\n"
        "Tricomas capitados de cabeça leitosa no ápice da flor, prontos para colheita.\n"
        "![[Figuras/cervantes/p0055_f6.webp]]\n"
        "**Malha Neural:** [[Tricomas]] [[Colheita]]\n"
        "#zettelkasten_atomico\n"
    )
    nota = _nota_do_texto(texto, "Figuras/cervantes/p0055_f6.md")

    assert not ml._isca_da_web(nota)
    assert ml._selecionar([nota], set(), _args(), set()) == [nota]


def test_uma_nota_sem_origem_nenhuma_nao_e_confundida_com_isca():
    """Guarda de borda: `origem` vazio não pode casar o prefixo por acidente (um
    `startswith('')` silencioso excluiria o vault inteiro da passada)."""
    texto = ("## Nota do dono\n"
             "Escrita à mão, sem frontmatter.\n"
             "**Malha Neural:** [[Cultivo]]\n"
             "#zettelkasten_atomico\n")

    assert not ml._isca_da_web(_nota_do_texto(texto, "Notas/minha.md", e_figura=False))
