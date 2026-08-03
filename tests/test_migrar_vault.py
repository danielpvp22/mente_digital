"""Testes da migração do vault para a estrutura multiusuário.

O vault sintético abaixo é o vault REAL em miniatura — cada peça existe porque
uma medição de 2026-08-03 mostrou que a regra ingênua erra nela:

  * átomo de obra em `Conhecimento_Novo/` e figura da MESMA obra em `Figuras/`
    (o acervo mora em DUAS pastas — regra de pasta perderia as 1.868 figuras);
  * `Figuras/_web/` (imagem que o assistente baixou PARA O DONO — mora na pasta
    de figuras e mesmo assim é PESSOAL);
  * nota sem `origem` (Inbox de captura — é do dono, não do acervo comum);
  * par `.md` + `.webp` (mover só um deixa figura sem imagem);
  * embed por CAMINHO (`![[Figuras/...]]`), que é como o servidor resolve a
    imagem e o motivo de `Figuras/` ficar na raiz.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

import migrar_vault_multiusuario as mig  # noqa: E402

ACERVO, PESSOAL, FIGURAS = mig.BALDE_ACERVO, mig.BALDE_PESSOAL, mig.BALDE_FIGURAS
DONO = "daniel"
LAY = mig.Layout(acervo="Acervo", pessoal="Pessoal", figuras="Figuras", web="Figuras/_web")

ORIGEM_LIVRO = "Livro 'The Cannabis Encyclopedia (Jorge Cervantes)' — Soil (p. 1-1)"
ORIGEM_WEB = "Web — busca de imagem por 'capivara'"


def _nota(caminho: Path, origem: str | None, corpo: str = "conteúdo") -> Path:
    """Escreve uma nota com (ou sem) frontmatter, como o vault real."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    cabecalho = f"---\norigem: {origem}\ncolhido_em: 2026-07-27\n---\n" if origem else ""
    caminho.write_text(f"{cabecalho}## Título\n{corpo}\n", encoding="utf-8")
    return caminho


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """O vault real em miniatura — ver o docstring do módulo."""
    v = tmp_path / "Cerebro_Digital"
    _nota(v / "Conhecimento_Novo" / "obra" / "atomo_livro.md", ORIGEM_LIVRO)
    _nota(v / "Conhecimento_Novo" / "Sintese_esp32.md", "Sintese",
          "**Malha Neural:** [[Microcontrolador]] [[IoT]]")
    _nota(v / "Importado_Gemini" / "conversa.md", "Assunto-Qualquer.json")
    _nota(v / "Listas" / "Lista_compras.md", None)          # sem origem = PESSOAL
    _nota(v / "Inbox_Captura.md", None)                     # idem, na raiz
    # Figura de OBRA: par .md + .webp, com o embed por CAMINHO que o vault real usa.
    fig = v / "Figuras" / "obra" / "obra_p0001_f1.md"
    _nota(fig, ORIGEM_LIVRO, "![[Figuras/obra/obra_p0001_f1.webp]]")
    fig.with_suffix(".webp").write_bytes(b"WEBPFAKE")
    # Figura que o assistente baixou da WEB para o dono: mora em Figuras/, é PESSOAL.
    web = v / "Figuras" / "_web" / "abc123.md"
    _nota(web, ORIGEM_WEB, "![[Figuras/_web/abc123.webp]]")
    web.with_suffix(".webp").write_bytes(b"WEBPFAKE")
    return v


@pytest.fixture
def apontar(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Aponta o script para o vault sintético (e não para o vault do dono)."""
    monkeypatch.setattr(mig.settings, "caminho_obsidian", str(vault))
    monkeypatch.setattr(mig.settings, "subpasta_acervo", LAY.acervo)
    monkeypatch.setattr(mig.settings, "subpasta_pessoal", LAY.pessoal)
    monkeypatch.setattr(mig.settings, "subpasta_figuras", LAY.figuras)
    monkeypatch.setattr(mig.settings, "imagem_web_subpasta", LAY.web)
    return vault


def _planejar(vault: Path):
    return mig.levantar(vault, DONO, LAY)


# --------------------------------------------------------------------------- #
# Classificação — a parte PURA
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("origem, esperado", [
    (ORIGEM_LIVRO, ACERVO),
    ("Livro 'Outro' — cap", ACERVO),
    ("Sintese", PESSOAL),
    ("Conversa", PESSOAL),
    ("Proativa", PESSOAL),
    ("TemaQuente", PESSOAL),
    ("Assunto-Qualquer.json", PESSOAL),
    (ORIGEM_WEB, PESSOAL),
])
def test_classifica_pela_origem(origem: str, esperado: str) -> None:
    assert mig.classificar(origem) == esperado


def test_nota_sem_origem_e_pessoal() -> None:
    """O que o DONO escreveu (captura, listas) não vai para o acervo comum."""
    assert mig.classificar("") == PESSOAL
    assert mig.classificar("   ") == PESSOAL


# --------------------------------------------------------------------------- #
# `Figuras/` é a TERCEIRA RAIZ — decisão do dono (2026-08-03)
# --------------------------------------------------------------------------- #
def test_figuras_de_obra_nao_se_move() -> None:
    """Os 3.663 embeds `![[Figuras/<obra>/...]]` são caminho a partir da RAIZ do
    vault; mover a pasta faria /api/imagem/ devolver 404 em todos eles."""
    rel = "Figuras/obra/obra_p0001_f1.md"
    assert mig.destino_de(rel, ORIGEM_LIVRO, DONO, LAY) == rel


def test_figura_web_sai_de_figuras_para_a_pasta_do_dono() -> None:
    """`Figuras/_web/` é testado ANTES de `Figuras/`: senão o pedaço pessoal seria
    absorvido pela regra "figura fica na raiz" e continuaria visível aos outros."""
    assert mig.destino_de("Figuras/_web/abc123.md", ORIGEM_WEB, DONO, LAY) == (
        "Pessoal/daniel/Figuras_web/abc123.md")


def test_censo_do_vault_sintetico(apontar: Path) -> None:
    _notas, censo, ilegiveis = _planejar(apontar)
    assert censo[ACERVO] == 1          # só o átomo de texto; a figura fica na raiz
    assert censo[PESSOAL] == 5         # sintese, gemini, lista, inbox, figura _web
    assert censo[FIGURAS] == 1         # a figura de obra, que não se move
    assert ilegiveis == []             # requisito: não-classificados = zero


def test_aplicar_deixa_figuras_de_obra_intactas(apontar: Path) -> None:
    notas, _c, _i = _planejar(apontar)
    contas, reescrever = mig.verificar_links(apontar, notas, LAY)
    mig.aplicar(apontar, notas, LAY, DONO, reescrever)

    fig = apontar / "Figuras" / "obra" / "obra_p0001_f1.md"
    assert fig.is_file() and fig.with_suffix(".webp").is_file()
    # e o embed continua apontando para o mesmo caminho de sempre
    assert "![[Figuras/obra/obra_p0001_f1.webp]]" in fig.read_text(encoding="utf-8")
    assert contas.get("quebrados", 0) == 0


# --------------------------------------------------------------------------- #
# Destino e idempotência — a parte PURA
# --------------------------------------------------------------------------- #
def test_desembrulhar_reconhece_o_que_ja_foi_migrado() -> None:
    assert mig.desembrulhar("Acervo/Conhecimento_Novo/x.md", LAY) == (
        ACERVO, "", "Conhecimento_Novo/x.md")
    assert mig.desembrulhar("Pessoal/daniel/Listas/x.md", LAY) == (
        PESSOAL, "daniel", "Listas/x.md")
    assert mig.desembrulhar("Conhecimento_Novo/x.md", LAY) == ("", "", "Conhecimento_Novo/x.md")


def test_pessoal_sem_dono_nao_conta_como_migrado() -> None:
    """`Pessoal/nota.md` solto não é de ninguém — tratá-lo como já-migrado
    deixaria a nota órfã para sempre."""
    assert mig.desembrulhar("Pessoal/nota.md", LAY) == ("", "", "Pessoal/nota.md")


def test_destino_preserva_o_caminho_interno() -> None:
    assert mig.destino_de("Conhecimento_Novo/o/x.md", ORIGEM_LIVRO, DONO, LAY) == (
        "Acervo/Conhecimento_Novo/o/x.md")
    assert mig.destino_de("Listas/x.md", "", DONO, LAY) == "Pessoal/daniel/Listas/x.md"


# --------------------------------------------------------------------------- #
# Reescrita dos embeds de `_web` — a parte PURA
# --------------------------------------------------------------------------- #
def test_reescrever_embeds_so_dentro_do_wikilink() -> None:
    """O mesmo caminho aparece na prosa da nota; um busca-e-substitui cego
    reescreveria o corpo do átomo, não o link."""
    texto = ("veja Figuras/_web/abc.webp na prosa\n"
             "![[Figuras/_web/abc.webp]]\n")
    novo, trocas = mig.reescrever_embeds(texto, "Figuras/_web/", "Pessoal/daniel/Figuras_web/")
    assert trocas == 1
    assert "![[Pessoal/daniel/Figuras_web/abc.webp]]" in novo
    assert "veja Figuras/_web/abc.webp na prosa" in novo      # a prosa fica intacta


def test_reescrever_embeds_nao_toca_os_outros_links() -> None:
    texto = "[[Microcontrolador]] ![[Figuras/obra/x.webp]]"
    novo, trocas = mig.reescrever_embeds(texto, "Figuras/_web/", "Pessoal/daniel/Figuras_web/")
    assert (novo, trocas) == (texto, 0)


def test_embed_web_e_reescrito_no_disco(apontar: Path) -> None:
    notas, _c, _i = _planejar(apontar)
    _contas, reescrever = mig.verificar_links(apontar, notas, LAY)
    assert [n.relativo for n in reescrever] == ["Figuras/_web/abc123.md"]

    conta = mig.aplicar(apontar, notas, LAY, DONO, reescrever)
    assert conta["reescritas"] == 1 and conta["embeds_reescritos"] == 1
    destino = apontar / "Pessoal" / "daniel" / "Figuras_web" / "abc123.md"
    assert "![[Pessoal/daniel/Figuras_web/abc123.webp]]" in destino.read_text(encoding="utf-8")
    # a imagem foi junto, senão o link recém-escrito já nasceria quebrado
    assert (apontar / "Pessoal/daniel/Figuras_web/abc123.webp").is_file()
    assert not list((apontar / "Figuras" / "_web").glob("*"))     # o subtree esvaziou


def test_webp_solto_de_web_vai_junto(apontar: Path) -> None:
    """Imagem cuja nota já foi apagada: deixá-la numa pasta compartilhada
    manteria aberto o vazamento que esta migração fecha."""
    solto = apontar / "Figuras" / "_web" / "orfa123.webp"
    solto.write_bytes(b"WEBPFAKE")

    notas, _c, _i = _planejar(apontar)
    assert solto in mig.assets_web_soltos(apontar, notas, LAY)
    _contas, reescrever = mig.verificar_links(apontar, notas, LAY)
    conta = mig.aplicar(apontar, notas, LAY, DONO, reescrever)

    assert conta["assets_soltos_web"] == 1
    assert (apontar / "Pessoal/daniel/Figuras_web/orfa123.webp").is_file()
    assert not solto.exists()


# --------------------------------------------------------------------------- #
# Dry-run x aplicar
# --------------------------------------------------------------------------- #
def test_dry_run_nao_move_nada(apontar: Path, monkeypatch, capsys) -> None:
    """O default é relatar. O vault é a única cópia — não há segunda chance."""
    monkeypatch.setattr(sys, "argv", ["migrar"])
    antes = {p.relative_to(apontar).as_posix(): p.read_bytes()
             for p in apontar.rglob("*") if p.is_file()}
    assert mig.main() == 0                       # sem --aplicar
    depois = {p.relative_to(apontar).as_posix(): p.read_bytes()
              for p in apontar.rglob("*") if p.is_file()}
    assert depois == antes                       # nem caminho nem CONTEÚDO mudaram
    assert "NADA foi movido" in capsys.readouterr().out


def test_aplicar_move_para_os_dois_baldes(apontar: Path) -> None:
    notas, _c, _i = _planejar(apontar)
    _contas, reescrever = mig.verificar_links(apontar, notas, LAY)
    conta = mig.aplicar(apontar, notas, LAY, DONO, reescrever)

    assert conta["movidas"] == 6 and conta["puladas"] == 1   # a figura de obra fica
    assert (apontar / "Acervo/Conhecimento_Novo/obra/atomo_livro.md").is_file()
    assert (apontar / "Pessoal/daniel/Listas/Lista_compras.md").is_file()
    assert (apontar / "Pessoal/daniel/Inbox_Captura.md").is_file()
    assert (apontar / "Pessoal/daniel/Importado_Gemini/conversa.md").is_file()
    # e o original não ficou para trás (é MOVER, não copiar)
    assert not (apontar / "Conhecimento_Novo/obra/atomo_livro.md").exists()


def test_mtime_preservado(apontar: Path) -> None:
    """O `sync` do Chroma é incremental POR MTIME: carimbar 25 mil notas com data
    nova mandaria reindexar o vault inteiro — horas de GPU à toa."""
    alvo = apontar / "Conhecimento_Novo" / "obra" / "atomo_livro.md"
    antigo = 1_600_000_000
    os.utime(alvo, (antigo, antigo))
    fig = apontar / "Figuras" / "obra" / "obra_p0001_f1.webp"
    os.utime(fig, (antigo, antigo))

    notas, _c, _i = _planejar(apontar)
    _contas, reescrever = mig.verificar_links(apontar, notas, LAY)
    mig.aplicar(apontar, notas, LAY, DONO, reescrever)

    assert (apontar / "Acervo/Conhecimento_Novo/obra/atomo_livro.md").stat().st_mtime == antigo
    assert fig.stat().st_mtime == antigo         # nem se moveu, nem foi tocada


def test_idempotente(apontar: Path) -> None:
    """Rodar 2x não afunda o vault em Pessoal/daniel/Pessoal/daniel/..."""
    notas, _c, _i = _planejar(apontar)
    _contas, reescrever = mig.verificar_links(apontar, notas, LAY)
    mig.aplicar(apontar, notas, LAY, DONO, reescrever)
    estado_1 = {p.relative_to(apontar).as_posix(): p.read_bytes()
                for p in apontar.rglob("*") if p.is_file()}

    notas2, censo2, _i2 = _planejar(apontar)
    contas2, reescrever2 = mig.verificar_links(apontar, notas2, LAY)
    conta2 = mig.aplicar(apontar, notas2, LAY, DONO, reescrever2)

    assert conta2.get("movidas", 0) == 0 and conta2["puladas"] == 7
    assert conta2.get("reescritas", 0) == 0        # 2ª passada não mexe no conteúdo
    assert censo2[ACERVO] == 1 and censo2[PESSOAL] == 5 and censo2[FIGURAS] == 1
    assert contas2.get("quebrados", 0) == 0 and contas2.get("quebrados_web", 0) == 0
    estado_2 = {p.relative_to(apontar).as_posix(): p.read_bytes()
                for p in apontar.rglob("*") if p.is_file()}
    assert estado_2 == estado_1
    assert not any("Pessoal/daniel/Pessoal" in a for a in estado_2)


# --------------------------------------------------------------------------- #
# Wikilinks
# --------------------------------------------------------------------------- #
def test_link_por_nome_e_neutro() -> None:
    """A Malha usa `[[Conceito]]` sem barra: o Obsidian resolve pelo NOME dentro
    do vault, e os nomes de nota são únicos (medido: 25.213 nomes, 0 colisões)."""
    assert mig.link_quebra("Microcontrolador", {"Conhecimento_Novo/Sintese_esp32.md"}) is False


def test_link_por_caminho_quebra_se_o_alvo_se_move() -> None:
    """`![[Figuras/o/x.webp]]` é caminho a partir da RAIZ do vault
    (main._dentro_do_vault resolve `raiz / caminho`)."""
    movidos = {"Figuras/_web/abc123.webp"}
    assert mig.link_quebra("Figuras/_web/abc123.webp", movidos) is True
    assert mig.link_quebra("Figuras/obra/obra_p0001_f1.webp", movidos) is False


def test_verificar_links_separa_o_que_tem_conserto(apontar: Path) -> None:
    notas, _c, _i = _planejar(apontar)
    contas, _reescrever = mig.verificar_links(apontar, notas, LAY)
    assert contas.get("quebrados", 0) == 0         # nada quebra sem conserto
    assert contas["quebrados_web"] == 1            # o embed de _web, que é reescrito
    assert contas["por_caminho_ok"] == 1           # o embed da figura de obra, que fica
    assert contas["por_nome_neutros"] == 2         # [[Microcontrolador]] [[IoT]]


def test_recusa_aplicar_sem_backup(apontar: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    """O vault é a ÚNICA cópia do conhecimento destilado — sem backup, não mexe."""
    monkeypatch.setattr(mig.settings, "backup_dir", str(tmp_path / "backups_vazio"))
    monkeypatch.setattr(sys, "argv", ["migrar", "--aplicar"])
    assert mig.main() == 4
    saida = capsys.readouterr().out
    assert "RECUSADO" in saida and "backup" in saida
    assert (apontar / "Conhecimento_Novo/obra/atomo_livro.md").is_file()   # intacto


def test_recusa_aplicar_com_link_quebrado_sem_conserto(
    apontar: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """Um embed por caminho para uma nota de TEXTO que se move não tem reescrita
    prevista — o script recusa em vez de quebrar o link em silêncio."""
    _nota(apontar / "Conhecimento_Novo" / "cita.md", "Sintese",
          "![[Conhecimento_Novo/obra/atomo_livro.md]]")
    monkeypatch.setattr(mig.settings, "backup_dir", str(tmp_path / "b"))
    monkeypatch.setattr(sys, "argv", ["migrar", "--aplicar"])
    assert mig.main() == 3
    assert "RECUSADO" in capsys.readouterr().out
    assert (apontar / "Conhecimento_Novo/obra/atomo_livro.md").is_file()   # intacto


# --------------------------------------------------------------------------- #
# Órfãos e ilegíveis
# --------------------------------------------------------------------------- #
def test_arquivo_sem_nota_nao_e_movido(apontar: Path) -> None:
    """2.743 .webp de obras já aposentadas: sem `origem` para ler, classificá-los
    seria chutar. Ficam onde estão e são relatados."""
    orfa = apontar / "Figuras" / "aposentada" / "p0001_f1.webp"
    orfa.parent.mkdir(parents=True, exist_ok=True)
    orfa.write_bytes(b"WEBPFAKE")

    notas, _c, _i = _planejar(apontar)
    assert orfa in mig.orfaos(apontar, notas, LAY)
    _contas, reescrever = mig.verificar_links(apontar, notas, LAY)
    mig.aplicar(apontar, notas, LAY, DONO, reescrever)
    assert orfa.is_file()                       # não foi movida nem apagada


def test_nota_ilegivel_para_o_script_em_vez_de_virar_pessoal(
    apontar: Path, monkeypatch, capsys
) -> None:
    """`ler_cabecalho` engole OSError e devolve vazio — e vazio significa PESSOAL.
    Sem esta checagem, uma nota ilegível viraria nota do dono em silêncio."""
    monkeypatch.setattr(mig, "_confirmar_sem_origem",
                        lambda p: "PermissionError: acesso negado" if "Inbox" in p.name else "")
    monkeypatch.setattr(sys, "argv", ["migrar"])
    assert mig.main() == 2
    assert "PARADO" in capsys.readouterr().out


def test_irmao_com_curinga_no_nome_viaja_junto(apontar: Path) -> None:
    """Nome de átomo vem de título traduzido pelo LLM e tem `[`/`]`/`?` de verdade.
    Num `glob` esses viram curinga e o `.webp` irmão ficaria para trás SEM erro;
    o índice compara strings, então o problema não existe."""
    fig = apontar / "Conhecimento_Novo" / "obra" / "Fase [1] do ciclo.md"
    _nota(fig, ORIGEM_LIVRO)
    fig.with_suffix(".webp").write_bytes(b"WEBPFAKE")

    notas, _c, _i = _planejar(apontar)
    assert [p.name for p in next(n for n in notas if n.caminho == fig).irmaos] == [
        "Fase [1] do ciclo.webp"]
    _contas, reescrever = mig.verificar_links(apontar, notas, LAY)
    mig.aplicar(apontar, notas, LAY, DONO, reescrever)
    assert (apontar / "Acervo/Conhecimento_Novo/obra/Fase [1] do ciclo.webp").is_file()


def test_indice_nao_confunde_notas_de_prefixo_parecido(apontar: Path) -> None:
    """`obra_p0001_f1` e `obra_p0001_f11` são notas diferentes: o `.webp` de uma
    não pode ser arrastado pela outra."""
    extra = apontar / "Figuras" / "obra" / "obra_p0001_f11.webp"
    extra.write_bytes(b"WEBPFAKE")
    indice = mig.indexar_irmaos(apontar)
    assert indice[(extra.parent, "obra_p0001_f1")] == [
        apontar / "Figuras" / "obra" / "obra_p0001_f1.webp"]
