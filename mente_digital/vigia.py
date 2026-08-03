"""
O VIGIA — o processo mínimo que espera o celular com o PC em zero.

O pedido (dono, 2026-08-02, escolha "os dois, em camadas"): o assistente não deve
ocupar a máquina enquanto ninguém o usa, mas o celular tem de conseguir levantá-lo
de outro cômodo. As duas coisas não cabem no mesmo processo: quem carrega Whisper,
XTTS e o vault não é "mínimo" nem descansando (o standby libera a VRAM, mas o
processo Python segue com ~7,7 GB de RAM comprometidos — medido hoje).

Então são dois. Este é o de baixo: **stdlib pura, sem torch, sem FastAPI, sem
uvicorn** — um `http.server` de um punhado de rotas que fica de plantão no logon e
não faz absolutamente nada além de esperar. Quando o celular bate autenticado, ele
SOBE o `app.py` e sai da frente. Quando o `app.py` se encerra sozinho por
ociosidade, o PC volta a zero e o vigia continua ali.

⚠ NÃO IMPORTE NADA PESADO AQUI. O valor deste arquivo é ser barato; um
`from mente_digital.rag import ...` distraído arrastaria o torch para dentro do
processo que existe justamente para não ter torch. Só `config` (0,23 s, sem
torch — medido), `acesso` e `rede` entram, e os três são de propósito minúsculos.

SEGURANÇA: `acordar` é a única rota que FAZ algo, e ela exige o token — a mesma
regra do `/api/api` do servidor grande (`acesso.cliente_autorizado`). Sem token
configurado, só loopback. É o "só abre o servidor quando autenticado" pedido: um
aparelho qualquer da LAN não levanta o assistente de ninguém. A rota de status
não tem gate porque não revela nada além de "tem servidor de pé?" — o mesmo
critério do `/api/health`.
"""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - subir o próprio app é o objetivo do módulo
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from mente_digital import acesso, rede
from mente_digital.config import settings

# Quanto tempo o vigia considera que "já mandei subir" ainda vale. O boot leva
# ~35 s; sem esta janela, o celular batendo a cada tique da tela de carregamento
# mandaria subir um app.py por tique — e o segundo morreria no teste de porta,
# depois de importar meio projeto à toa.
SEGUNDOS_SUBINDO = 90.0


@dataclass(frozen=True)
class Veredito:
    """O que fazer com um pedido de acordar. Puro — decidido sem tocar em nada."""

    subir: bool
    estado: str          # "ja_de_pe" | "subindo" | "subindo_agora"


def decidir(servidor_de_pe: bool, subindo_ha: Optional[float]) -> Veredito:
    """Este pedido deve levantar o `app.py`?

    Três respostas, e as três importam para o celular: já está de pé (entra
    direto), já mandei subir (mostre a tela de carregamento e espere) e vou subir
    agora (mostre a tela de carregamento). Puro/testável, como `standby.avaliar`.
    """
    if servidor_de_pe:
        return Veredito(False, "ja_de_pe")
    if subindo_ha is not None and subindo_ha < SEGUNDOS_SUBINDO:
        return Veredito(False, "subindo")
    return Veredito(True, "subindo_agora")


def comando_para_subir(raiz: Path, executavel: Optional[str] = None) -> list[str]:
    """A linha de comando que levanta o app. PURA — testável sem subir nada.

    `--oculto` e não `--standby`: quem pede isto é alguém que QUER usar o
    assistente agora, então os modelos sobem de verdade; o que não faz sentido é
    abrir uma janela na cara de quem não está no PC. A janela nasce escondida e a
    bandeja fica de porta de entrada, então trazê-la depois custa zero.

    `pythonw` quando existir: sem o `w`, o app viveria atrás de um console preto
    que o Alt+Tab acha — e este caminho é justamente o que roda sem ninguém
    olhando."""
    exe = Path(executavel or sys.executable)
    candidato = exe.with_name("pythonw.exe")
    py = str(candidato if candidato.exists() else exe)
    return [py, str(raiz / "app.py"), "--oculto"]


def subir_app(raiz: Path) -> bool:
    """Dispara o `app.py` DESGRUDADO deste processo.

    Desgrudado importa: o vigia precisa poder ser reiniciado (ou morrer) sem
    levar o assistente junto, e o assistente precisa sobreviver ao vigia. No
    Windows isso é `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`; fora dele,
    `start_new_session`."""
    comando = comando_para_subir(raiz)
    try:
        if os.name == "nt":
            desgrudado = 0x00000008 | 0x00000200      # DETACHED_PROCESS | NEW_GROUP
            subprocess.Popen(comando, cwd=str(raiz), creationflags=desgrudado,  # nosec B603
                             close_fds=True)
        else:
            subprocess.Popen(comando, cwd=str(raiz), start_new_session=True,  # nosec B603
                             close_fds=True)
        return True
    except Exception as exc:                          # noqa: BLE001 - nunca derruba o vigia
        print(f"[VIGIA] não consegui subir o app: {exc}", flush=True)
        return False


class Vigia:
    """O estado do plantão. Fica fora do handler HTTP porque o `http.server` cria
    uma instância de handler POR REQUISIÇÃO — guardar "mandei subir às 22h" lá
    dentro seria guardar em algo que morre no fim da resposta."""

    def __init__(self, raiz: Path, relogio=None) -> None:
        self.raiz = raiz
        self._relogio = relogio or __import__("time").monotonic
        self._mandou_subir_em: Optional[float] = None
        self._trava = threading.Lock()

    def servidor_de_pe(self) -> bool:
        return rede.porta_em_uso(settings.host, settings.port)

    def _subindo_ha(self) -> Optional[float]:
        if self._mandou_subir_em is None:
            return None
        return self._relogio() - self._mandou_subir_em

    def status(self) -> dict:
        de_pe = self.servidor_de_pe()
        subindo = self._subindo_ha()
        return {
            "vigia": True,
            "servidor": de_pe,
            "subindo": bool(not de_pe and subindo is not None and subindo < SEGUNDOS_SUBINDO),
            "porta_servidor": settings.port,
        }

    def acordar(self) -> dict:
        """Levanta o app se preciso. Serializado: dois celulares (ou dois tiques
        da mesma tela) chegando juntos não podem disparar dois `app.py`."""
        with self._trava:
            veredito = decidir(self.servidor_de_pe(), self._subindo_ha())
            if not veredito.subir:
                return {"estado": veredito.estado, "porta_servidor": settings.port}
            if not subir_app(self.raiz):
                return {"estado": "falhou", "porta_servidor": settings.port}
            self._mandou_subir_em = self._relogio()
            print("[VIGIA] pedido autenticado — subindo o assistente.", flush=True)
            return {"estado": veredito.estado, "porta_servidor": settings.port}


def _montar_handler(vigia: Vigia):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MenteVigia/1.0"

        def _responder(self, codigo: int, corpo: dict) -> None:
            dados = json.dumps(corpo).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)

        def _autorizado(self) -> bool:
            token = self.headers.get("X-Mente-Token")
            host = self.client_address[0] if self.client_address else None
            return acesso.cliente_autorizado(host, token, settings.access_token)

        def do_GET(self):                              # noqa: N802 - assinatura da stdlib
            if self.path.rstrip("/") == "/vigia/status":
                self._responder(200, vigia.status())
            else:
                self._responder(404, {"erro": "rota desconhecida"})

        def do_POST(self):                             # noqa: N802
            if self.path.rstrip("/") != "/vigia/acordar":
                self._responder(404, {"erro": "rota desconhecida"})
                return
            # Drena o corpo mesmo sem usá-lo: deixar bytes no socket faz o cliente
            # ver a conexão como quebrada em vez de ler a resposta.
            tamanho = int(self.headers.get("Content-Length") or 0)
            if tamanho:
                self.rfile.read(tamanho)
            if not self._autorizado():
                print("[VIGIA] pedido de acordar RECUSADO (token).", flush=True)
                self._responder(401, {"erro": "não autorizado"})
                return
            self._responder(200, vigia.acordar())

        def log_message(self, *_args):
            """Silencia o log de acesso da stdlib. O que interessa (recusa, subida)
            este módulo imprime por conta própria, com contexto."""

    return Handler


def servir(raiz: Path, porta: Optional[int] = None) -> None:
    """Fica de plantão. Bloqueia — é o corpo do processo do vigia."""
    porta = porta or settings.vigia_port
    vigia = Vigia(raiz)
    httpd = ThreadingHTTPServer((settings.host, porta), _montar_handler(vigia))
    protegido = "com token" if settings.access_token else "só loopback"
    print(f"[VIGIA] de plantão em {settings.host}:{porta} ({protegido}); "
          f"o assistente responde na {settings.port} quando subir.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
