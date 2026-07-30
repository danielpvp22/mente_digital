"""A PORTA PRIMEIRO, os modelos depois (2026-07-29).

O uvicorn só reserva a porta DEPOIS do lifespan. Sem a checagem, uma segunda
instância carregava LLM + Whisper + embeddings + os 20 s de pré-montagem do XTTS,
escrevia "Mente Digital online" e só então falhava no bind — deixando um processo
zumbi com ~4,7 GB de VRAM presos. Duas cópias vivas passavam da capacidade da 3080,
o que apareceu para o dono como "vazamento de VRAM".
"""
import socket

from mente_digital import rede


def _porta_livre() -> int:
    """Uma porta efêmera que o SO acabou de nos dar e já devolvemos."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    porta = s.getsockname()[1]
    s.close()
    return porta


def test_porta_livre_nao_e_acusada():
    assert rede.porta_em_uso("127.0.0.1", _porta_livre()) is False


def test_porta_ocupada_e_detectada():
    ocupada = socket.socket()
    ocupada.bind(("127.0.0.1", 0))
    ocupada.listen(1)
    try:
        assert rede.porta_em_uso("127.0.0.1", ocupada.getsockname()[1]) is True
    finally:
        ocupada.close()


def test_a_checagem_nao_SEGURA_a_porta():
    """É um teste, não uma reserva. Se o socket ficasse aberto, o uvicorn falharia
    no bind logo em seguida — o conserto viraria o próprio bug."""
    porta = _porta_livre()

    assert rede.porta_em_uso("127.0.0.1", porta) is False

    depois = socket.socket()
    try:
        depois.bind(("127.0.0.1", porta))     # levantaria OSError se estivesse presa
    finally:
        depois.close()


def test_chamadas_repetidas_nao_vazam_socket():
    """Chamada barata e sem efeito colateral: o lifespan a faz no boot, e scripts
    de diagnóstico podem repeti-la à vontade."""
    porta = _porta_livre()

    for _ in range(50):
        assert rede.porta_em_uso("127.0.0.1", porta) is False
