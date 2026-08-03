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
torch — medido), `acesso`, `rede` e o registro dos aparelhos entram, e os quatro
são de propósito minúsculos (medido em 2026-08-03: o registro custa +8 módulos e
nenhum import pesado — 268 ms → 249 ms de import, dentro do ruído).

SEGURANÇA: `acordar` é a única rota que FAZ algo, e ela exige credencial — a MESMA
regra do servidor grande, e desde 2026-08-03 isso inclui a identidade por aparelho.
Sem essa parte o vigia ficava um degrau atrás do resto do sistema, nos dois sentidos:
o aparelho REVOGADO continuava levantando o PC do dono pelo token compartilhado, e o
aparelho PAREADO (que já não guarda o token antigo) não conseguia levantar nada — o
celular migrado batia na porta do plantão e ouvia 401. A rota de status não tem gate
porque não revela nada além de "tem servidor de pé?" — o mesmo critério do
`/api/health`.
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
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:      # só para a anotação: `ssl` é carregado sob demanda, e este
    import ssl         # módulo existe para não carregar o que não vai usar

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

    def __init__(self, raiz: Path, relogio=None, registro=None) -> None:
        self.raiz = raiz
        self._relogio = relogio or __import__("time").monotonic
        self._mandou_subir_em: Optional[float] = None
        self._trava = threading.Lock()
        # Injetável para o teste; em produção nasce None e só é construído se a
        # identidade por aparelho estiver LIGADA (ver `_registro`).
        self._registro_aparelhos = registro
        # Trava PRÓPRIA, e não a do `acordar`: `threading.Lock` não é reentrante, e
        # o dia em que alguém autorizar de dentro do `acordar` o plantão travaria
        # para sempre — em silêncio, que é o pior jeito de um vigia falhar.
        self._trava_registro = threading.Lock()

    def servidor_de_pe(self) -> bool:
        return rede.porta_em_uso(settings.host, settings.port)

    # --- O gate ---------------------------------------------------------------
    def _registro(self):
        """Constrói o registro na PRIMEIRA necessidade, e uma vez só.

        Uma vez só porque o castigo progressivo por IP vive na RAM da instância:
        dois registros seriam dois contadores, e a força bruta ganharia o dobro de
        tentativas de graça. O `ThreadingHTTPServer` atende cada pedido numa thread,
        então a construção anda sob a mesma trava do `acordar`.
        """
        with self._trava_registro:
            if self._registro_aparelhos is None:
                from mente_digital.registro_aparelhos import RegistroAparelhos

                reg = RegistroAparelhos(settings.db_telemetria)
                reg.init()          # idempotente; o servidor grande faz o mesmo
                reg.configurar_bloqueio(settings.aparelhos_bloqueio_base_segundos,
                                        settings.aparelhos_bloqueio_teto_segundos)
                self._registro_aparelhos = reg
            return self._registro_aparelhos

    def autorizado(self, credencial: Optional[str], host: Optional[str]) -> bool:
        """Este pedido pode levantar o assistente?

        ⚠ Com a identidade DESLIGADA o caminho é o de sempre e não toca em disco: o
        plantão existe para ser barato, e abrir SQLite a cada tique da tela de
        carregamento do celular seria pagar por uma função que o dono não ligou.
        `RegistroAparelhos.autorizar(habilitado=False)` daria o mesmo veredito — mas
        depois de abrir o banco para descobrir que não precisava.
        """
        if not settings.aparelhos_habilitado:
            return acesso.cliente_autorizado(host, credencial, settings.access_token)
        return self._registro().autorizar(
            credencial, host, "/vigia/acordar",
            habilitado=True,
            token_legado=settings.access_token,
            aceita_token_legado=settings.aparelhos_token_legado,
        ).autorizado

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
            return vigia.autorizado(token, host)

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


class CertificadoInvalido(RuntimeError):
    """TLS pedido no `.env` e impossível de cumprir. Mata o plantão de propósito —
    ver o ⚠ de `contexto_tls`."""


class _Plantao(ThreadingHTTPServer):
    """O servidor do plantão, só para calar o traceback do handshake errado.

    Com TLS ligado, um cliente que ainda fale `http://` (o celular com a
    configuração antiga é o caso certo de acontecer) produz um `ssl.SSLError` por
    tentativa, e o `socketserver` responde a isso com um traceback inteiro. Num
    processo que existe para ficar calado meses a fio, isso é ruído que esconde o
    que importa. Vira UMA linha — que continua dizendo quem bateu e por quê; o
    resto segue com o traceback de sempre, porque erro engolido é o defeito que
    este projeto mais combate.
    """

    def handle_error(self, request, client_address):    # noqa: D102 - assinatura da stdlib
        import ssl
        import sys
        import traceback

        exc = sys.exc_info()[1]
        if isinstance(exc, ssl.SSLError):
            print(f"[VIGIA] handshake TLS recusado de {client_address[0]}: "
                  f"{getattr(exc, 'reason', None) or exc} (cliente falando HTTP puro?)",
                  flush=True)
            return
        traceback.print_exc()


def contexto_tls(cert: str, chave: str) -> Optional["ssl.SSLContext"]:
    """O MESMO par de certificados do servidor grande (MENTE_SSL_CERT/KEY), ou None.

    Por que o plantão precisa disto, e por que descobrir tarde sai caro: o app do
    celular DERIVA o endereço do vigia do endereço do assistente, trocando só a
    porta (`Endereco.vigia`, Android) — inclusive o ESQUEMA. No dia em que o dono
    ligar o HTTPS para destravar o microfone de fora de casa, o app passaria a
    falar `https://…:8765` com um vigia de HTTP puro, e a falha chegaria na tela
    como "o PC está desligado". Seria a AMBIGUIDADE que o vigia existe para matar,
    ressuscitada pelo conserto de outra coisa.

    E há o motivo direto: `acordar` é a única rota que carrega o token. Deixá-la em
    claro enquanto todo o resto vai cifrado seria proteger tudo menos a chave.

    `ssl` é stdlib — não fere a regra de não trazer peso para cá.

    ⚠ NÃO cai para HTTP quando o certificado está configurado e QUEBRADO — levanta.
    A 1ª versão caía, copiando o fail-soft do `main.py` ("vigia mudo é pior que
    vigia em claro"), e uma revisão adversária derrubou o argumento: se o servidor
    está configurado para TLS, o celular também está (ele DERIVA o esquema), então
    um plantão em HTTP puro **já está mudo para o celular** — o fallback não salva
    ninguém e só deixa a credencial de acordar em claro num socket que escuta em
    `0.0.0.0`. Pior: o aviso é um `print`, e no ÚNICO caminho de "sobe com o
    Windows" que este projeto oferece (`inicializacao.script_vbs` → `sh.Run …, 0,
    False`, janela oculta e sem redirecionamento) ele não tem console nem arquivo
    onde cair. Seria uma regressão de confidencialidade completamente silenciosa,
    meses depois, quando o cert expirasse — e o cert do Tailscale expira em 90
    dias com renovação manual.

    Caminho vazio continua sendo HTTP sem drama: "não configurei TLS" é uma
    ESCOLHA, não um erro. O que vira exceção é a contradição entre o que o `.env`
    pede e o que existe no disco.
    """
    import ssl

    if not (cert and chave):
        return None
    if not (os.path.exists(cert) and os.path.exists(chave)):
        raise CertificadoInvalido(
            f"MENTE_SSL_CERT/KEY apontam para arquivo inexistente ({cert!r}, {chave!r})")
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert, keyfile=chave)
        return ctx
    except CertificadoInvalido:
        raise
    except Exception as exc:
        raise CertificadoInvalido(f"não consegui carregar o certificado: {exc}") from exc


def registrar_falha(raiz: Path, mensagem: str) -> None:
    """Deixa o motivo em DISCO antes de morrer.

    O plantão roda sem console (ver o ⚠ acima), então `print` sozinho é o mesmo que
    silêncio: o dono veria só o app deixando de se encerrar, semanas depois, sem
    nenhum lugar para olhar. Um arquivo é o canal que sobrevive a um processo sem
    console — o mesmo recurso que `potencia_cpu` já usa para falar com o servidor.

    Best-effort de propósito: se nem o arquivo der para escrever, o processo ainda
    tem de morrer com a exceção original, não com uma exceção sobre o log.
    """
    try:
        destino = raiz / "dados"
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "vigia_erro.txt").write_text(mensagem, encoding="utf-8")
    except Exception as exc:                          # noqa: BLE001 - ver docstring
        print(f"[VIGIA] não consegui nem gravar o motivo da falha: {exc}", flush=True)


def servir(raiz: Path, porta: Optional[int] = None) -> None:
    """Fica de plantão. Bloqueia — é o corpo do processo do vigia."""
    porta = porta or settings.vigia_port
    # O certificado é resolvido ANTES de abrir a porta: falhar com o socket já no
    # ar deixaria a porta presa por um instante e, pior, um `server_close` a mais
    # no caminho de erro. Nada de rede acontece até o TLS estar decidido.
    try:
        ctx = contexto_tls(settings.ssl_cert, settings.ssl_key)
    except CertificadoInvalido as exc:
        registrar_falha(raiz, f"[VIGIA] {exc}\nO plantão NÃO subiu: o .env pede TLS e o "
                              f"certificado não serve. Sem isto, a credencial de acordar "
                              f"iria em claro num socket que escuta em 0.0.0.0.\n")
        print(f"[VIGIA] {exc} — plantão NÃO vai subir (motivo em dados/vigia_erro.txt).",
              flush=True)
        raise
    vigia = Vigia(raiz)
    httpd = _Plantao((settings.host, porta), _montar_handler(vigia))
    if ctx is not None:
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    protegido = "com token" if settings.access_token else "só loopback"
    esquema = "https" if ctx is not None else "http"
    print(f"[VIGIA] de plantão em {esquema}://{settings.host}:{porta} ({protegido}); "
          f"o assistente responde na {settings.port} quando subir.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
