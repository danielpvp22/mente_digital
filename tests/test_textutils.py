"""
Utilitários de relevância léxica — o coração do anti "Cache Hit falso".
Pura lógica de string; sem dependências pesadas.
"""
from mente_digital import textutils


def test_normaliza_tira_acento_e_caixa():
    assert textutils.normaliza("Não TENHO informações") == "nao tenho informacoes"


def test_palavras_chave_remove_stopwords_e_curtas():
    chaves = textutils.palavras_chave("gostaria de entender o TensorFlow RT")
    assert "tensorflow" in chaves
    # 'gostaria', 'de', 'o' são stopwords; 'rt' tem só 2 letras e nenhum dígito
    assert "gostaria" not in chaves
    assert "de" not in chaves
    assert "rt" not in chaves


def test_palavras_chave_mantem_token_curto_com_digito():
    # tokens curtos com dígito (ex.: versões) contam como significativos
    assert "3d" in textutils.palavras_chave("render 3d")


def test_contem_alguma_aterramento_lexico():
    chaves = {"tensorflow"}
    assert textutils.contem_alguma("O TensorFlow é um framework", chaves) is True
    assert textutils.contem_alguma("Uma receita de bolo", chaves) is False


def test_contem_alguma_sem_chaves_e_falso():
    assert textutils.contem_alguma("qualquer texto", set()) is False


def test_tem_sobreposicao():
    assert textutils.tem_sobreposicao({"a", "b"}, {"b", "c"}) is True
    assert textutils.tem_sobreposicao({"a"}, {"c"}) is False


def test_limpar_query_remove_saudacao_e_capa_tamanho():
    bruto = "Olá gostaria de entender melhor sobre o TensorFlow RT hoje"
    limpa = textutils.limpar_query(bruto)
    # saudações/fillers somem; termos reais e caixa preservados
    assert "TensorFlow" in limpa
    assert "Olá" not in limpa
    assert "gostaria" not in limpa
    # capado em no máx. 6 palavras
    assert len(limpa.split()) <= 6


def test_limpar_query_preserva_ordem_e_caixa():
    assert textutils.limpar_query("Python versus Rust") == "Python versus Rust"
