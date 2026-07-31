"""A pergunta que NÃO SE SUSTENTA SOZINHA (2026-07-31).

O caso real: o dono perguntou "o que são tricomas?", recebeu uma boa resposta, e
emendou **"tem imagem de um?"** — que voltou falando de "um PMC com fita vermelha
ou azul". Duas coisas falharam juntas, e as duas são de FORMA da frase, não de
assunto:

1. o "um" órfão (sem substantivo depois) não estava em nenhuma régua, então a
   pergunta passou por auto-contida: o trace registra `extrator_ms: 0` — o LLM que
   resolveria o pronome nem chegou a ser chamado;
2. "imagem" foi tratada como se fosse o assunto. Medido no índice de produção, a
   busca por essa palavra traz os baldes de conversa sobre YOLO e detecção de
   objetos, que é onde ela vive no vault.

Os testes abaixo fixam as duas correções E as duas regressões que elas não podem
reabrir — ambas já custaram caro neste repo: "como funciona o tensor RT?" não pode
enxergar o turno do esp32, e "o que o livro diz sobre cultivo de tomate?" não pode
receber o histórico (foi assim que uma resposta colou "6 a 9 semanas" de floração
de dois turnos antes).
"""
from mente_digital import otimizador


class TestIndefinidoOrfao:
    def test_pergunta_que_termina_em_indefinido_depende_do_turno_anterior(self):
        # o substantivo foi elidido porque está no turno anterior
        assert otimizador.referencia_contexto("tem imagem de um?")
        assert otimizador.precisa_antecedente("tem imagem de um?")

    def test_indefinido_com_substantivo_e_auto_contida(self):
        # "um" aqui determina "balastro": a frase tem sujeito próprio
        assert not otimizador.referencia_contexto("me fale sobre um balastro")
        assert not otimizador.precisa_antecedente("me fale sobre um balastro")

    def test_pergunta_completa_sobre_o_assunto_nao_vira_anafora(self):
        assert not otimizador.precisa_antecedente("tem imagem de um tricoma?")


class TestPalavraDeFormato:
    def test_formato_sozinho_nao_e_nucleo(self):
        # "foto" diz a FORMA da resposta; o assunto está no turno anterior
        assert otimizador.precisa_antecedente("tem foto?")

    def test_formato_com_assunto_se_sustenta(self):
        assert not otimizador.precisa_antecedente("tem foto da deficiência de nitrogênio?")

    def test_pedido_de_imagem_reconhecido(self):
        assert otimizador.pedido_de_imagem("tem imagem de um tricoma?")
        assert otimizador.pedido_de_imagem("mostra uma foto disso")
        assert not otimizador.pedido_de_imagem("o que são tricomas?")

    def test_sem_formato_tira_so_a_palavra_de_formato(self):
        assert otimizador.sem_formato("imagem tricoma") == "tricoma"
        assert otimizador.sem_formato("figura deficiencia nitrogenio") == \
            "deficiencia nitrogenio"

    def test_sem_formato_nao_zera_a_query(self):
        # sem isto, "tem foto?" viraria uma busca por string vazia
        assert otimizador.sem_formato("imagem") == "imagem"
        assert otimizador.sem_formato("") == ""


class TestRegressoesQueNaoPodemVoltar:
    def test_troca_limpa_de_assunto_continua_auto_contida(self):
        # 'tensor rt esp32' — a query que o extrator montava vendo o turno anterior
        assert not otimizador.precisa_antecedente("como funciona o tensor RT?")

    def test_pergunta_longa_e_propria_nao_recebe_historico(self):
        # a alucinação de 2026-07-29: floração colada numa pergunta sobre tomate
        assert not otimizador.precisa_antecedente(
            "O que o livro diz sobre cultivo de tomate?")

    def test_continuacao_sem_pronome_segue_dependente(self):
        # o motivo original do precisa_antecedente existir
        assert otimizador.precisa_antecedente("explique melhor")
