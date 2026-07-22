"""
Testes da Guarda de Egressão (#6) — mascaramento de PII na query web (egressao.py).
Puro, sem rede: exercita cada detector E os falsos-positivos que ele NÃO pode causar.
"""
from mente_digital import egressao


def test_email_mascarado():
    limpo, achados = egressao.mascarar_pii("busca sobre danielpvp22@gmail.com por favor")
    assert "[email]" in limpo
    assert "@gmail.com" not in limpo
    assert achados == ["email"]


def test_cpf_formatado():
    limpo, achados = egressao.mascarar_pii("meu cpf 123.456.789-09 caiu no serasa?")
    assert limpo == "meu cpf [cpf] caiu no serasa?"
    assert achados == ["cpf"]


def test_cnpj_formatado():
    limpo, achados = egressao.mascarar_pii("nota da 12.345.678/0001-95 hoje")
    assert "[cnpj]" in limpo
    assert achados == ["cnpj"]


def test_cartao_valido_luhn_mascarado():
    # 4111 1111 1111 1111 é um número de teste Visa VÁLIDO no Luhn.
    limpo, achados = egressao.mascarar_pii("paguei com 4111 1111 1111 1111 ontem")
    assert "[cartao]" in limpo
    assert "4111" not in limpo
    assert achados == ["cartao"]


def test_sequencia_longa_que_nao_e_cartao_preservada():
    # 16 dígitos que NÃO passam no Luhn não são cartão — a busca não pode ser mutilada.
    limpo, achados = egressao.mascarar_pii("codigo do produto 1234 5678 9012 3456")
    assert "cartao" not in achados
    assert "1234" in limpo


def test_telefone_com_parenteses():
    limpo, achados = egressao.mascarar_pii("me liga no (41) 99876-5432 depois")
    assert "[telefone]" in limpo
    assert "99876" not in limpo
    assert achados == ["telefone"]


def test_telefone_com_codigo_pais():
    limpo, achados = egressao.mascarar_pii("whats +55 41 99876-5432")
    assert "[telefone]" in limpo
    assert achados == ["telefone"]


def test_telefone_solto_sem_marcador_passa():
    # Sem parênteses nem +55, é ambíguo demais — conservador, deixa passar.
    q = "liga 99876 5432 pra mim"
    limpo, achados = egressao.mascarar_pii(q)
    assert achados == []
    assert limpo == q


def test_ddd_curto_nao_vira_telefone():
    # "DDD 41" e números curtos não têm estrutura de telefone -> não mascara.
    limpo, achados = egressao.mascarar_pii("qual o ddd 41 e o preço 4500 reais?")
    assert achados == []
    assert limpo == "qual o ddd 41 e o preço 4500 reais?"


def test_busca_comum_intacta():
    q = "quanto o TensorRT acelera o YOLOv8 na RTX 3080?"
    limpo, achados = egressao.mascarar_pii(q)
    assert limpo == q
    assert achados == []


def test_multiplos_tipos_ordenados():
    limpo, achados = egressao.mascarar_pii("email a@b.com e cpf 111.222.333-44")
    assert "[email]" in limpo and "[cpf]" in limpo
    assert achados == ["cpf", "email"]  # ordenado p/ determinismo


def test_texto_vazio():
    assert egressao.mascarar_pii("") == ("", [])
