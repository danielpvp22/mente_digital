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

from mente_digital import acesso, jogo_ativo, rede, vez
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
    estado: str          # "ja_de_pe" | "subindo" | "ocupado" | "subindo_agora"


def decidir(servidor_de_pe: bool, subindo_ha: Optional[float],
            jogo: Optional[str] = None) -> Veredito:
    """Este pedido deve levantar o `app.py`?

    Quatro respostas, e as quatro importam para o celular: já está de pé (entra
    direto), já mandei subir (mostre a tela de carregamento e espere), o PC está
    ocupado (não vai subir agora — deixe o recado) e vou subir agora. Puro/testável,
    como `standby.avaliar`.

    ⚠ A ORDEM É DELIBERADA e cada troca quebra um caso:
    - `ja_de_pe` VENCE o jogo. Se o assistente já está no ar, o jogo não é da
      conta de ninguém: a pessoa já podia usar um segundo atrás, e recusar agora
      seria derrubá-la do nada.
    - `subindo` VENCE o jogo. Aqui já mandamos subir (talvez antes de o jogo
      abrir); dizer "ocupado" seria MENTIRA — o app está vindo de qualquer jeito,
      e o celular ficaria mostrando "não deu" enquanto a tela do PC acende.
    - `ocupado` vem por último, e é o único que não tem volta neste tique.

    ⚠ O estado é `ocupado`, NÃO `jogo`. O nome atravessa a rede até o aparelho de
    outra pessoa; contar a ela o que o dono está fazendo é o espelho do vazamento
    que o mensageiro inteiro foi desenhado para evitar. Ver `vez.texto_ao_pedinte`.
    """
    if servidor_de_pe:
        return Veredito(False, "ja_de_pe")
    if subindo_ha is not None and subindo_ha < SEGUNDOS_SUBINDO:
        return Veredito(False, "subindo")
    if jogo:
        return Veredito(False, "ocupado")
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
        # O jogo da passada ANTERIOR. `vez.deve_liberar` age na borda, não no
        # estado — sem esta lembrança não há transição para detectar.
        self._ultimo_jogo: Optional[str] = None
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

    # --- O jogo, e o recado que o pedido deixa --------------------------------
    def jogo_agora(self) -> Optional[str]:
        """Que jogo está aberto, ou None. Fail-soft: erro aqui devolve None e o
        plantão volta a ser o de sempre — recusar o dono por causa de uma leitura
        de processos que falhou seria trocar um conforto por uma tranca."""
        if not settings.vigia_respeita_jogo:
            return None
        try:
            alvos = jogo_ativo.JOGOS_PADRAO
            extras = [j.strip() for j in settings.vigia_jogos_extras.split(",") if j.strip()]
            return jogo_ativo.detectar(jogo_ativo.processos_em_execucao(),
                                       set(alvos) | set(extras))
        except Exception as exc:                      # noqa: BLE001
            print(f"[VIGIA] não consegui olhar os processos: {exc}", flush=True)
            return None

    def _arquivo_pedidos(self) -> Path:
        return vez.arquivo_pedidos(self.raiz)

    def registrar_pedido(self, usuario: str, aparelho: str = "") -> bool:
        """Deixa o bilhete de quem quis entrar. O pedido NÃO PODE SE PERDER.

        ⚠ ARQUIVO, e não o mensageiro. Escrever a mensagem daqui exigiria
        `telemetry.Database`, e o valor deste processo é ser barato — há teste em
        subprocesso que falha se peso entrar. O assistente drena este arquivo
        quando sobe: a decisão de virar conversa é dele, que já sabe fazê-la.

        Append e não reescrita: dois celulares podem bater juntos (o
        `ThreadingHTTPServer` atende cada um numa thread), e `open(..., 'a')` com
        uma linha por vez é o que o sistema de arquivos serializa por nós.
        """
        try:
            caminho = self._arquivo_pedidos()
            caminho.parent.mkdir(parents=True, exist_ok=True)
            pedido = vez.Pedido(usuario or vez.ANONIMO, vez.agora_iso(), aparelho)
            with open(caminho, "a", encoding="utf-8") as fh:
                fh.write(vez.linha(pedido) + "\n")
            return True
        except Exception as exc:                      # noqa: BLE001
            print(f"[VIGIA] não consegui guardar o pedido: {exc}", flush=True)
            return False

    def tem_pedido_pendente(self) -> bool:
        try:
            return self._arquivo_pedidos().stat().st_size > 0
        except OSError:
            return False

    def tique_do_jogo(self) -> bool:
        """Uma passada do plantão. Devolve se levantou o assistente.

        É aqui que a promessa "quando o jogo fechar, eu te aviso" se cumpre: o
        assistente está DESLIGADO nessa hora (é a premissa toda), então não há
        ninguém além do vigia para notar que o jogo saiu.

        A decisão mora em `vez.deve_liberar`, pura, e é uma BORDA: só a transição
        de "tinha jogo" para "não tem" dispara. Sem isso cada passada tentaria
        subir um app já de pé.
        """
        agora = self.jogo_agora()
        antes, self._ultimo_jogo = self._ultimo_jogo, agora
        if not vez.deve_liberar(agora, antes, self.tem_pedido_pendente()):
            return False
        print(f"[VIGIA] {antes} fechou e havia gente esperando — subindo o assistente.",
              flush=True)
        with self._trava:
            if self.servidor_de_pe():
                return False
            if not subir_app(self.raiz):
                return False
            self._mandou_subir_em = self._relogio()
        return True

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
        return self._avaliar(credencial, host).autorizado

    def _avaliar(self, credencial: Optional[str], host: Optional[str]):
        return self._registro().autorizar(
            credencial, host, "/vigia/acordar",
            habilitado=True,
            token_legado=settings.access_token,
            aceita_token_legado=settings.aparelhos_token_legado,
        )

    def quem_e(self, credencial: Optional[str], host: Optional[str]) -> tuple[str, str]:
        """(usuário, aparelho) de um pedido JÁ AUTORIZADO. Só para o recado.

        ⚠ Não decide nada — quem decide é `autorizado`, que já rodou. Isto existe
        porque o recado ao dono precisa de um NOME, e o único nome confiável é o
        que o gate devolve. Perguntá-lo ao cliente deixaria qualquer um assinar o
        pedido como outra pessoa.

        ⚠ Com a identidade DESLIGADA não há nome nenhum a dar: o token legado é o
        mesmo para todos, e por construção não deixa rastro de quem. Devolve
        vazio, e o recado vira "alguém quis usar" — que é a verdade disponível,
        não um palpite.
        """
        if not settings.aparelhos_habilitado:
            return "", ""
        try:
            v = self._avaliar(credencial, host)
            return (v.usuario or ""), (v.aparelho_id or "")
        except Exception as exc:                      # noqa: BLE001
            print(f"[VIGIA] não consegui identificar quem pediu: {exc}", flush=True)
            return "", ""

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

    def acordar(self, usuario: str = "", aparelho: str = "") -> dict:
        """Levanta o app se preciso. Serializado: dois celulares (ou dois tiques
        da mesma tela) chegando juntos não podem disparar dois `app.py`."""
        with self._trava:
            jogo = self.jogo_agora()
            veredito = decidir(self.servidor_de_pe(), self._subindo_ha(), jogo)
            if veredito.estado == "ocupado":
                # O recado é a razão de a recusa ser aceitável. Sem ele o pedinte
                # ouve "não" e o dono nunca fica sabendo que alguém quis entrar —
                # que é o estado de hoje, só que agora com o "não" explícito.
                self.registrar_pedido(usuario, aparelho)
                self._ultimo_jogo = jogo      # a borda começa a contar daqui
                return {"estado": "ocupado", "porta_servidor": settings.port,
                        "aviso": vez.texto_ao_pedinte(False)}
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
            # De QUEM é este pedido, para o recado ao dono ("a Ana tentou 3x").
            # ⚠ A identidade sai da CREDENCIAL já autenticada, nunca de um
            # cabeçalho que o cliente escolhe — senão qualquer um poderia assinar
            # o pedido com o nome de outro, e o dono decidiria sobre uma
            # identidade inventada. É o mesmo princípio de o gate DEVOLVER o
            # usuário em vez de o chamador declará-lo (ver `aparelhos.Veredito`).
            token = self.headers.get("X-Mente-Token")
            host = self.client_address[0] if self.client_address else None
            quem, aparelho = vigia.quem_e(token, host)
            self._responder(200, vigia.acordar(quem, aparelho))

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

    ⚠ E LIMITA AS THREADS (revisão de segurança, 2026-08-03). O `ThreadingHTTPServer`
    cria uma thread por conexão, SEM teto, e a thread nasce ANTES de o gate rodar — logo,
    o custo é pago por quem ainda não provou nada. Num processo que existe para viver com
    dezenas de MB e cuja única função é levantar o PC de longe, uma enxurrada de conexões
    (nem precisa ser lenta) o derruba por exaustão de thread. O ataque nega exatamente a
    função do plantão, e do jeito mais silencioso possível: você tenta acordar o PC pelo
    celular, não acontece nada, e a tela diz "o PC está desligado".

    A defesa é um SEMÁFORO, não um pool: conexão que chega com o teto cheio ESPERA um
    slot e é atendida (ou o cliente desiste), em vez de ganhar uma thread própria. 32 é
    ordens de grandeza acima do uso real — são quatro celulares perguntando o status na
    tela de carregamento — e ordens de grandeza abaixo do que esgota o processo.
    """

    # Teto de conexões simultâneas. `ThreadingHTTPServer` não tem esse conceito, então
    # ele é imposto no `process_request` abaixo.
    MAX_CONEXOES = 32
    # daemon_threads herdado do ThreadingHTTPServer: thread pendurada não impede o
    # processo de morrer quando o assistente sobe e o plantão sai de cena.

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._vagas = threading.Semaphore(self.MAX_CONEXOES)

    def process_request(self, request, client_address):     # noqa: D102 - stdlib
        # Segura ANTES de criar a thread — segurar depois seria criar a thread que
        # queremos evitar. Timeout para o atacante de conexões lentas não transformar
        # espera em enfileiramento infinito: sem vaga em 5s, a conexão é descartada.
        if not self._vagas.acquire(timeout=5.0):
            self.handle_error_de_lotacao(client_address)
            # ⚠ `super().shutdown_request`, NÃO o override abaixo: aqui a vaga nunca foi
            # adquirida, e passar pelo override a LIBERARIA — o contador do semáforo
            # cresceria a cada recusa e o teto viraria ficção justamente sob ataque, que
            # é a única hora em que ele importa.
            super().shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._vagas.release()   # a thread não chegou a nascer: devolve a vaga aqui
            raise

    def shutdown_request(self, request):                    # noqa: D102 - stdlib
        try:
            super().shutdown_request(request)
        finally:
            # Devolvido no FIM de cada conexão atendida. Fica no `shutdown_request`
            # (e não no handler) porque é o ponto por onde a stdlib passa em TODOS os
            # caminhos de encerramento, inclusive quando o handler estoura.
            self._vagas.release()

    def handle_error_de_lotacao(self, client_address) -> None:
        """Recusa por lotação é EVENTO, não silêncio: é o sinal de que alguém está
        martelando o plantão.

        Sai por `print`, como a recusa de handshake TLS logo abaixo — é a convenção
        deste módulo. ⚠ Com honestidade sobre o limite: o plantão sobe pelo `.vbs` com
        janela oculta e sem redirecionamento, então na prática este texto cai no vazio.
        Vale por consistência e para quem rodar o vigia à mão depurando; o canal que o
        dono realmente vê é o alerta do assistente (`alertar_seguranca`), e ele não
        existe aqui porque este processo não tem — de propósito — nada do projeto
        dentro dele além do registro de aparelhos.
        """
        de_onde = client_address[0] if client_address else "?"
        print(f"[VIGIA] plantão lotado ({self.MAX_CONEXOES} conexões) — "
              f"conexão de {de_onde} descartada.", flush=True)

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
    parar = _vigiar_o_jogo(vigia)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
        httpd.server_close()


#: De quanto em quanto o plantão olha se o jogo fechou. Generoso de propósito: a
#: leitura custa um snapshot de processos (Toolhelp), o plantão existe para ser
#: barato, e ninguém percebe a diferença entre ser avisado 10 s ou 40 s depois de
#: uma partida acabar. É o oposto da régua do VAD, onde 200 ms se ouvem.
SEGUNDOS_ENTRE_TIQUES = 20.0


def _vigiar_o_jogo(vigia: Vigia, intervalo: float = SEGUNDOS_ENTRE_TIQUES):
    """Thread que cumpre a metade "eu te aviso quando o jogo fechar".

    ⚠ PRECISA existir aqui e não no assistente: nessa hora o assistente está
    DESLIGADO — é a premissa inteira da função. Não há mais ninguém no processo
    para notar que o jogo saiu.

    `daemon=True` porque o dono do processo é o `serve_forever`: quando ele cai
    (Ctrl-C, logoff), esta thread não pode segurar o encerramento por até um
    intervalo inteiro. E o `Event.wait` no lugar de `sleep` para que a saída seja
    imediata em vez de esperar o tique corrente.
    """
    parar = threading.Event()
    if not settings.vigia_respeita_jogo:
        return parar

    def _laco() -> None:
        while not parar.wait(intervalo):
            try:
                vigia.tique_do_jogo()
            except Exception as exc:                  # noqa: BLE001
                # Uma falha de fundo JAMAIS derruba o plantão: o valor dele é
                # estar de pé quando o celular bater, e um erro ao olhar
                # processos não pode custar isso. Mesma régua da pesquisa
                # agendada no `scheduler`.
                print(f"[VIGIA] tique do jogo falhou: {exc}", flush=True)

    threading.Thread(target=_laco, name="vigia-jogo", daemon=True).start()
    return parar
