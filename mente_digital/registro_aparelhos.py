"""
O registro dos aparelhos — a parte SUJA da identidade (SQLite, relógio, sessões vivas).

`aparelhos.py` decide; este módulo lembra, conta e executa. A divisão é a mesma de
`standby.py`/`SchedulerService` e de `agenda.py`/`scheduler.py`: a regra fica pura e
testável, e o que toca disco fica num lugar só.

O que mora aqui:

1. **Persistência** (tabelas `aparelhos` e `aparelhos_pareamento`, no MESMO arquivo
   SQLite da telemetria). Conexão própria em vez de métodos novos em `telemetry.Database`
   porque este é um assunto fechado — e o WAL já ligado lá torna o acesso concorrente
   seguro sem coordenação extra.

2. **Bloqueio progressivo por IP**, em RAM. Por que não em disco: cada tentativa errada
   viraria uma ESCRITA, e é o atacante quem escolhe quantas tentativas faz — um
   contador persistido é ele ditando o volume de I/O do servidor. O custo de ser RAM é
   honesto e fica dito: reiniciar o servidor zera o castigo. Quem reinicia o servidor é
   o dono, não o atacante.

3. **Revogação com efeito IMEDIATO.** Tirar a linha do banco não fecha um WebSocket que
   já está aberto — a sessão daquele celular seguiria falando com o assistente até ele
   desligar o app. Então cada sessão viva se registra aqui (`registrar_sessao`) e
   `revogar` CHAMA os derrubadores. Sem isso, "revogar" seria uma promessa que só vale
   na próxima conexão.

⚠ AUDITORIA — o que É e o que NÃO É gravado, de propósito. Uma linha por requisição HTTP
faria a SPA (que consulta várias rotas ao abrir) encher a tabela `auditoria` sozinha, e
daria ao atacante uma amplificação: cada 401 dele custaria uma escrita minha. Então:
recusa, pareamento e revogação SEMPRE entram; o acesso bem-sucedido entra ESTRANGULADO
(a 1ª vez de cada par aparelho+rota por janela) e o resto vive nas colunas `ultimo_uso`/
`ultimo_ip` da própria linha do aparelho. É o suficiente para "o que cada aparelho fez"
sem transformar a trilha num log de tráfego.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterator, Optional

from mente_digital import aparelhos as regras
from mente_digital import identidade
from mente_digital.aparelhos import Aparelho, Situacao, Veredito
from mente_digital.telemetry import db as db_padrao
from mente_digital.telemetry import telemetry

# Estrangulamento da trilha de acesso bem-sucedido (ver o ⚠ do cabeçalho).
JANELA_AUDITORIA_SEGUNDOS = 300.0
# Mesma ideia para o carimbo de último uso: gravar em toda requisição seria uma escrita
# por chamada de API. A precisão que o dono precisa ("quando este celular apareceu pela
# última vez") é de minutos, não de milissegundos.
JANELA_ULTIMO_USO_SEGUNDOS = 60.0


@dataclass(frozen=True)
class ResultadoPareamento:
    """O segredo sai daqui UMA vez e nunca mais: o banco guarda só a impressão."""

    ok: bool
    motivo: str
    credencial: Optional[str] = None
    aparelho_id: Optional[str] = None


class RegistroAparelhos:
    def __init__(self, path: str, relogio: Optional[Callable[[], datetime]] = None,
                 db=None) -> None:
        self.path = path
        self._agora = relogio or datetime.now
        # O banco da TRILHA é injetável para o teste não escrever na `auditoria` real
        # (o conftest já redireciona MENTE_DB_TELEMETRIA, mas depender disso acopla
        # este módulo à ordem de import dos testes — ver a lição #5 da consultoria TTFT).
        self._db = db if db is not None else db_padrao
        self._trava = threading.Lock()
        # Base e teto do castigo progressivo. Instância (não classe) porque dois
        # registros no mesmo processo — app e teste — não podem partilhar calibração.
        self._parametros_bloqueio: tuple[float, float] = (2.0, 900.0)
        # ip -> (falhas_consecutivas, bloqueado_ate)
        self._falhas: dict[str, tuple[int, Optional[datetime]]] = {}
        # ORÇAMENTO GLOBAL DE PAREAMENTO (2026-08-03, revisão de segurança).
        #
        # O castigo progressivo é por IP, e é ótimo contra UM atacante — mas é cego para
        # o distribuído: com N máquinas (botnet, rotação de IPv6), cada uma chega com o
        # contador zerado e ganha as 2 tentativas livres na hora. A conta que fizemos diz
        # que ainda assim é inviável (1.000 IPs contra 31^10 na janela de validade do
        # código dá P ≈ 1,3e-11) — ou seja, isto NÃO conserta um buraco aberto.
        #
        # Existe pelo motivo oposto: hoje nada LIMITA por desenho, e a folga vive de um
        # número (a entropia do código) permanecer o que é. Um dia alguém encurta o código
        # "para ficar mais fácil de ditar" e a defesa inteira cai sem que ninguém perceba,
        # porque a conta que a sustentava nunca esteve no código. Um teto global torna a
        # varredura impossível independentemente da entropia — e custa 10 linhas.
        #
        # Só conta PAREAMENTO (o elo de baixa entropia, digitado à mão). O gate normal
        # segue só com o castigo por IP: lá o segredo tem 256 bits, e um teto global ali
        # deixaria um atacante trancar os quatro usuários de fora — negação de serviço
        # servida de bandeja.
        # AVISAR O DONO quando alguém está martelando (decisão dele, 2026-08-03: "essas
        # notificações devem chegar no meu celular"). Ligado em `main.py` ao
        # `SchedulerService.alertar_seguranca`; None = ninguém escutando, e aí o castigo
        # segue mudo como sempre foi.
        #
        # ⚠ Por que um atributo opcional e não um import: este módulo é usado pelo VIGIA,
        # um processo de stdlib pura sem FastAPI nem scheduler — importar o scheduler aqui
        # arrastaria o mundo para dentro do plantão de 61 MB que existe justamente para
        # não ter isso. Mesma disciplina do `vigia.py`.
        #
        # O contrato (definido pelo lado que recebe): síncrono, nunca levanta, nunca
        # bloqueia — porque isto é chamado de DENTRO do gate de acesso.
        self.ao_alerta: Optional[Callable[[str, str, str], None]] = None
        self._pareamentos_falhos: list[datetime] = []
        # (tentativas, janela em segundos). 30 chutes errados em 10 min já é ordens de
        # grandeza acima do dedo trocado do dono ditando um código por telefone.
        self._teto_pareamento: tuple[int, float] = (30, 600.0)
        # aparelho_id -> {id_sessao: derrubador}
        self._sessoes: dict[str, dict[int, Callable[[], None]]] = {}
        self._proxima_sessao = 0
        # (aparelho_id, rota) -> instante do último registro na trilha
        self._auditado_em: dict[tuple[str, str], datetime] = {}
        self._uso_em: dict[str, datetime] = {}

    # --- Persistência ---------------------------------------------------------
    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Cópia deliberada de `telemetry.Database._conn` — mesma semântica de vida
        determinística e mesmo `timeout=10` (que é o busy_timeout). Não é reuso porque
        importar a classe só para o gerenciador de conexão acoplaria este módulo ao
        histórico de chat; a duplicação são 8 linhas e o WAL já é do arquivo."""
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        """Idempotente, como todas as migrações do projeto."""
        try:
            with self._conn() as conn:
                c = conn.cursor()
                c.execute(
                    """CREATE TABLE IF NOT EXISTS aparelhos
                       (id TEXT PRIMARY KEY, apelido TEXT, impressao TEXT, sal TEXT,
                        criado_em TEXT, ultimo_uso TEXT, ultimo_ip TEXT,
                        revogado_em TEXT, expira_em TEXT, usuario TEXT)"""
                )
                # O código vive na tabela para sobreviver a restart: o dono gera no PC,
                # anda até o celular e digita. Um código em RAM morreria num reload do
                # uvicorn no meio do caminho.
                c.execute(
                    """CREATE TABLE IF NOT EXISTS aparelhos_pareamento
                       (codigo TEXT PRIMARY KEY, emitido_em TEXT, usado_em TEXT,
                        apelido TEXT, usuario TEXT, validade_minutos INTEGER)"""
                )
                # Migração idempotente para banco que nasceu antes do multiusuário.
                # Mesmo padrão do `conversa_id` em telemetry.py: lê `PRAGMA table_info`
                # e só ALTERa o que falta. O backfill carimba o dono padrão — todo
                # aparelho que já estava pareado é dele.
                for tabela in ("aparelhos", "aparelhos_pareamento"):
                    self._garantir_coluna_usuario(c, tabela)
                self._garantir_coluna_validade(c)
        except Exception as exc:
            telemetry.error("APARELHOS", "Erro ao criar as tabelas do registro", exc)

    @staticmethod
    def _garantir_coluna_usuario(cursor, tabela: str) -> None:
        """Acrescenta `usuario` se faltar, e carimba o legado como do dono padrão.

        O backfill fica DENTRO do ramo do ALTER de propósito: rodando a cada boot, ele
        re-carimbaria como do dono qualquer linha que legitimamente nascesse sem usuário
        — é a mesma armadilha que o agente do SQLite descreveu na `auditoria`.
        """
        colunas = {c[1] for c in cursor.execute(f"PRAGMA table_info([{tabela}])")}
        if "usuario" in colunas:
            return
        cursor.execute(f"ALTER TABLE [{tabela}] ADD COLUMN usuario TEXT")
        cursor.execute(f"UPDATE [{tabela}] SET usuario = ? WHERE usuario IS NULL",
                       (identidade.DONO_PADRAO,))

    @staticmethod
    def _garantir_coluna_validade(cursor) -> None:
        """Acrescenta `validade_minutos` se faltar — a validade PRÓPRIA de um código.

        ⚠ SEM backfill, ao contrário do `usuario` logo acima, e a diferença é o ponto:
        aqui NULL SIGNIFICA alguma coisa ("este código não pediu validade própria, use o
        padrão do settings"). Carimbar os antigos com o valor de hoje congelaria neles um
        número que o dono ainda pode mudar no `.env` — e mudaria, retroativamente, o
        comportamento de códigos já emitidos, que é justamente o efeito que a validade
        por código existe para evitar.
        """
        colunas = {c[1] for c in cursor.execute("PRAGMA table_info([aparelhos_pareamento])")}
        if "validade_minutos" not in colunas:
            cursor.execute(
                "ALTER TABLE [aparelhos_pareamento] ADD COLUMN validade_minutos INTEGER")

    def _linha_para_aparelho(self, row: tuple) -> Aparelho:
        return Aparelho(
            id=row[0], apelido=row[1], impressao=row[2], sal=row[3], criado_em=row[4],
            ultimo_uso=row[5], ultimo_ip=row[6], revogado_em=row[7], expira_em=row[8],
            # Linha anterior à migração (ou banco de teste montado na mão) cai no dono
            # padrão. Nunca None: um aparelho sem usuário passaria pelo gate e morreria
            # depois, no `exigir_dono`, longe da causa.
            usuario=(row[9] if len(row) > 9 and row[9] else identidade.DONO_PADRAO),
        )

    def buscar(self, aparelho_id: str) -> Optional[Aparelho]:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT id, apelido, impressao, sal, criado_em, ultimo_uso, "
                    "ultimo_ip, revogado_em, expira_em, usuario FROM aparelhos WHERE id = ?",
                    (aparelho_id,),
                ).fetchone()
            return self._linha_para_aparelho(row) if row else None
        except Exception as exc:
            telemetry.error("APARELHOS", f"Erro ao buscar aparelho {aparelho_id}", exc)
            return None

    def listar(self, incluir_revogados: bool = False) -> list[Aparelho]:
        """O que o painel do dono mostra."""
        sql = ("SELECT id, apelido, impressao, sal, criado_em, ultimo_uso, ultimo_ip, "
               "revogado_em, expira_em, usuario FROM aparelhos")
        if not incluir_revogados:
            sql += " WHERE revogado_em IS NULL"
        sql += " ORDER BY criado_em"
        try:
            with self._conn() as conn:
                return [self._linha_para_aparelho(r) for r in conn.execute(sql).fetchall()]
        except Exception as exc:
            telemetry.error("APARELHOS", "Erro ao listar aparelhos", exc)
            return []

    def contar_ativos(self) -> int:
        return len(self.listar())

    # --- Inscrição (só o dono inicia) ----------------------------------------
    def emitir_codigo(self, apelido: str, teto: int, usuario: Optional[str] = None,
                      validade_minutos: Optional[int] = None) -> Optional[str]:
        """Gera o código de pareamento. Chamado a partir do PAINEL (loopback), nunca
        por um pedido remoto — um aparelho novo não pode pedir a própria entrada.

        None = teto atingido. O teto é conferido AQUI e de novo no `parear`: entre
        emitir e usar o código o dono pode ter pareado outro celular, e o teto que
        vale é o do momento em que a vaga é ocupada.

        `validade_minutos` é a validade DESTE código, e existe para o caso "convidar
        alguém que só vai parear mais tarde" não ter de alargar a janela de TODOS os
        códigos pelo `.env`. Alargar no settings tem três defeitos que isto não tem: vale
        para todo mundo, é RETROATIVO (a validade é conferida no pareamento, então
        ressuscita código antigo e esquecido), e exige lembrar de baixar de volta depois.
        None = o padrão do settings, que é o comportamento de sempre.
        """
        if not regras.pode_inscrever(self.contar_ativos(), teto):
            telemetry.warn("APARELHOS", f"Código recusado: teto de {teto} aparelhos atingido.")
            return None
        # O USUÁRIO é fixado AQUI, na emissão — é o ato explícito do dono dizendo para
        # quem é este aparelho. Fazer o celular escolher o próprio usuário na hora de
        # parear seria deixar quem chega decidir de qual memória vai ler.
        # Normaliza já: o nome vira pasta (`Pessoal/<dono>/`) e coleção do Chroma, e um
        # apelido inválido tem de falhar aqui, na mão do dono, não lá na frente.
        dono = identidade.normalizar(usuario) if usuario else identidade.DONO_PADRAO
        # Valida ANTES de gerar: um número fora da faixa tem de morrer na mão de quem
        # digitou, não virar uma linha no banco com um prazo que ninguém pediu. Mesmo
        # lugar em que o `identidade.normalizar` acima recusa apelido inválido.
        validade = regras.validar_validade(validade_minutos) if validade_minutos else None
        codigo = regras.gerar_codigo()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO aparelhos_pareamento "
                    "(codigo, emitido_em, usado_em, apelido, usuario, validade_minutos) "
                    "VALUES (?, ?, NULL, ?, ?, ?)",
                    (codigo, self._agora().isoformat(), apelido, dono, validade),
                )
            prazo = f" válido por {validade} min" if validade else ""
            telemetry.track(
                "APARELHOS",
                f"Código de pareamento emitido para '{apelido}' (usuário {dono}){prazo}.")
            return codigo
        except Exception as exc:
            telemetry.error("APARELHOS", "Erro ao emitir código de pareamento", exc)
            return None

    def parear(self, codigo_bruto: str, ip: str, teto: int, validade_minutos: int,
               expira_dias: int) -> ResultadoPareamento:
        """Troca um código válido pela credencial do aparelho. Uso ÚNICO.

        O código passa pelo MESMO bloqueio progressivo do gate: ele é a peça de baixa
        entropia do sistema (digitado à mão), então é aqui que a força bruta faria
        sentido — e é aqui que ela fica cara.
        """
        codigo = regras.normalizar_codigo(codigo_bruto)
        agora = self._agora()
        if self._bloqueado(ip, agora):
            self._auditar("aparelho_pareamento_recusado", f"ip={ip} motivo={regras.MOTIVO_BLOQUEADO}")
            return ResultadoPareamento(False, regras.MOTIVO_BLOQUEADO)
        # O teto GLOBAL vem depois do castigo por IP e antes de qualquer leitura: quem
        # varre com muitos IPs não paga nem o SELECT. Ver `_pareamentos_falhos`.
        if self._teto_pareamento_estourado(agora):
            self._auditar("aparelho_pareamento_recusado",
                          f"ip={ip} motivo=teto_global_pareamento")
            return ResultadoPareamento(False, regras.MOTIVO_BLOQUEADO)

        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT emitido_em, usado_em, apelido, usuario, validade_minutos "
                    "FROM aparelhos_pareamento WHERE codigo = ?",
                    (codigo,),
                ).fetchone()
        except Exception as exc:
            telemetry.error("APARELHOS", "Erro ao ler código de pareamento", exc)
            return ResultadoPareamento(False, regras.MOTIVO_CODIGO_INVALIDO)

        emitido = _iso_para_data(row[0]) if row else None
        # A validade do PRÓPRIO código vence a global quando ele pediu uma; NULL (todo
        # código anterior a esta coluna) cai no `validade_minutos` recebido do settings,
        # que é o comportamento de sempre. O `len(row)` guarda o banco montado à mão em
        # teste antigo, no mesmo espírito do `len(row) > 3` do usuário logo abaixo.
        validade = regras.validade_efetiva(
            row[4] if row and len(row) > 4 else None, validade_minutos)
        recusa = regras.codigo_valido(emitido, bool(row and row[1]), agora, validade)
        if recusa is not None:
            self._contar_falha(ip, agora)
            self._registrar_falha_pareamento(agora)
            self._auditar("aparelho_pareamento_recusado", f"ip={ip} motivo={recusa}")
            return ResultadoPareamento(False, recusa)

        if not regras.pode_inscrever(self.contar_ativos(), teto):
            self._auditar("aparelho_pareamento_recusado", f"ip={ip} motivo={regras.MOTIVO_TETO}")
            return ResultadoPareamento(False, regras.MOTIVO_TETO)

        aparelho_id = regras.gerar_id()
        segredo = regras.gerar_segredo()
        sal = regras.gerar_sal()
        expira = (agora + timedelta(days=expira_dias)).isoformat() if expira_dias > 0 else None
        try:
            with self._conn() as conn:
                # Marca o código como usado NA MESMA transação da inserção: se o
                # servidor cair entre as duas, um código de uso único teria virado
                # de uso duplo.
                # O usuário viaja do CÓDIGO para o APARELHO nesta mesma transação — é o
                # único momento em que ele é definido, e depois é imutável (ver o campo
                # `Aparelho.usuario`). Código de antes da migração cai no dono padrão.
                conn.execute(
                    "INSERT INTO aparelhos (id, apelido, impressao, sal, criado_em, "
                    "ultimo_uso, ultimo_ip, revogado_em, expira_em, usuario) "
                    "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                    (aparelho_id, row[2] or "aparelho", regras.impressao(segredo, sal), sal,
                     agora.isoformat(), expira,
                     (row[3] if len(row) > 3 and row[3] else identidade.DONO_PADRAO)),
                )
                conn.execute(
                    "UPDATE aparelhos_pareamento SET usado_em = ? WHERE codigo = ?",
                    (agora.isoformat(), codigo),
                )
        except Exception as exc:
            telemetry.error("APARELHOS", "Erro ao gravar o aparelho pareado", exc)
            return ResultadoPareamento(False, regras.MOTIVO_CODIGO_INVALIDO)

        self._limpar_falhas(ip)
        self._auditar("aparelho_pareado", f"id={aparelho_id} apelido={row[2]} ip={ip}")
        telemetry.track("APARELHOS", f"Aparelho '{row[2]}' pareado ({aparelho_id}).")
        return ResultadoPareamento(
            True, regras.MOTIVO_OK, regras.montar_credencial(aparelho_id, segredo), aparelho_id
        )

    # --- O gate ---------------------------------------------------------------
    def autorizar(self, credencial: Optional[str], host: Optional[str], rota: str,
                  habilitado: bool, token_legado: str, aceita_token_legado: bool) -> Veredito:
        """Resolve a `Situacao` (banco + rate limit) e delega a decisão às regras puras."""
        agora = self._agora()
        ip = host or "?"
        partes = regras.partir_credencial(credencial)
        ap: Optional[Aparelho] = None
        confere = False
        if habilitado and partes is not None:
            ap = self.buscar(partes[0])
            if ap is not None:
                confere = regras.confere_segredo(partes[1], ap.sal, ap.impressao)

        veredito = regras.avaliar(Situacao(
            habilitado=habilitado, host=host, credencial=credencial,
            token_legado=token_legado, aceita_token_legado=aceita_token_legado,
            agora=agora, aparelho=ap, segredo_confere=confere,
            bloqueado_ate=self._bloqueado_ate(ip),
        ))

        if veredito.autorizado:
            self._limpar_falhas(ip)
            if veredito.aparelho_id:
                self._marcar_uso(veredito.aparelho_id, ip, agora)
                self._auditar_acesso(veredito.aparelho_id, rota, ip, agora)
        else:
            # O legado negado NÃO conta falha: com a função ligada, uma requisição sem
            # credencial nenhuma (a SPA antes de configurar o token) cairia aqui em
            # sequência e bloquearia o dono da própria máquina.
            if veredito.motivo != regras.MOTIVO_SEM_CREDENCIAL:
                self._contar_falha(ip, agora)
            self._auditar_recusa(veredito, rota, ip, agora)
        return veredito

    # --- Revogação ------------------------------------------------------------
    def revogar(self, aparelho_id: str) -> bool:
        """Efeito imediato: marca no banco E derruba o que já está aberto."""
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE aparelhos SET revogado_em = ? WHERE id = ? AND revogado_em IS NULL",
                    (self._agora().isoformat(), aparelho_id),
                )
                mudou = cur.rowcount > 0
        except Exception as exc:
            telemetry.error("APARELHOS", f"Erro ao revogar {aparelho_id}", exc)
            return False
        if mudou:
            derrubadas = self.derrubar_sessoes(aparelho_id)
            self._auditar("aparelho_revogado", f"id={aparelho_id} sessoes_derrubadas={derrubadas}")
            telemetry.track("APARELHOS", f"Aparelho {aparelho_id} revogado ({derrubadas} sessões derrubadas).")
        return mudou

    def registrar_sessao(self, aparelho_id: Optional[str], derrubar: Callable[[], None]) -> Optional[int]:
        """Uma sessão viva (WebSocket) se anuncia. Devolve o id para o `encerrar_sessao`.

        `aparelho_id` None (loopback/token legado) não registra: não há o que revogar
        individualmente, e guardar sessão sem dono seria vazamento de memória.
        """
        if not aparelho_id:
            return None
        with self._trava:
            self._proxima_sessao += 1
            sid = self._proxima_sessao
            self._sessoes.setdefault(aparelho_id, {})[sid] = derrubar
            return sid

    def encerrar_sessao(self, aparelho_id: Optional[str], sid: Optional[int]) -> None:
        if not aparelho_id or sid is None:
            return
        with self._trava:
            vivas = self._sessoes.get(aparelho_id)
            if vivas is not None:
                vivas.pop(sid, None)
                if not vivas:
                    self._sessoes.pop(aparelho_id, None)

    def derrubar_sessoes(self, aparelho_id: str) -> int:
        """Chama cada derrubador. Um que exploda NÃO pode impedir os outros de cair —
        revogação parcial é pior que nenhuma, porque o dono acredita ter fechado a porta."""
        with self._trava:
            vivas = list(self._sessoes.pop(aparelho_id, {}).values())
        n = 0
        for derrubar in vivas:
            try:
                derrubar()
                n += 1
            except Exception as exc:
                telemetry.error("APARELHOS", f"Erro ao derrubar sessão de {aparelho_id}", exc)
        return n

    def sessoes_vivas(self, aparelho_id: str) -> int:
        with self._trava:
            return len(self._sessoes.get(aparelho_id, {}))

    # --- Bloqueio progressivo (RAM) ------------------------------------------
    def _bloqueado_ate(self, ip: str) -> Optional[datetime]:
        with self._trava:
            return self._falhas.get(ip, (0, None))[1]

    def _bloqueado(self, ip: str, agora: datetime) -> bool:
        return regras.bloqueado(self._bloqueado_ate(ip), agora)

    def _contar_falha(self, ip: str, agora: datetime) -> None:
        base, teto = self._parametros_bloqueio
        with self._trava:
            n = self._falhas.get(ip, (0, None))[0] + 1
            atraso = regras.atraso_bloqueio(n, base, teto)
            ate = agora + timedelta(seconds=atraso) if atraso > 0 else None
            self._falhas[ip] = (n, ate)
        if atraso > 0:
            telemetry.warn("APARELHOS", f"{ip}: {n} falhas — bloqueado por {atraso:.0f}s.")
            # Só quando o castigo JÁ disparou, não a cada falha: as 2 primeiras são o
            # dedo trocado do próprio dono digitando, e avisar nelas ensinaria a ignorar
            # o aviso. O estrangulamento fino é do outro lado (janela por chave).
            self._avisar_alerta(
                f"{n} tentativas de acesso recusadas",
                f"IP {ip} — bloqueado por {atraso:.0f}s. Se não foi você, revogue os "
                f"aparelhos que não reconhecer.",
                f"auth:{ip}",
            )

    def _avisar_alerta(self, titulo: str, detalhe: str, chave: str) -> None:
        """Chama o ouvinte, se houver. Uma falha aqui NÃO pode derrubar o gate: quem
        está no meio disto é uma requisição sendo recusada, e trocar uma recusa
        correta por um 500 seria transformar o alarme na porta de entrada."""
        ouvinte = self.ao_alerta
        if ouvinte is None:
            return
        try:
            ouvinte(titulo, detalhe, chave)
        except Exception as exc:
            telemetry.error("APARELHOS", "Falha ao emitir alerta de segurança", exc)

    def _registrar_falha_pareamento(self, agora: datetime) -> None:
        """Anota uma tentativa de pareamento errada, SEM olhar de que IP veio."""
        _tentativas, janela = self._teto_pareamento
        with self._trava:
            corte = agora - timedelta(seconds=janela)
            # Poda ao inserir: a lista nunca cresce além da janela, então não é preciso
            # um limpador em background para um processo que fica dias no ar.
            self._pareamentos_falhos = [
                t for t in self._pareamentos_falhos if t >= corte] + [agora]

    def _teto_pareamento_estourado(self, agora: datetime) -> bool:
        tentativas, janela = self._teto_pareamento
        if tentativas <= 0:
            return False                       # 0 = sem teto global (escape hatch)
        corte = agora - timedelta(seconds=janela)
        with self._trava:
            recentes = sum(1 for t in self._pareamentos_falhos if t >= corte)
        if recentes >= tentativas:
            telemetry.warn(
                "APARELHOS",
                f"Teto GLOBAL de pareamento atingido: {recentes} tentativas erradas em "
                f"{janela:.0f}s. Pareamento suspenso — sinal de varredura distribuída."
            )
            return True
        return False

    def configurar_teto_pareamento(self, tentativas: int, janela_segundos: float) -> None:
        """Instância, não classe: teste e app no mesmo processo não partilham calibração
        (mesma razão de `configurar_bloqueio`)."""
        self._teto_pareamento = (tentativas, janela_segundos)

    def _limpar_falhas(self, ip: str) -> None:
        with self._trava:
            self._falhas.pop(ip, None)

    def configurar_bloqueio(self, base_segundos: float, teto_segundos: float) -> None:
        self._parametros_bloqueio = (base_segundos, teto_segundos)

    # --- Trilha ---------------------------------------------------------------
    def _auditar(self, acao: str, detalhe: str) -> None:
        self._db.registrar_auditoria(acao, detalhe)

    def _auditar_recusa(self, veredito: Veredito, rota: str, ip: str, agora: datetime) -> None:
        """Recusa SEMPRE entra — mas estrangulada pelo mesmo par (ip, motivo), senão um
        flood de 401 vira um flood de INSERT que o atacante controla."""
        chave = (f"recusa:{ip}", veredito.motivo)
        with self._trava:
            ultimo = self._auditado_em.get(chave)
            if ultimo is not None and (agora - ultimo).total_seconds() < JANELA_AUDITORIA_SEGUNDOS:
                return
            self._auditado_em[chave] = agora
        alvo = veredito.aparelho_id or "-"
        self._auditar("acesso_recusado", f"motivo={veredito.motivo} ip={ip} rota={rota} aparelho={alvo}")

    def _auditar_acesso(self, aparelho_id: str, rota: str, ip: str, agora: datetime) -> None:
        chave = (aparelho_id, rota)
        with self._trava:
            ultimo = self._auditado_em.get(chave)
            if ultimo is not None and (agora - ultimo).total_seconds() < JANELA_AUDITORIA_SEGUNDOS:
                return
            self._auditado_em[chave] = agora
        self._auditar("acesso_aparelho", f"aparelho={aparelho_id} rota={rota} ip={ip}")

    def _marcar_uso(self, aparelho_id: str, ip: str, agora: datetime) -> None:
        """Carimba último uso/IP — estrangulado (ver JANELA_ULTIMO_USO_SEGUNDOS)."""
        with self._trava:
            ultimo = self._uso_em.get(aparelho_id)
            if ultimo is not None and (agora - ultimo).total_seconds() < JANELA_ULTIMO_USO_SEGUNDOS:
                return
            self._uso_em[aparelho_id] = agora
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE aparelhos SET ultimo_uso = ?, ultimo_ip = ? WHERE id = ?",
                    (agora.isoformat(), ip, aparelho_id),
                )
        except Exception as exc:
            telemetry.error("APARELHOS", f"Erro ao carimbar uso de {aparelho_id}", exc)


def _iso_para_data(valor: Optional[str]) -> Optional[datetime]:
    """Data ilegível vira None, e None o `codigo_valido` já trata como inválido —
    fail-closed: um campo corrompido não pode virar código eterno."""
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None
