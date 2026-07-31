"""
Revalidação do acervo web no idle: a imagem é o que a nota AFIRMA ser? (2026-07-31)

O BUG HISTÓRICO: o gate que julga o candidato ANTES de baixar
(`imagem_web.casa_com_o_pedido`) só passou a existir em 2026-07-31. O que entrou antes
nunca foi julgado — e das 40 notas de `Figuras/_web/`, **4 (10%)** mostram outra coisa:
'polvo' é um calendário do EBT da Flórida, 'estômato' é um formulário de currículo em
espanhol, 'solo argiloso' é uma miniatura do Rainbow Six Siege (o buscador casou o
"solo" de *solo queue*), 'coral cerebral' é um render médico de vasos cerebrais. Todas
INDEXADAS, todas anexáveis como figura numa resposta.

O achado que define o desenho: o TÍTULO da página, que a nota já carrega desde a
ingestão, denuncia as 4 — sem VLM, sem GPU, sem rede. O Qwen2.5-VL-7B também pega 4/4,
por 7,9 GB de VRAM e 0,87 s/imagem. Onde o barato empata com o caro, o caro perde.

E O SEGUNDO BUG, achado ao medir: `textutils.contem_alguma` casa TOKEN INTEIRO, então
um "s" derruba o casamento — 'tricoma' não casava "## tricomas", 'bráctea' não casava
"## Brácteas, las hojas...". Foram **5 falsos positivos em 40, todos de plural**, e é
por isso que existe `contem_alguma_flex`. Nota boa condenada é pior que nota ruim
passando: a primeira o dono perde sem saber.
"""
import re

import pytest

from mente_digital import etl, imagem_web, textutils


# --- tolerância a plural: o defeito que os falsos positivos revelaram ---------
@pytest.mark.parametrize("plural, singular", [
    ("tricomas", "tricoma"),      # o caso real: '## tricomas   PDF'
    ("bracteas", "bractea"),      # '## Brácteas, las hojas que acompañan...'
    ("flores", "flor"),
    ("animais", "animal"),
    ("leoes", "leao"),
    ("jovens", "jovem"),
])
def test_radical_junta_singular_e_plural(plural, singular):
    assert textutils.radical(plural) == textutils.radical(singular)


def test_radical_nao_mutila_palavra_curta():
    # 'gas' -> 'gas', não 'ga': abaixo de 4 letras não há plural a tirar com segurança.
    assert textutils.radical("gas") == "gas"
    assert textutils.radical("os") == "os"


def test_flex_casa_plural_e_a_original_nao():
    # Os dois lados do contraste importam: se `contem_alguma` passar a casar plural,
    # alguém afrouxou o gate do VectorStore sobre ~24 mil notas sem medir.
    assert textutils.contem_alguma_flex("## tricomas   PDF", {"tricoma"})
    assert not textutils.contem_alguma("## tricomas   PDF", {"tricoma"})


# --- confere_o_acervo: os 4 casos reais --------------------------------------
@pytest.mark.parametrize("termo, titulo", [
    ("polvo", "Florida EBT Reload Schedule – Find Out Now!"),
    ("estômato", "Formato de Hoja de Vida Minerva 1003 © (PDF y Word)"),
    ("solo argiloso", "$10,000 CONSOLE TOURNEY AGAINST RICCI - Rainbow Six Siege - YouTube"),
    ("coral cerebral", "Foto de Cérebro De Mra Coronal 3d Sagital Visão Mip Mostrando Artéria"),
])
def test_pega_as_quatro_imagens_que_mentem(termo, titulo):
    assert not imagem_web.confere_o_acervo(termo, titulo)


@pytest.mark.parametrize("termo, titulo", [
    ("tricoma", "tricomas   PDF"),                       # plural — era falso positivo
    ("bráctea", "Brácteas, las hojas que acompañan a las flores"),   # idem
    ("camaleão", "Características, tipos, cuidados e muito mais do camaleão"),
    ("capivara", "Fotos De Uma Capivara - FDPLEARN"),
    ("urso-polar", "Urso polar: características, habitat e curiosidades"),
])
def test_nao_condena_nota_boa(termo, titulo):
    assert imagem_web.confere_o_acervo(termo, titulo)


def test_descricao_salva_quando_o_titulo_nao_menciona():
    # Título de isca não cita o bicho, mas a descrição do VLM cita: basta UM dos dois.
    assert imagem_web.confere_o_acervo(
        "camaleão", "Conheça 10 animais incríveis que você não viu",
        "A imagem mostra um camaleão colorido sobre um galho.")


def test_sem_keyword_utilizavel_aceita():
    # Mesma escolha do `casa_com_o_pedido`: não condenar por falha do normalizador.
    assert imagem_web.confere_o_acervo("", "qualquer coisa")
    assert imagem_web.confere_o_acervo("de a o", "qualquer coisa")


# --- a tautologia: o corpo REPETE o termo, e isso zerava a checagem ----------
def test_evidencia_ignora_as_linhas_derivadas_do_termo():
    """Sem isto a revalidação pegava 0 das 4 imagens erradas, e parecia funcionar.

    A nota repete o termo buscado em DOIS lugares gerados a partir dele: a linha de
    crédito ("Imagem obtida na web em uma busca por 'polvo'") e a Malha
    ("[[Polvo]]"). Passar o corpo inteiro como evidência faz o termo casar consigo
    mesmo — a checagem aprova tudo, inclusive o calendário do EBT.
    """
    nota = (f"---\ntipo: figura\n---\n## Florida EBT Reload Schedule\n"
            f"![[x.webp]]\nA imagem mostra um calendário.\n"
            f"{imagem_web.CREDITO_PREFIXO} 'polvo', em Bing.\n"
            f"**Malha Neural:** [[Polvo]] [[Imagem da Web]]\n")
    ev = etl._evidencia_independente(nota)
    assert "polvo" not in ev.lower() and "Polvo" not in ev
    assert "calendário" in ev          # a evidência de verdade permanece
    assert not imagem_web.confere_o_acervo("polvo", "Florida EBT Reload Schedule", ev)


def test_evidencia_preserva_a_descricao_do_vlm():
    nota = (f"---\ntipo: figura\n---\n## Conheça 10 animais incríveis\n![[x.webp]]\n"
            f"A imagem mostra um camaleão colorido sobre um galho.\n"
            f"{imagem_web.CREDITO_PREFIXO} 'camaleão', em Bing.\n"
            f"**Malha Neural:** [[Camaleão]]\n")
    ev = etl._evidencia_independente(nota)
    assert "camaleão colorido" in ev
    # o título de isca não cita o bicho; quem salva a nota é a descrição do VLM
    assert imagem_web.confere_o_acervo("camaleão", "Conheça 10 animais incríveis", ev)


# --- a rotina do idle: marca, e é idempotente --------------------------------
def _nota(termo: str, titulo: str) -> str:
    return (f"---\norigem: {imagem_web.ORIGEM_PREFIXO} '{termo}'\ntipo: figura\n---\n"
            f"## {titulo}\n![[Figuras/_web/x.webp]]\ncorpo\n")


def test_marca_a_suspeita_e_confirma_a_boa(tmp_path, monkeypatch):
    monkeypatch.setattr(imagem_web, "pasta_do_acervo", lambda: tmp_path)
    ruim = tmp_path / "ruim.md"; ruim.write_text(_nota("polvo", "Florida EBT Reload Schedule"),
                                                 encoding="utf-8")
    boa = tmp_path / "boa.md"; boa.write_text(_nota("capivara", "Fotos De Uma Capivara"),
                                              encoding="utf-8")
    marcadas, lidas = etl.EtlProcessor._revalidar_notas(None, [boa, ruim])
    assert (marcadas, lidas) == (1, 2)
    assert "acervo_confere: NAO" in ruim.read_text(encoding="utf-8")
    assert "acervo_confere: sim" in boa.read_text(encoding="utf-8")


def test_segunda_passada_nao_reescreve(tmp_path, monkeypatch):
    # Idempotência não é preciosismo: o reindex do VectorStore é por `mtime`, e reescrever
    # sem mudança faria toda nota de acervo ser re-embeddada a cada ciclo de idle.
    monkeypatch.setattr(imagem_web, "pasta_do_acervo", lambda: tmp_path)
    md = tmp_path / "a.md"
    md.write_text(_nota("polvo", "Florida EBT Reload Schedule"), encoding="utf-8")
    etl.EtlProcessor._revalidar_notas(None, [md])
    antes = md.stat().st_mtime_ns, md.read_text(encoding="utf-8")
    etl.EtlProcessor._revalidar_notas(None, [md])
    assert (md.stat().st_mtime_ns, md.read_text(encoding="utf-8")) == antes


def test_veredicto_muda_quando_a_nota_muda(tmp_path, monkeypatch):
    # Se o dono trocar a imagem/título, a marca tem de acompanhar — senão o campo vira
    # lixo fossilizado que ninguém confia.
    monkeypatch.setattr(imagem_web, "pasta_do_acervo", lambda: tmp_path)
    md = tmp_path / "a.md"
    md.write_text(_nota("polvo", "Florida EBT Reload Schedule"), encoding="utf-8")
    etl.EtlProcessor._revalidar_notas(None, [md])
    assert "acervo_confere: NAO" in md.read_text(encoding="utf-8")
    md.write_text(md.read_text(encoding="utf-8").replace(
        "## Florida EBT Reload Schedule", "## Polvo gigante do Pacífico"), encoding="utf-8")
    etl.EtlProcessor._revalidar_notas(None, [md])
    assert "acervo_confere: sim" in md.read_text(encoding="utf-8")


def test_nota_sem_termo_ou_sem_titulo_e_ignorada(tmp_path, monkeypatch):
    monkeypatch.setattr(imagem_web, "pasta_do_acervo", lambda: tmp_path)
    md = tmp_path / "a.md"
    md.write_text("---\ntipo: figura\n---\n## Sem origem\ncorpo\n", encoding="utf-8")
    assert etl.EtlProcessor._revalidar_notas(None, [md]) == (0, 0)
    assert "acervo_confere" not in md.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_flag_desligada_nao_toca_em_nada(tmp_path, monkeypatch):
    monkeypatch.setattr(imagem_web, "pasta_do_acervo", lambda: tmp_path)
    monkeypatch.setattr(etl.settings, "idle_revalidar_acervo", False)
    md = tmp_path / "a.md"
    md.write_text(_nota("polvo", "Florida EBT Reload Schedule"), encoding="utf-8")
    proc = etl.EtlProcessor.__new__(etl.EtlProcessor)
    await proc.revalidar_acervo_web()
    assert "acervo_confere" not in md.read_text(encoding="utf-8")


def test_regex_do_termo_segue_o_contrato_do_imagem_web():
    # A regex do etl é construída a partir de `imagem_web.ORIGEM_PREFIXO`. Se alguém mudar
    # o prefixo lá e recopiar aqui, isto pega a divergência.
    linha = f"origem: {imagem_web.ORIGEM_PREFIXO} 'camaleão'"
    assert etl._TERMO_DO_ACERVO.search(linha).group(1) == "camaleão"
