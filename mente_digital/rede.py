"""
Checagem de porta ANTES de carregar qualquer modelo.

Bug real, diagnosticado pelos logs em 2026-07-29: o uvicorn só reserva a porta
DEPOIS que o lifespan termina. Sem esta checagem, uma segunda instância do app
carregava LLM, Whisper, embeddings, ChromaDB, a MALHA e os 20 s de pré-montagem do
XTTS — cerca de 45 s e ~4,7 GB de VRAM — escrevia `[SERVER] Mente Digital online`
no log, e só então falhava no bind.

Duas consequências, as duas observadas:

1. Das três linhas "online" daquele log, DUAS eram de processos que nunca
   atenderam uma requisição. O log mentia.
2. O processo que perdia o bind NÃO morria: ficava zumbi segurando a GPU. Com as
   duas cópias vivas, 2 × 4,67 GB de modelos + 1,42 GB de desktop passavam da
   capacidade da 3080 (10,24 GB) — o que o dono viu como "vazamento de VRAM" e
   que na verdade eram duas cópias inteiras do aplicativo.
"""
from __future__ import annotations

import os
import socket


def porta_em_uso(host: str, porta: int) -> bool:
    """Alguém já escuta em `host:porta`? Puro/testável, custa milissegundos.

    Testa por BIND e não por `connect`: é exatamente o veredito que o uvicorn vai
    receber depois, e não depende de o servidor existente responder a nada.

    `SO_REUSEADDR` só fora do Windows, espelhando o que o asyncio faz: no POSIX ele
    permite bindar sobre um socket em TIME_WAIT (sem isso o teste acusaria ocupada
    uma porta que o uvicorn conseguiria pegar); no Windows a opção tem semântica de
    SEQUESTRO e daria o contrário — dizer que está livre uma porta em uso.

    É um TESTE, não uma reserva: o socket é fechado em seguida, então existe uma
    janela mínima em que outro processo poderia tomar a porta. Aceitável de
    propósito — o objetivo é pegar o caso comum ("já tem um rodando"), não garantir
    exclusão mútua. Se a janela perder, o uvicorn falha como antes; só que sem os
    45 s desperdiçados e sem deixar zumbi.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, porta))
        return False
    except OSError:
        return True
    finally:
        s.close()
