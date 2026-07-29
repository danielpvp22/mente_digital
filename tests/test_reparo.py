"""Triagem e reparo de átomos quebrados (mente_digital/reparo.py).

Os casos vêm dos 577 alvos REAIS da Cannabis Encyclopedia medidos em 2026-07-28:
nota de figura, Malha grudada no fim do corpo, '##' órfão, corpo cortado no meio
da palavra e corpo degenerado. A triagem é o que impede o reparo de reescrever
118 átomos que nunca estiveram quebrados.
"""
from mente_digital import reparo

FM = "---\norigem: Livro 'The Cannabis Encyclopedia' — 5 - Jardim (p. 90-110)\ncolhido_em: 2026-07-20\n---\n"
TAGS = "#zettelkasten_atomico #conhecimento_novo"


def _nota(corpo: str, titulo: str = "Auxinas afetam o alongamento celular",
          malha: str = "**Malha Neural:** [[Auxinas]] [[Crescimento]]",
          tags: str = TAGS) -> str:
    partes = [FM + f"## {titulo}", corpo]
    if malha:
        partes.append(malha)
    if tags:
        partes.append(tags)
    return "\n".join(p for p in partes if p) + "\n"


# --- separar -----------------------------------------------------------------
def test_separar_preserva_frontmatter_verbatim():
    """O frontmatter sai INTACTO: ele carrega `titulo_original` da passada de
    tradução, que uma remontagem por normalizar_atomo apagaria."""
    fm = FM[:-4] + "titulo_original: Auxins affect cell elongation\n---\n"
    a = reparo.separar(fm + "## T\nCorpo completo aqui.\n" + TAGS + "\n")
    assert a.frontmatter == fm
    assert "titulo_original" in reparo.montar(a, a.corpo)


def test_separar_descola_malha_grudada_no_corpo():
    """58 átomos reais: a frase terminava certo e o '**Malha Neural:**' na mesma
    linha fazia a régua enxergar o corpo continuando."""
    texto = _nota("Citocininas são sintetizadas nas raízes. **Malha Neural:** [[Raízes]]", malha="")
    a = reparo.separar(texto)
    assert a.corpo == "Citocininas são sintetizadas nas raízes."
    assert a.malha == "**Malha Neural:** [[Raízes]]"
    assert reparo.classificar(a) == reparo.OK


def test_separar_colhe_tags_penduradas_e_ignora_cerquilha_orfa():
    a = reparo.separar(_nota("Plantar clones em 1 de março. #zettelkasten_atomico #", tags=""))
    assert a.corpo == "Plantar clones em 1 de março."
    assert a.tags == "#zettelkasten_atomico"


def test_separar_sem_frontmatter_nao_quebra():
    a = reparo.separar("## Só o título\nE o corpo.\n")
    assert (a.frontmatter, a.titulo, a.corpo) == ("", "Só o título", "E o corpo.")


# --- montar ------------------------------------------------------------------
def test_montar_e_idempotente_em_nota_limpa():
    texto = _nota("Auxinas influenciam a divisão celular.")
    assert reparo.montar(reparo.separar(texto), reparo.separar(texto).corpo) == texto


def test_montar_troca_so_o_corpo():
    texto = _nota("Auxinas influenciam a divisão celu")
    novo = reparo.montar(reparo.separar(texto), "Auxinas influenciam a divisão celular e o alongamento.")
    assert "## Auxinas afetam o alongamento celular" in novo
    assert "**Malha Neural:** [[Auxinas]] [[Crescimento]]" in novo
    assert novo.rstrip().endswith(TAGS)
    assert "divisão celu\n" not in novo


def test_montar_nao_ressuscita_tag_ja_promovida():
    """Átomo promovido (sem #conhecimento_novo) não pode voltar a ser 'novo' —
    seria desfazer a maturação que o uso conquistou."""
    texto = _nota("Corpo cortado no mei", tags="#zettelkasten_atomico")
    novo = reparo.montar(reparo.separar(texto), "Corpo reescrito por inteiro.")
    assert "#conhecimento_novo" not in novo


# --- classificar / triar -----------------------------------------------------
def test_nota_de_figura_fica_fora_do_reparo():
    texto = _nota("- ![[Figuras/livro/p0175_f1.webp]] — Nitrogen (N)—mobile", titulo="Figuras", malha="")
    assert reparo.triar(texto)[0] == reparo.FIGURA


def test_corpo_degenerado_e_vazio():
    assert reparo.triar(_nota("O hid"))[0] == reparo.VAZIO


def test_corpo_cortado_no_meio_e_truncado():
    assert reparo.triar(_nota("Usar barreira contra vento reduz o impacto do vent"))[0] == reparo.TRUNCADO


def test_frase_curta_mas_completa_nao_e_defeito():
    """A régua separa QUEBRADO de CURTO: 6 palavras completas é átomo correto."""
    assert reparo.triar(_nota("A cobertura orgânica retém a umidade do solo."))[0] == reparo.OK


def test_termina_em_unidade_entre_parenteses_e_ok():
    assert reparo.triar(_nota("Adicione 7 galões de água (26,5 L)"))[0] == reparo.OK


def test_linha_em_branco_a_mais_nao_e_residuo():
    """O `_salvar_atomos` escreve `bloco + '\\n'` sobre um bloco que já termina em
    '\\n' — TODO átomo do vault tem uma linha vazia sobrando no fim. Se isso
    contasse como defeito, a passada reescreveria a base inteira à toa."""
    texto = _nota("A cobertura orgânica retém a umidade do solo.") + "\n"
    assert not reparo.tem_residuo(texto)
    assert reparo.triar(texto)[0] == reparo.OK


def test_residuo_e_conserto_sem_llm():
    """Corpo íntegro + lixo de formatação: `montar` já limpa, não paga GPU."""
    corpo = "A rega manual é eficaz e exige intervenção mecânica nula."
    cat, atomo = reparo.triar(_nota(corpo + "\n##"))
    assert cat == reparo.RESIDUO
    assert cat not in reparo.PRECISAM_LLM
    assert reparo.montar(atomo, atomo.corpo) == _nota(corpo)


# --- limpar_saida ------------------------------------------------------------
def test_limpar_saida_reduz_o_formato_inteiro_ao_corpo():
    saida = ("## Auxinas e crescimento\n"
             "Auxinas concentram-se nas pontas e favorecem o crescimento vertical.\n"
             "**Malha Neural:** [[Auxinas]]\n"
             "#zettelkasten_atomico #conhecimento_novo\n")
    assert reparo.limpar_saida(saida) == (
        "Auxinas concentram-se nas pontas e favorecem o crescimento vertical.")


def test_limpar_saida_tira_aspas_e_angulares():
    assert reparo.limpar_saida('"<A planta absorve nitrogênio pelas raízes.>"') == (
        "A planta absorve nitrogênio pelas raízes.")


# --- corpo_aceitavel ---------------------------------------------------------
def test_aceita_corpo_reparado():
    a = reparo.separar(_nota("Usar barreira contra vento reduz o impacto do vent"))
    ok, motivo = reparo.corpo_aceitavel(
        "Barreiras contra vento reduzem o impacto das rajadas sobre as plantas.", a)
    assert (ok, motivo) == (True, "")


def test_recusa_nada_vazio_curto_e_truncado_de_novo():
    a = reparo.separar(_nota("Usar barreira contra vento reduz o impacto do vent"))
    for saida in ("", "NADA", "Barreira de vento", "Barreiras contra vento reduzem o imp"):
        ok, motivo = reparo.corpo_aceitavel(saida, a)
        assert not ok and motivo


def test_recusa_saida_em_ingles():
    a = reparo.separar(_nota("Usar barreira contra vento reduz o impacto do vent"))
    ok, _ = reparo.corpo_aceitavel(
        "Windbreaks reduce the impact of wind on the plants in the garden.", a)
    assert not ok


def test_recusa_saida_MISTA_que_o_detector_de_idioma_deixa_passar():
    """Visto no 1º dry-run com o 8B: o modelo começa em PT e escorrega para o
    inglês da fonte. `idioma.em_ingles` exige AUSÊNCIA de português e por isso
    aceita a frase misturada — aqui a régua é dominância."""
    a = reparo.separar(_nota("Auxinas influenciam a divisão celu"))
    # Português de sobra no começo, inglês no fim: só a régua POR FRASE pega.
    misto = ("Auxinas influenciam a assimilação de água e a divisão celular, muitas "
             "vezes softening cell walls. Top branches grow vertically tall.")
    assert not reparo.em_pt_dominante(misto)
    ok, motivo = reparo.corpo_aceitavel(misto, a)
    assert not ok and "inglês" in motivo


def test_regua_de_REESCRITA_e_mais_dura_que_a_de_GUARDA():
    """Custos opostos: recusar um reparo não custa nada, reescrever um átomo
    correto estraga o que funciona. Medido na base do 8B — a régua frouxa marcou
    468 átomos e 5 de 20 amostrados eram português curto legítimo."""
    curta_pt = "Transferem nutrientes via ação capilar."
    assert not reparo.em_pt_dominante(curta_pt)      # guarda: marca (custo zero)
    assert reparo.frases_em_ingles(curta_pt) == []   # reescrita: não toca


def test_frase_inglesa_de_verdade_entra_na_reescrita():
    corpo = ("Recipientes rígidos servem ao cultivo. Small containers are ideal for "
             "short vegetative and flowering stages.")
    assert reparo.frases_em_ingles(corpo) == [
        "Small containers are ideal for short vegetative and flowering stages."]


def test_atomo_integro_com_frase_inglesa_e_categoria_IDIOMA():
    texto = _nota("Recipientes rígidos servem ao cultivo. Small containers are ideal "
                  "for short vegetative and flowering stages.")
    cat, _a = reparo.triar(texto)
    assert cat == reparo.IDIOMA
    assert cat not in reparo.PRECISAM_LLM       # não entra sem o dono pedir


def test_o_ingles_nao_chega_ao_prompt():
    """O modelo continua no idioma do corpo parcial que o prompt mostra. Corpo
    inglês inteiro vira "", e ele reescreve do título + trecho-fonte."""
    misto = ("Recipientes rígidos servem ao cultivo. Small containers are ideal for "
             "short vegetative and flowering stages.")
    assert reparo.corpo_sem_ingles(misto) == "Recipientes rígidos servem ao cultivo."
    assert reparo.corpo_sem_ingles(
        "All organic products should be certified by an independent third party.") == ""
    limpo = "A cobertura orgânica retém a umidade do solo."
    assert reparo.corpo_sem_ingles(limpo) == limpo


def test_frase_inglesa_com_um_e_portugues_no_meio_nao_escapa():
    """'…concentrated e inhibit lateral buds': o 'e' zerava a régua de 'nenhuma
    palavra portuguesa na frase'. Por isso a régua por frase tem duas condições."""
    assert not reparo.em_pt_dominante(
        "Auxinas atuam nas pontas. Top branches grow vertically taller where auxins "
        "are concentrated e inhibit lateral buds.")


def test_tira_a_frase_que_so_ecoa_o_titulo():
    corpo = ("Adicionar cobre, zinco e magnésio em quantidades mínimas aos fertilizantes. "
             "Solução para deficiência de níquel.")
    assert reparo.sem_eco_do_titulo(corpo, "Solução para deficiência de níquel") == (
        "Adicionar cobre, zinco e magnésio em quantidades mínimas aos fertilizantes.")


def test_eco_do_titulo_nao_apaga_o_corpo_inteiro():
    """Se sobrar nada, mantém o que veio — melhor eco que átomo vazio."""
    corpo = "Poda apical."
    assert reparo.sem_eco_do_titulo(corpo, "Poda apical") == corpo


def test_a_primeira_frase_e_poupada_mesmo_ecoando_o_titulo():
    """Nela o eco costuma ser o SUJEITO: tirá-la deixou um corpo começando em
    'É também misturado com…', sem dizer o quê (visto no dry-run)."""
    titulo = "Potassium hydroxide (KOH) é popular e eficaz para aumentar o pH"
    corpo = f"{titulo}. É também misturado com carbonato de potássio."
    assert reparo.sem_eco_do_titulo(corpo, titulo) == corpo


def test_corpo_pt_com_termo_tecnico_em_ingles_passa():
    """O glossário do dono manda PRESERVAR o termo do meio ('bud', 'leaching') —
    a régua de dominância não pode reprovar o átomo bem traduzido."""
    a = reparo.separar(_nota("Levar o meio de cultivo por leaching com solução dilu"))
    bom = ("Levar o meio de cultivo por leaching (lixiviação) com solução de "
           "fertilizante diluída resolve o excesso de nitrogênio.")
    assert reparo.em_pt_dominante(bom)
    assert reparo.corpo_aceitavel(bom, a)[0]


def test_recusa_so_o_que_nao_muda_nada():
    corpo = "Auxinas influenciam a assimilação de água e a divisão celular."
    a = reparo.separar(_nota(corpo))
    assert reparo.corpo_aceitavel(corpo, a) == (False, "idêntico ao original")


def test_acrescentar_o_ponto_final_conta_como_reparo():
    """74 dos 577 alvos são frase completa sem o ponto: devolver a mesma frase
    pontuada É o conserto — recusá-la deixaria a nota quebrada."""
    a = reparo.separar(_nota("Auxinas influenciam a assimilação de água e a divisão celular"))
    ok, _ = reparo.corpo_aceitavel(
        "Auxinas influenciam a assimilação de água e a divisão celular.", a)
    assert ok


# --- juntar_trechos ----------------------------------------------------------
def test_juntar_trechos_respeita_o_orcamento():
    rank = [(0.9, "a" * 30), (0.8, "b" * 30), (0.7, "c" * 30)]
    junto = reparo.juntar_trechos(rank, 70)
    assert junto == "a" * 30 + "\n\n" + "b" * 30


def test_juntar_trechos_corta_o_primeiro_em_vez_de_ficar_sem_fonte():
    assert reparo.juntar_trechos([(0.9, "x" * 100)], 40) == "x" * 40
