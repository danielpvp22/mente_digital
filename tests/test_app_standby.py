"""
A casca diante de um servidor de plantão.

Sobe um HTTP de mentira (não um mock de `urlopen`): o que pode dar errado aqui é
justamente a conversa pela rede — a rota certa, o campo certo, o POST com o
corpo certo. Um mock provaria só que eu chamei a função que eu mesmo escrevi.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app


class _Servidorzinho(BaseHTTPRequestHandler):
    descansando = False
    recebidos: list = []

    def _responder(self, corpo: dict) -> None:
        dados = json.dumps(corpo).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):                              # noqa: N802 - assinatura da stdlib
        if self.path.startswith("/api/health"):
            self._responder({"pronto": True, "servicos": {},
                             "descansando": type(self).descansando})
        else:
            self.send_error(404)

    def do_POST(self):                             # noqa: N802
        tamanho = int(self.headers.get("Content-Length") or 0)
        corpo = json.loads(self.rfile.read(tamanho) or b"{}")
        type(self).recebidos.append((self.path, corpo.get("acao")))
        self._responder({"estado": "ligado", "depois": {"vram_mb": 5000}})

    def log_message(self, *_args):                 # silencia o log da stdlib
        pass


@pytest.fixture
def servidorzinho():
    _Servidorzinho.descansando = False
    _Servidorzinho.recebidos = []
    httpd = HTTPServer(("127.0.0.1", 0), _Servidorzinho)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", _Servidorzinho
    httpd.shutdown()


def _esperar_post(classe, tentativas: int = 60) -> list:
    """O POST sai numa thread (ele demora ~30s no servidor real, e a barra de
    progresso precisa continuar desenhando enquanto isso)."""
    import time

    for _ in range(tentativas):
        if classe.recebidos:
            return classe.recebidos
        time.sleep(0.05)
    return classe.recebidos


def test_abrir_a_janela_num_servidor_de_plantao_religa(servidorzinho):
    """O beco que o modo economia criou: com o `--standby` no logon, abrir o app
    pelo atalho de sempre achava a porta ocupada, entrava no caminho "janela num
    servidor que já existe" e a tela de boot esperava modelos que ninguém tinha
    mandado carregar — até os 150 s do botão de escape."""
    base, classe = servidorzinho
    classe.descansando = True

    assert app._acordar_se_dormindo(base, "") is True
    assert _esperar_post(classe) == [("/api/energia", "ligar")]


def test_servidor_acordado_nao_leva_pedido_nenhum(servidorzinho):
    base, classe = servidorzinho
    classe.descansando = False

    assert app._acordar_se_dormindo(base, "") is False
    assert classe.recebidos == []


def test_servidor_fora_do_ar_nao_quebra_a_abertura(servidorzinho):
    """Porta fechada é o caso NORMAL no boot local (a janela nasce antes de o
    uvicorn fazer o bind). Não pode virar exceção no caminho de abertura."""
    base, _classe = servidorzinho
    porta_morta = base.rsplit(":", 1)[0] + ":9"    # descarte, nunca escuta

    assert app._acordar_se_dormindo(porta_morta, "") is False


def test_servidor_antigo_sem_o_campo_nao_e_acordado(servidorzinho):
    """Servidor sem a chave `descansando` (versão anterior a 2026-08-02) não pode
    receber um `ligar` a cada abertura — ele nem tem modo economia."""
    base, classe = servidorzinho
    classe.descansando = None                      # vira JSON null -> falsy

    assert app._acordar_se_dormindo(base, "") is False
    assert classe.recebidos == []
