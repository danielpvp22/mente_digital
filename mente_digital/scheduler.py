"""
SchedulerService — a "responsabilidade contínua" dos agentes (o que a Alexa faz):
disparar algo SOZINHO no futuro, sem o usuário perguntar.

Por que existe (e por que não dava para reusar o ETL idle): o resto do servidor só
"fala" DENTRO de um pipeline que o usuário disparou. Um alarme quebra esse pressuposto
— ele parte do relógio, não de uma mensagem. E o `run_idle` é oportunista (roda quando
o chat para), não um cron. Aqui há um loop próprio que lê a tabela `agendamentos`
(persistente: sobrevive a restart) e faz PUSH para as sessões vivas.

Tipos de agendamento (coluna `tipo`):
- 'lembrete' : alarme/timer/lembrete. Dispara uma fala no horário. Pode recorrer.
- 'watcher'  : "me avise quando X". Recorrente por intervalo; a cada checagem busca na
               web e pergunta ao LLM se a condição já é verdadeira; ao SIM, avisa e encerra.
- 'briefing' : "flash briefing" diário. Monta uma fala curta com os temas do usuário.
- 'pomodoro' : ciclo foco<->pausa; cada disparo anuncia a transição e reprograma a próxima.
- 'seguranca': alerta para o MESTRE que não achou ninguém conectado. Não nasce de um
               pedido do usuário — é a fila durável reusada para o push não se perder.
- 'mensagem' : aviso do mensageiro (usuário -> MESTRE e a resposta dele) que não achou
               o destinatário conectado. Também não é um pedido do usuário: a mensagem
               em si vive na tabela `mensagens`, e aqui está só o CARTEIRO — a mesma
               fila, pelo mesmo motivo (ela já sabe reentregar, esperar o ack e não
               duplicar). Ver mensageiro.py.

Pilares respeitados:
- GPU serializada: watcher e briefing usam o LLM só depois de `interactive_idle.wait()`
  e com `preemptible=True` — a conversa ao vivo sempre passa na frente (igual ao ETL).
- Nada de push sem ouvinte: se ninguém está conectado, o disparo vira 'pendente_entrega'
  e é entregue na próxima conexão (o próprio loop reentrega, e o WS chama no accept).

MULTIUSUÁRIO (2026-08-03): este é o único serviço do projeto que age SEM um turno — ele
parte do relógio, não de uma pergunta —, e por isso é o que mais depende do contrato do
`identidade.py`. Duas regras nasceram daí:
  (1) as consultas do `tick` usam `todos_os_donos=True`. É a exceção deliberada ao
      filtro por dono: o loop não tem dono no contexto, então a consulta filtrada
      levantaria `DonoIndefinido` com a flag ligada, o `except` do `run_forever`
      engoliria, e os alarmes de TODAS as quatro pessoas parariam EM SILÊNCIO.
  (2) todo disparo entra em `identidade.usar_dono(ag["dono"])` antes de qualquer coisa.
      É esse `with` que faz o reagendamento da recorrência, a auditoria, as lacunas do
      briefing e — sobretudo — a ESCOLHA DE PARA QUEM FALAR caírem na pessoa certa.
O oposto de (1) é o alarme que não toca; o oposto de (2) é o lembrete de um tocando no
celular do outro. Os dois defeitos são silenciosos, e é por isso que ambos têm teste.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from pathlib import Path

from mente_digital import agenda
from mente_digital import backup
from mente_digital import calendario
from mente_digital import energia
from mente_digital import identidade
from mente_digital import mensageiro
from mente_digital import potencia
from mente_digital import tomada
from mente_digital import prompts
from mente_digital import standby
from mente_digital import textutils
from mente_digital import vram
from mente_digital.config import BASE_DIR, settings
from mente_digital.rag import NENHUM
from mente_digital.telemetry import db, telemetry

if TYPE_CHECKING:
    from mente_digital.state import AppContext

# Estrangulamento do alerta de segurança para o MESTRE. Mesma janela e mesmo espírito do
# `registro_aparelhos.JANELA_AUDITORIA_SEGUNDOS`, pelo mesmo motivo: sem ela, QUEM ESCOLHE
# quantas notificações o dono recebe é o atacante — cada 401 dele viraria um push no
# celular e uma linha no SQLite, e a defesa vira amplificador. Constante de módulo (e não
# campo de config) porque é um piso de segurança, não uma preferência a calibrar.
JANELA_ALERTA_SEGURANCA_SEGUNDOS = 300.0
# Teto do dicionário de estrangulamento: a chave é o IP, e um atacante que roda IPs faria
# esse dict crescer sem fim. Podar o que já expirou é O(n) e só acontece no estouro.
MAX_CHAVES_ALERTA = 512


class SchedulerService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._parado = asyncio.Event()
        # #28: detector de vazamento de VRAM, alimentado a cada tick.
        self._monitor_vram = vram.MonitorVram(
            settings.vram_leak_amostras, settings.vram_leak_slack_bytes
        )
        # Estado do LLM no tick anterior: um flip (unload<->reload no ciclo de idle)
        # muda o patamar de VRAM legitimamente — a janela do detector recomeça (reset).
        self._llama_ready_anterior: Optional[bool] = None
        # ACK de aplicação (painel 2026-07): pushes ENVIADOS aguardando a confirmação
        # do cliente — ack_id -> (agendamento, enviado_em). Só RAM: num restart, o
        # status durável já é pendente_entrega (pessimista), então nada se perde —
        # no pior caso o usuário ouve o aviso duas vezes, nunca zero.
        self._aguardando_ack: dict[str, tuple[dict, datetime]] = {}
        # Pesquisa proativa AGENDADA (#1): quando a última passada rodou e se há uma em
        # voo. Só RAM — um restart apenas adia a 1ª passada em um intervalo, sem perda.
        self._ultima_pesquisa_idle: Optional[datetime] = None
        self._pesquisa_em_andamento = False
        # Backup diário (ops-backup-01): flag anti-sobreposição; a idempotência
        # entre restarts é o NOME do arquivo (mente_AAAA-MM-DD.zip existir = já rodou).
        self._backup_em_andamento = False
        # Ingestão de livros (Fase 1, 2026-07-25): uma passada por vez; a fila
        # durável são os próprios arquivos em dados/ingestao/pendentes.
        self._ingestao_em_andamento = False
        # Consolidação de átomos (Fase 2): 1x/dia, nunca com sessão viva. Só RAM —
        # restart apenas adia a 1ª passada, e a operação é idempotente/arquivada.
        self._ultima_consolidacao: Optional[datetime] = None
        self._consolidacao_em_andamento = False
        # Colheita acadêmica (Fase 4): 1x/dia, sem sessão viva, OPT-IN. O dedup
        # durável de URL vive no SQLite, então restart não re-baixa nada.
        self._ultima_academico: Optional[datetime] = None
        self._academico_em_andamento = False
        # OCR de livro escaneado (Fase 3): a fila são os PDFs em aguardando_ocr/; o
        # progresso por página vive em disco, então restart não perde transcrição.
        self._ocr_em_andamento = False
        # Modo economia automático (2026-08-02): anti-sobreposição e o último motivo
        # impresso — o watcher roda a cada tick e repetir "faltam 840s" no log seria
        # ruído que esconde a linha que importa.
        self._standby_em_andamento = False
        self._ultimo_motivo_standby = ""
        # Alerta de segurança (2026-08-03): chave de estrangulamento -> instante do
        # último alerta. Só RAM, e isso basta: num restart o pior caso é o dono receber
        # um aviso a mais — o oposto (perder o aviso) é que não pode acontecer.
        self._alertado_em: dict[str, datetime] = {}
        # ⚠ O ÚNICO estado deste serviço tocado de FORA do event loop: `alertar_seguranca`
        # é chamada do gate de acesso, que roda no pool de threads do `asyncio.to_thread`
        # — vários workers, em paralelo, sob rajada. Sem a trava, a poda do dicionário
        # (que itera) contra uma inserção de outra thread levanta "dictionary changed size
        # during iteration": o `_avisar_alerta` engoliria, e o alerta se perderia no meio
        # de um ataque. Todo o resto aqui é single-threaded no loop e não precisa dela.
        self._trava_alerta = threading.Lock()

    # -- loop principal ---------------------------------------------------------
    async def run_forever(self) -> None:
        """Loop de background. Tolera erro por tick (um agendamento ruim não derruba
        o serviço) e dorme `scheduler_tick_seconds` entre passadas."""
        telemetry.track("SCHEDULER", "Agendador iniciado.")
        while not self._parado.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                telemetry.error("SCHEDULER", "Erro no tick do agendador", exc)
            try:
                await asyncio.wait_for(self._parado.wait(), timeout=settings.scheduler_tick_seconds)
            except asyncio.TimeoutError:
                pass

    def parar(self) -> None:
        self._parado.set()

    async def tick(self, agora: Optional[datetime] = None) -> None:
        agora = agora or datetime.now()
        await self._probe_vram()  # #28/#29: amostra a VRAM 1x por tick
        await self._amostrar_consumo()   # wattímetro: energia acumulada por dia
        # ⚠ `todos_os_donos=True` nas DUAS leituras: ver a regra (1) do cabeçalho. Este
        # loop roda FORA de turno; a consulta filtrada levantaria `DonoIndefinido` com a
        # flag ligada e pararia os alarmes de todo mundo sem um único erro visível. O
        # roteamento acontece linha a linha, dentro do `_disparar`/`_entregar_pendente`.
        vencidos = await asyncio.to_thread(
            db.get_agendamentos_vencidos, agora.isoformat(), todos_os_donos=True)
        for ag in vencidos:
            await self._disparar(ag, agora)
        # Reentrega o que ficou pendente por falta de ouvinte — mas só se agora há alguém.
        # O gate é "QUALQUER sessão viva" só para evitar a leitura do banco com a casa
        # vazia; quem decide se ESTA linha pode ser falada é o dono dela, lá dentro.
        if self._ha_sessoes():
            for ag in await asyncio.to_thread(db.get_agendamentos_pendentes,
                                              todos_os_donos=True):
                await self._entregar_pendente(ag, agora)
        # Pesquisa proativa AGENDADA (#1): dispara em BACKGROUND para NÃO atrasar o loop
        # de alarmes — uma passada leva dezenas de segundos (web + LLM), e um lembrete não
        # pode esperar por isso. A flag/última-passada guardam contra sobreposição.
        if self._pesquisa_idle_devida(agora):
            self._ultima_pesquisa_idle = agora
            self._pesquisa_em_andamento = True
            self.ctx.track_task(self._executar_pesquisa_idle())
        # Backup diário (painel 2026-07-24, ops-backup-01): o vault é a ÚNICA cópia
        # do conhecimento destilado. Em background (zip não pode atrasar um alarme).
        if self._backup_devido(agora):
            self._backup_em_andamento = True
            self.ctx.track_task(self._executar_backup(agora))
        # Ingestão de livros (Fase 1): consome jobs de capítulo SÓ no idle total —
        # nunca com sessão viva (restrição do dono: atomização não compete com a fala).
        if self._ingestao_devida(agora):
            self._ingestao_em_andamento = True
            self.ctx.track_task(self._executar_ingestao())
        # Consolidação de átomos (Fase 2): funde os acúmulos quase-idênticos, com
        # originais arquivados. 1x/dia e só em idle total, como a ingestão.
        if self._consolidacao_devida(agora):
            self._ultima_consolidacao = agora
            self._consolidacao_em_andamento = True
            self.ctx.track_task(self._executar_consolidacao())
        # Colheita acadêmica (Fase 4): busca/baixa PDFs sobre os temas do dono. Só
        # em idle e opt-in — é egressão de fundo, sem pergunta em curso.
        if self._academico_devido(agora):
            self._ultima_academico = agora
            self._academico_em_andamento = True
            self.ctx.track_task(self._executar_academico())
        # OCR (Fase 3): só com a GPU LIMPA — o LLM é descarregado antes (exigência
        # do dono). Sem sessão viva, como todo trabalho de fundo.
        if self._ocr_devido(agora):
            self._ocr_em_andamento = True
            self.ctx.track_task(self._executar_ocr())
        # MODO ECONOMIA (2026-08-02): o único item deste tick que NÃO exige sessão
        # vazia — ele existe justamente para o app aberto o dia inteiro. Ver standby.py.
        # Em background como os vizinhos: `liberar_vram` + `enxugar` levam segundos e
        # um alarme não pode ficar esperando o PC dormir.
        if self._standby_devido():
            self._standby_em_andamento = True
            self.ctx.track_task(self._executar_standby())

    # -- backup diário (painel 2026-07-24, ops-backup-01) -----------------------
    def _backup_devido(self, agora: datetime) -> bool:
        if not settings.backup_habilitado or self._backup_em_andamento:
            return False
        return not backup.caminho_do_dia(Path(settings.backup_dir), agora).exists()

    async def _executar_backup(self, agora: datetime) -> None:
        """Zipa vault + SQLite + .env em to_thread. NUNCA propaga (trabalho de fundo
        não pode derrubar o scheduler — mesmo contrato da pesquisa agendada)."""
        try:
            alvo = await asyncio.to_thread(
                backup.executar,
                Path(settings.caminho_obsidian),
                Path(settings.db_telemetria),
                BASE_DIR / ".env",
                Path(settings.backup_dir),
                agora,
                settings.backup_retencao,
            )
            telemetry.track("BACKUP", f"Backup diário salvo: {alvo.name}")
        except Exception as exc:
            telemetry.error("BACKUP", "Falha no backup diário", exc)
        finally:
            self._backup_em_andamento = False

    # -- ingestão de livros — Fase 1 (2026-07-25) -------------------------------
    def _ingestao_devida(self, agora: datetime) -> bool:
        if (not settings.ingestao_habilitada or self._ingestao_em_andamento
                or self._ha_sessoes()):
            return False
        pend = Path(settings.dir_ingestao) / "pendentes"
        if pend.is_dir() and any(pend.glob("*.json")):
            return True
        # Pasta vigiada: PDF novo largado em dados/livros/entrada também dispara a
        # passada (a extração acontece dentro dela, antes da atomização).
        entrada = Path(settings.dir_livros) / "entrada"
        return (settings.livros_entrada_habilitada and entrada.is_dir()
                and any(entrada.glob("*.pdf")))

    def _ocr_devido(self, agora: datetime) -> bool:
        """Devido enquanto houver livro escaneado na fila. Sem intervalo de tempo: o
        trabalho é finito (acaba quando o livro acaba) e cada passada é capada por
        páginas — diferente do crawler, que é infinito e por isso é 1x/dia."""
        if (not settings.ocr_habilitado or self._ocr_em_andamento
                or self._ha_sessoes() or not self.ctx.interactive_idle.is_set()):
            return False
        fila = Path(settings.dir_livros) / "aguardando_ocr"
        return fila.is_dir() and any(fila.glob("*.pdf"))

    async def _executar_ocr(self) -> None:
        """DESCARREGA o LLM e transcreve N páginas em subprocesso.

        A GPU limpa é pré-condição do dono ("só quando nada do projeto estiver na
        VRAM, pra não misturar duas venvs"): `ctx.liberar_vram()` descarrega LLM,
        XTTS, Whisper e embeddings — o OCR sobe ~3 GB próprios e não caberia junto
        na 3080. O `restaurar_vram` no finally é OBRIGATÓRIO: STT/TTS não
        auto-carregam, então sem ele a voz voltaria muda em silêncio. O LLM fica de
        fora da restauração (é lazy: religa no próximo turno). Nunca propaga."""
        try:
            if not self.ctx.interactive_idle.is_set() or self._ha_sessoes():
                return
            await self.ctx.liberar_vram()
            n = await self.ctx.etl.ocr_livros()
            if n:
                telemetry.track("OCR", f"Passada de OCR concluída ({n} página(s)).")
        except Exception as exc:
            telemetry.error("OCR", "Falha na passada de OCR", exc)
        finally:
            try:
                await self.ctx.restaurar_vram()
            except Exception as exc:
                telemetry.error("OCR", "Falha ao restaurar serviços após o OCR", exc)
            self._ocr_em_andamento = False

    # -- modo economia automático (2026-08-02) ----------------------------------
    def _fundo_ocupado(self) -> bool:
        """Há trabalho de fundo em curso que PRECISA dos modelos carregados?

        Todos os jobs abaixo religam o LLM por conta própria e contam com ele até
        terminarem; descarregar no meio não economiza, só desperdiça o que já foi
        feito. O `idle_em_andamento` vem do `EtlProcessor.run_idle` — é o único que
        não é despachado por este serviço (nasce do fim de conversa e da /api/idle)."""
        return any((
            self._pesquisa_em_andamento, self._backup_em_andamento,
            self._ingestao_em_andamento, self._consolidacao_em_andamento,
            self._academico_em_andamento, self._ocr_em_andamento,
            self._standby_em_andamento, self.ctx.idle_em_andamento,
        ))

    def _standby_devido(self) -> bool:
        """Consulta o watcher puro (standby.py) e loga o veredito quando ele MUDA.

        O log condicional não é preciosismo: isto age sozinho na ausência do dono e
        mexe na GPU. Sem rastro, "por que ele não dormiu ontem à noite?" só se
        responde reencenando a noite inteira. Com rastro, é uma linha."""
        situacao = standby.Situacao(
            minutos=settings.idle_standby_minutos,
            segundos_sem_uso=self.ctx.segundos_sem_uso(),
            descansando=self.ctx.descansando,
            interativo_em_voo=not self.ctx.interactive_idle.is_set(),
            fundo_ocupado=self._fundo_ocupado(),
        )
        veredito = standby.avaliar(situacao)
        if veredito.dormir:
            return True
        # Só interessa o veto que segura um standby JÁ vencido no relógio — "faltam
        # 840s" a cada tick seria ruído escondendo a linha que importa.
        vencido = (situacao.minutos > 0
                   and situacao.segundos_sem_uso >= situacao.minutos * 60
                   and not situacao.descansando)
        if vencido and veredito.motivo != self._ultimo_motivo_standby:
            self._ultimo_motivo_standby = veredito.motivo
            telemetry.track("ECONOMIA", f"Standby adiado: {veredito.motivo}.")
        return False

    async def _executar_standby(self) -> None:
        """Solta os modelos e AVISA quem está conectado.

        O aviso é a parte que não pode faltar. A interface guarda o estado de energia
        numa variável lida uma vez na abertura (`lerEnergia`), e é ela que faz o
        "religar ANTES de enviar" funcionar. Se o servidor dormisse calado, a página
        continuaria acreditando estar 'ligado' e a próxima pergunta seria respondida
        com o RAG cego — o embedding não auto-carrega, então viria uma resposta bem
        formada e sem contexto nenhum. Degradação silenciosa é justamente o defeito
        que este projeto mais combate; por isso dormir sozinho obriga a avisar."""
        try:
            # Fecha a corrida entre o `tick` (que checou) e esta task (que age):
            # um turno pode ter começado no meio. Espera curta — se alguém voltou
            # a conversar, o standby não é mais devido e o próximo tick reavalia.
            if not await self.ctx.aguardar_ocio(2.0):
                telemetry.track("ECONOMIA", "Standby abortado — conversa retomada.")
                return
            antes = energia.medir()
            liberados = await self.ctx.liberar_vram()
            await asyncio.to_thread(energia.enxugar)
            depois = energia.medir()
            self.ctx.descansando = True
            self._ultimo_motivo_standby = ""
            minutos = settings.idle_standby_minutos
            telemetry.track(
                "ECONOMIA",
                f"Descansando após {minutos} min sem uso "
                f"({', '.join(sorted(liberados)) or 'nada a soltar'}) — "
                f"liberou {energia.resumo(energia.delta(antes, depois))}.",
            )
            await self.avisar_energia("descansando", depois)
        except Exception as exc:
            telemetry.error("ECONOMIA", "Falha ao entrar em modo economia", exc)
        finally:
            self._standby_em_andamento = False

    async def avisar_energia(self, estado: str, medida: Optional[dict] = None) -> None:
        """Empurra o estado de energia para TODA sessão viva.

        Sem áudio e sem bolha, ao contrário do `_notificar_falado`: isto é um chip de
        status mudando de cor, não um recado. Também serve ao caso de duas cascas
        abertas (janela do PC + celular) — quem não clicou fica sabendo.

        ⚠ Este é o ÚNICO push que continua sendo broadcast depois do multiusuário, e é
        de propósito: o estado da GPU é da MÁQUINA, não de uma pessoa. Quem está
        conectado precisa saber que os modelos foram soltos — é dessa variável que
        depende o religar-antes-de-enviar do front. Filtrar por dono aqui deixaria a
        sessão dos outros três acreditando estar 'ligado' e faria a próxima pergunta
        deles sair com o RAG cego, que é justamente a degradação silenciosa que o aviso
        existe para impedir."""
        payload = {"tipo": "energia", "estado": estado}
        if medida:
            payload["medida"] = medida
        for s in list(self.ctx.sessoes):
            try:
                await s.safe_send(payload)
            except Exception as exc:                # noqa: BLE001 - sessão morrendo
                telemetry.warn("ECONOMIA", f"Não consegui avisar uma sessão: {exc}")

    def _academico_devido(self, agora: datetime) -> bool:
        if (not settings.academico_habilitado or self._academico_em_andamento
                or self._ha_sessoes()):
            return False
        ult = self._ultima_academico
        return ult is None or (agora - ult).total_seconds() >= settings.academico_intervalo_horas * 3600

    async def _executar_academico(self) -> None:
        """Uma passada de colheita acadêmica: rede + extração, SEM LLM (a atomização
        é a passada de ingestão, que roda no idle seguinte). Por isso NÃO religa o
        modelo — se ele está descarregado, continua descarregado. Nunca propaga."""
        try:
            telemetry.track("ACADEMICO", "Passada de colheita acadêmica iniciada (sem sessão).")
            n = await self.ctx.etl.colheita_academica()
            telemetry.track("ACADEMICO", f"Passada concluída ({n} paper(s) enfileirados).")
        except Exception as exc:
            telemetry.error("ACADEMICO", "Falha na colheita acadêmica", exc)
        finally:
            self._academico_em_andamento = False

    async def _executar_ingestao(self) -> None:
        """Atomiza capítulos pendentes SEM sessão aberta. Mesmo contrato da
        pesquisa agendada: religa o modelo, cede a GPU a quem chegar (preemptible
        dentro do ETL) e devolve a VRAM ao fim. NUNCA propaga."""
        try:
            telemetry.track("INGESTAO", "Passada de ingestão de livro iniciada (sem sessão).")
            # Pasta vigiada primeiro: extrai o PDF novo (rápido, sem GPU) para que os
            # jobs dele já entrem na fila que a atomização abaixo consome.
            await self.ctx.etl.ingerir_pasta_livros()
            await self.ctx.llama.ensure_loaded()
            # DRENA a fila numa passada só, em vez de um capítulo por tick. Medido
            # 2026-07-25: o capítulo levava ~2s e o tick seguinte só vinha 18s depois
            # — 90% de GPU ociosa com 82 capítulos enfileirados. O gate de conversa
            # continua a cada capítulo (aqui) e a cada lote (o _esperar_idle do ETL),
            # então a fala do dono ainda passa na frente em segundos.
            n = 0
            while self.ctx.interactive_idle.is_set() and not self._ha_sessoes():
                feitos = await self.ctx.etl.ingestao_livros()
                if not feitos:
                    break
                n += feitos
            if (settings.idle_descarregar_modelo and self.ctx.interactive_idle.is_set()
                    and not self._ha_sessoes()):
                await self.ctx.llama.unload()
            telemetry.track("INGESTAO", f"Passada concluída ({n} capítulo(s)).")
        except Exception as exc:
            telemetry.error("INGESTAO", "Falha na ingestão de livro", exc)
        finally:
            self._ingestao_em_andamento = False

    # -- consolidação de átomos — Fase 2 (2026-07-25) ---------------------------
    def _consolidacao_devida(self, agora: datetime) -> bool:
        if (not settings.consolidacao_habilitada or self._consolidacao_em_andamento
                or self._ha_sessoes()):
            return False
        ult = self._ultima_consolidacao
        return ult is None or (agora - ult).total_seconds() >= settings.consolidacao_intervalo_horas * 3600

    async def _executar_consolidacao(self) -> None:
        """Uma passada de consolidação SEM sessão aberta. Mesmo contrato dos
        vizinhos de idle: religa o modelo, cede a GPU a quem chegar (preemptible
        dentro do ETL), devolve a VRAM ao fim e NUNCA propaga."""
        try:
            telemetry.track("CONSOLIDACAO", "Passada de consolidação iniciada (sem sessão).")
            await self.ctx.llama.ensure_loaded()
            n = await self.ctx.etl.consolidar_atomos()
            if (settings.idle_descarregar_modelo and self.ctx.interactive_idle.is_set()
                    and not self._ha_sessoes()):
                await self.ctx.llama.unload()
            telemetry.track("CONSOLIDACAO", f"Passada concluída ({n} grupo(s)).")
        except Exception as exc:
            telemetry.error("CONSOLIDACAO", "Falha na consolidação", exc)
        finally:
            self._consolidacao_em_andamento = False

    # -- pesquisa proativa AGENDADA (#1): o "cresce a noite toda" ---------------
    def _pesquisa_idle_devida(self, agora: datetime) -> bool:
        """Decide se ESTE tick deve disparar uma passada de pesquisa proativa.

        Gatilho POR TEMPO (não por fim-de-sessão): o run_idle roda 1x quando o chat
        para; sem sessão a base não cresce. Guardas: desligado por default (intervalo
        0); uma passada por vez; respeita o intervalo configurado; e — o veto que
        importa — não disputa a GPU com quem está usando o assistente.

        ⚠ QUAL É ESSE VETO, E POR QUE ELE MUDOU (2026-08-05). Até aqui a régua era
        `_ha_sessoes()`, e esta docstring prometia "nunca concorre com sessão viva". A
        promessa era verdadeira e INÚTIL: a sessão só sai de `ctx.sessoes` quando o
        WebSocket DESCONECTA (ws.py), e o app foi feito para ficar ABERTO o dia inteiro
        — então o gate fechava no primeiro cliente e não abria mais. Medido no log de
        boot de 2026-08-05, com o intervalo de 2 h configurado certo: uma única passada,
        aos 17,4 s, na fresta antes de a webview conectar o socket; depois disso, nunca
        mais. Um "cresce a noite toda" que só cresce nos 17 segundos em que ninguém
        abriu a janela não cresce nada.

        A régua nova é ATIVIDADE — quanto tempo faz desde o último turno interativo
        (`ctx.ultima_interacao`) — mais o veto de decode em voo (`interactive_idle`).
        Não é um terceiro critério inventado aqui: é o MESMO remédio que o `standby.py`
        já tinha adotado para o MESMO defeito (ver o comentário do
        `AppContext.ultima_interacao`), aplicado ao segundo job que sofria dele.

        O que a régua velha queria proteger continua protegido, e melhor: quem disputa a
        GPU serializada é a INFERÊNCIA, não uma aba esquecida aberta — `interactive_idle`
        enxerga a primeira, `ctx.sessoes` só enxergava a segunda."""
        if settings.pesquisa_agendada_intervalo_seconds <= 0:
            return False
        if self._pesquisa_em_andamento:
            return False
        # Pergunta em voo: a GPU é serializada, o crawler entraria na fila do turno.
        if not self.ctx.interactive_idle.is_set():
            return False
        # Conversa recente: quem acabou de falar volta a falar. É este limiar que
        # substitui "não há sessão aberta".
        if self.ctx.segundos_sem_uso() < settings.pesquisa_agendada_ocio_seconds:
            return False
        ult = self._ultima_pesquisa_idle
        if ult is not None and (agora - ult).total_seconds() < settings.pesquisa_agendada_intervalo_seconds:
            return False
        return True

    async def _executar_pesquisa_idle(self) -> None:
        """Uma passada de pesquisa proativa + temas quentes, com a casa em silêncio.

        Religa o modelo (o idle o descarrega e `collect` NÃO auto-carrega — só `stream`)
        e devolve a VRAM ao fim. Os métodos do ETL já cedem a vez à conversa
        (interactive_idle + preemptible) e são auto-capados, então rodar em background é
        seguro. NUNCA propaga: é trabalho de fundo, não pode derrubar o scheduler."""
        try:
            telemetry.track("PESQUISA_IDLE", "Passada agendada iniciada (sem uso recente).")
            await self.ctx.llama.ensure_loaded()
            await self.ctx.etl.pesquisa_proativa()
            await self.ctx.etl.pesquisa_temas_quentes()
            # Devolve a GPU entre as passadas (pilar do idle), mas só se ninguém voltou.
            # A régua é a MESMA da porta de entrada (ver `_pesquisa_idle_devida`): com
            # `_ha_sessoes()` aqui, o app aberto o dia inteiro significava "religa o
            # modelo a cada 2 h e nunca mais o solta" — o oposto do que a linha promete.
            if (settings.idle_descarregar_modelo and self.ctx.interactive_idle.is_set()
                    and self.ctx.segundos_sem_uso() >= settings.pesquisa_agendada_ocio_seconds):
                await self.ctx.llama.unload()
            telemetry.track("PESQUISA_IDLE", "Passada agendada concluída.")
        except Exception as exc:
            telemetry.error("PESQUISA_IDLE", "Falha na pesquisa agendada", exc)
        finally:
            self._pesquisa_em_andamento = False

    async def _amostrar_consumo(self) -> None:
        """WATTÍMETRO: uma amostra de energia por passada, estrangulada por `devido`.

        Fica no tick do scheduler e não num laço próprio pelo mesmo motivo do
        `_probe_vram`: já existe um relógio de fundo rodando, e um segundo laço só
        para isto seria mais uma thread para drenar no shutdown.

        ⚠ Roda MESMO em standby, de propósito. É quando o assistente está
        descansando que o dono mais quer saber quanto a máquina ainda gasta — e
        nenhuma das duas fontes (NVML e o arquivo do ajudante) depende de modelo
        carregado. Parar aqui abriria um buraco justamente na parte da conta que
        ele pediu para enxergar.

        Nunca levanta: uma falha de contabilidade não pode derrubar o loop que
        dispara os alarmes.
        """
        registro = getattr(self.ctx, "consumo", None)
        if registro is None or not registro.devido(settings.consumo_intervalo_seconds):
            return
        try:
            medida = await asyncio.to_thread(energia.medir)
            mj = await asyncio.to_thread(potencia.energia_acumulada_mj)
            parede = tomada.a_partir_de_energia(medida)
            await asyncio.to_thread(
                registro.registrar, mj, medida.get("cpu_watts"),
                parede.total.minimo, parede.total.maximo,
            )
        except Exception as exc:                       # noqa: BLE001 - ver docstring
            telemetry.error("CONSUMO", "Falha ao amostrar o consumo", exc)

    async def _probe_vram(self) -> None:
        """#28: alimenta o detector de vazamento e avisa se disparar. #29: recalibra
        o orçamento de tokens de fundo pela VRAM livre. No-op sem CUDA ou desligado."""
        if not settings.vram_monitor_habilitado:
            return
        uso = await asyncio.to_thread(vram.ler_uso)
        if uso is None:
            return
        # Unload/religar do LLM muda o patamar por causa CONHECIDA — sem reset, a
        # janela compara o vale pós-unload com o pico pós-reload e acusa vazamento
        # falso (medido 2026-07-21: aviso aos 7,33 GB logo após religar o 2507).
        llama = getattr(self.ctx, "llama", None)
        ready = bool(getattr(llama, "ready", False))
        if self._llama_ready_anterior is not None and ready != self._llama_ready_anterior:
            self._monitor_vram.reset()
        self._llama_ready_anterior = ready
        if self._monitor_vram.registrar(uso["usado"]):
            telemetry.warn(
                "VRAM",
                f"Possível vazamento: uso subiu de forma sustentada "
                f"(agora {uso['usado'] / 1e9:.2f} GB de {uso['total'] / 1e9:.2f} GB).",
            )
        self.ctx.orcamento_fundo = vram.orcamento_tokens(
            uso["livre_frac"],
            settings.vram_orcamento_base_tokens,
            settings.vram_orcamento_min_tokens,
            settings.vram_frac_min,
            settings.vram_frac_ok,
        )

    # -- despacho por tipo ------------------------------------------------------
    def _dono_do(self, ag: dict) -> str:
        """De quem é este agendamento — com um piso, e o piso tem motivo.

        A migração carimbou `DONO_PADRAO` em toda linha antiga, então `dono` vazio só
        aparece num INSERT feito à mão. Ainda assim o piso existe em vez de recusar a
        linha: sem ele, com a flag ligada, cada `atualizar_agendamento` dessa linha
        falharia, o status nunca mudaria e o alarme dispararia A CADA TICK, para sempre.
        Trocar um dado faltante por um laço infinito de fala é pior que assumir o dono
        que o backfill já assumiu — mas assumir CALADO seria pior ainda, daí o aviso."""
        dono = ag.get("dono")
        if dono:
            return str(dono)
        telemetry.warn(
            "SCHEDULER",
            f"Agendamento {ag.get('id')} sem dono — assumindo '{identidade.DONO_PADRAO}'.")
        return identidade.DONO_PADRAO

    async def _disparar(self, ag: dict, agora: datetime) -> None:
        """Despacha por tipo, DENTRO da identidade do dono do agendamento.

        Este `usar_dono` é a regra (2) do cabeçalho e não é cosmético: o `to_thread` das
        chamadas de banco copia o contexto, então é ele que faz `atualizar_agendamento`,
        `registrar_auditoria` e `get_lacunas` mirarem a pessoa certa — e é ele que o
        `_notificar_falado` lê para saber em qual celular falar. Um disparo sem ele, com
        a flag ligada, ou não acha a linha (o UPDATE tem `AND dono`) ou fala com todos."""
        with identidade.usar_dono(self._dono_do(ag)):
            tipo = ag["tipo"]
            if tipo == "lembrete":
                await self._disparar_lembrete(ag, agora)
            elif tipo == "watcher":
                await self._checar_watcher(ag, agora)
            elif tipo == "briefing":
                await self._disparar_briefing(ag, agora)
            elif tipo == "pomodoro":
                await self._disparar_pomodoro(ag, agora)
            elif tipo == "seguranca":
                await self._disparar_alerta(ag, agora)
            elif tipo == "mensagem":
                await self._disparar_mensagem(ag, agora)
            else:
                telemetry.warn("SCHEDULER", f"Tipo de agendamento desconhecido: {tipo!r} (id {ag['id']}).")
                await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="cancelado")

    async def _disparar_lembrete(self, ag: dict, agora: datetime) -> None:
        entregue = await self._notificar_falado(ag["mensagem"], ack_id=str(ag["id"]))
        # ACK de aplicação (painel 2026-07): safe_send True NÃO prova entrega — num
        # TCP meio-aberto o send_json bufferiza no SO e "dá certo" (o ws-ping do
        # uvicorn só derruba o zumbi ~20-40s depois; o disparo cairia nessa janela).
        # PESSIMISTA por default: o status durável vira pendente_entrega JÁ, e só o
        # ack do cliente (confirmar_entrega) conclui/reprograma. Sem ack no timeout,
        # a reentrega existente cobre — o lembrete pode duplicar, nunca sumir.
        await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="pendente_entrega")
        if entregue:
            self._aguardando_ack[str(ag["id"])] = (ag, agora)
        else:
            telemetry.track("SCHEDULER", f"Lembrete {ag['id']} sem ouvinte — pendente de entrega.")

    async def _entregar_pendente(self, ag: dict, agora: datetime) -> None:
        """Reentrega UMA linha pendente — sempre sob a identidade do dono dela.

        O `usar_dono` se repete aqui (o `_disparar` já tem o seu) porque este método tem
        DOIS chamadores: o laço do `tick`, que varre todos os donos e chega sem dono no
        contexto, e o `entregar_pendentes` do accept, que já sabe de quem é a conexão.
        Marcar de novo é idempotente; confiar no chamador é que não seria."""
        with identidade.usar_dono(self._dono_do(ag)):
            if self._espera_ack(str(ag["id"]), agora):
                return  # enviado há pouco, aguardando o ack — não duplica a fala por tick
            if ag["tipo"] == "seguranca":
                # Alerta guardado por falta de ouvinte: o card rico vai junto com a
                # bolha falada, para o alerta chegar igual ao que teria chegado ao vivo.
                dados = self._payload(ag)
                await self._empurrar_card_seguranca(
                    dados.get("titulo", "alerta"), dados.get("detalhe", ""), ag["mensagem"])
            elif ag["tipo"] == "mensagem":
                # Mesmo raciocínio do alerta: o card leva os campos separados (id, tipo,
                # remetente) que a tela do mensageiro desenha e que a bolha falada não
                # carrega. Reentregar só a fala deixaria a mensagem sem o botão de
                # responder — ela CHEGOU, mas não dá para agir sobre ela.
                await self._empurrar_card_mensagem(self._payload(ag))
            if not await self._notificar_falado(ag["mensagem"], ack_id=str(ag["id"])):
                return  # ainda sem ouvinte real; tenta no próximo tick
            self._aguardando_ack[str(ag["id"])] = (ag, agora)
            telemetry.track("SCHEDULER", f"Agendamento {ag['id']} reenviado — aguardando ack.")

    def _espera_ack(self, ack_id: str, agora: datetime) -> bool:
        """True se este push foi enviado há pouco e ainda esperamos a confirmação.
        Expirado -> sai da espera (o status pendente_entrega faz a reentrega normal)."""
        item = self._aguardando_ack.get(ack_id)
        if item is None:
            return False
        _ag, enviado = item
        if (agora - enviado).total_seconds() > settings.proativo_ack_timeout_seconds:
            self._aguardando_ack.pop(ack_id, None)
            return False
        return True

    async def confirmar_entrega(self, ack_id: str) -> None:
        """ACK do cliente: AGORA o push foi exibido de verdade — conclui/reprograma.
        Chamado pelo ws ao receber {"tipo":"ack_proativo","id":...}. Ack atrasado ou
        duplicado é no-op (o estado durável já decidiu)."""
        item = self._aguardando_ack.pop(str(ack_id), None)
        if item is None:
            return
        ag, enviado = item
        dono, atual = self._dono_do(ag), identidade.dono_atual()
        if atual is not None and atual != dono:
            # O ack chega pelo WebSocket com um id que o CLIENTE escolhe, e a espera é um
            # dicionário GLOBAL. Sem esta conferência bastava acertar o número para
            # concluir (ou reprogramar) o lembrete de outra pessoa — que então nunca
            # tocaria, sem erro nenhum no log. Devolve o item à espera: o ack legítimo
            # do dono ainda tem de funcionar depois deste.
            self._aguardando_ack[str(ack_id)] = (ag, enviado)
            telemetry.warn(
                "SCHEDULER",
                f"Ack do agendamento {ag['id']} recusado: veio de {atual!r}, não de {dono!r}.")
            return
        with identidade.usar_dono(dono):
            agora = datetime.now()
            if ag["tipo"] == "watcher":
                await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="concluido")
                await asyncio.to_thread(db.registrar_auditoria, "watcher_satisfeito", ag["mensagem"])
            elif ag["tipo"] == "seguranca":
                # Alerta não recorre e não é "lembrete disparado" na trilha: quem audita
                # segurança procura pela ação de segurança, não no meio dos alarmes.
                await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="concluido")
                await asyncio.to_thread(db.registrar_auditoria, "alerta_seguranca_entregue", ag["mensagem"])
            elif ag["tipo"] == "mensagem":
                # Ramo próprio pelo mesmo motivo do alerta, e não por simetria: sem ele a
                # entrega cairia no `else`, que audita "lembrete_disparado" — e a trilha
                # do dono passaria a mostrar alarmes que ele nunca criou. A linha da
                # tabela `mensagens` é a fonte da verdade; este agendamento é só o
                # carteiro, e o carteiro termina aqui (mensagem não recorre).
                await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="concluido")
            else:
                await asyncio.to_thread(db.registrar_auditoria, "lembrete_disparado", ag["mensagem"])
                await self._reprogramar_ou_concluir(ag, agora)
            telemetry.track("SCHEDULER", f"Ack recebido — agendamento {ag['id']} confirmado.")

    async def _reprogramar_ou_concluir(self, ag: dict, agora: datetime) -> None:
        """Recorrente -> agenda a próxima ocorrência (à frente de agora, sem drift).
        Único -> conclui."""
        rec = ag.get("recorrencia")
        if not rec:
            await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="concluido")
            return
        try:
            base = datetime.fromisoformat(ag["proximo_disparo"])
        except (ValueError, TypeError):
            base = agora
        prox = agenda.proximo_disparo(base, rec)
        if prox is None:
            await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="concluido")
            return
        # Se o servidor ficou dias fora, avança até o próximo instante FUTURO.
        guarda = 0
        while prox <= agora and guarda < 1000:
            seg = agenda.proximo_disparo(prox, rec)
            if seg is None:
                break
            prox, guarda = seg, guarda + 1
        await asyncio.to_thread(
            db.atualizar_agendamento, ag["id"], status="ativo", proximo_disparo=prox.isoformat()
        )

    # -- pomodoro (#19): ciclo foco <-> pausa -----------------------------------
    async def _disparar_pomodoro(self, ag: dict, agora: datetime) -> None:
        """Fim de uma fase -> anuncia a transição e REPROGRAMA a próxima fase no MESMO
        agendamento (alterna foco/pausa via payload). Cicla até o usuário cancelar. A
        entrega é IGNORADA (é tempo-real: um aviso perdido enquanto ausente não faz sentido
        segurar), como o briefing reprograma independentemente da entrega."""
        fase = self._payload(ag).get("fase", "foco")
        if fase == "foco":
            msg = f"Fim do foco! Faça uma pausa de {settings.pomodoro_pausa_min} minutos."
            prox_fase, prox_min = "pausa", settings.pomodoro_pausa_min
        else:
            msg = f"Pausa encerrada. De volta ao foco por {settings.pomodoro_foco_min} minutos!"
            prox_fase, prox_min = "foco", settings.pomodoro_foco_min
        await self._notificar_falado(msg)
        await asyncio.to_thread(db.registrar_auditoria, "pomodoro", msg)
        prox = (agora + timedelta(minutes=prox_min)).isoformat()
        await asyncio.to_thread(
            db.atualizar_agendamento, ag["id"], status="ativo",
            proximo_disparo=prox, payload=json.dumps({"fase": prox_fase}),
        )

    # -- watcher ("me avise quando X") ------------------------------------------
    async def _checar_watcher(self, ag: dict, agora: datetime) -> None:
        # Expira sozinho: não fica batendo na web para sempre.
        payload = self._payload(ag)
        termos = payload.get("termos", "")
        condicao = payload.get("condicao", ag["mensagem"])
        nascido = payload.get("nascido_em")
        if nascido:
            try:
                if agora - datetime.fromisoformat(nascido) > timedelta(hours=settings.watcher_expira_horas):
                    await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="concluido")
                    telemetry.track("SCHEDULER", f"Watcher {ag['id']} expirou (sem satisfazer a condição).")
                    return
            except (ValueError, TypeError):
                pass

        if self.ctx.web is None:
            await self._reprogramar_watcher(ag, agora)
            return
        dados = await self.ctx.web.search(termos, consulta=termos)
        if not dados or dados == NENHUM:
            await self._reprogramar_watcher(ag, agora)
            return

        # Avalia a condição com o LLM — cedendo a GPU à conversa (igual ao ETL).
        await self.ctx.interactive_idle.wait()
        try:
            veredito = await self.ctx.llama.collect(
                prompts.prompt_watcher(condicao, dados),
                max_tokens=80,
                system_prompt=prompts.SYS_WATCHER,
                temperature=0.0,
                preemptible=True,
            )
        except Exception as exc:
            # InferenciaPreemptada (usuário voltou) ou falha real: tenta de novo no
            # próximo intervalo, sem derrubar o watcher.
            telemetry.track("SCHEDULER", f"Watcher {ag['id']} adiado ({type(exc).__name__}).")
            await self._reprogramar_watcher(ag, agora)
            return

        if textutils.normaliza(veredito).lstrip().startswith("sim"):
            frase = veredito.strip()
            msg = f"Aviso sobre '{condicao}': {frase}" if frase else f"A condição que você pediu se cumpriu: {condicao}"
            # Mesmo desenho pessimista do lembrete: pendente até o ACK do cliente.
            entregue = await self._notificar_falado(msg, ack_id=str(ag["id"]))
            await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="pendente_entrega")
            if entregue:
                self._aguardando_ack[str(ag["id"])] = (ag, agora)
            telemetry.track("SCHEDULER", f"Watcher {ag['id']} satisfeito -> aguardando ack.")
        else:
            await self._reprogramar_watcher(ag, agora)

    async def _reprogramar_watcher(self, ag: dict, agora: datetime) -> None:
        rec = ag.get("recorrencia") or f"intervalo:{settings.watcher_intervalo_seconds}"
        prox = agenda.proximo_disparo(agora, rec) or (agora + timedelta(seconds=settings.watcher_intervalo_seconds))
        await asyncio.to_thread(
            db.atualizar_agendamento, ag["id"], status="ativo", proximo_disparo=prox.isoformat()
        )

    # -- briefing diário --------------------------------------------------------
    async def _disparar_briefing(self, ag: dict, agora: datetime) -> None:
        texto = await self._montar_briefing(agora)
        entregue = await self._notificar_falado(texto) if texto else True
        if entregue and texto:
            await asyncio.to_thread(db.registrar_auditoria, "briefing_entregue", "Briefing diário")
        # Briefing é sempre recorrente (diário); reprograma independentemente da entrega.
        await self._reprogramar_ou_concluir(ag, agora)

    async def _montar_briefing(self, agora: datetime) -> Optional[str]:
        """Fala curta de bom dia com os temas que o usuário vem explorando (lacunas) e,
        se der, um fato fresco da web sobre o principal. Tudo cede a GPU à conversa."""
        try:
            lacunas = await asyncio.to_thread(db.get_lacunas, 3)
        except Exception:
            lacunas = []
        temas = ", ".join(l["termos"] for l in lacunas)
        contexto = ""
        if lacunas and self.ctx.web is not None:
            try:
                dados = await self.ctx.web.search(lacunas[0]["termos"], consulta=lacunas[0]["termos"])
                if dados and dados != NENHUM:
                    contexto = dados[:1500]
            except Exception as exc:
                telemetry.warn("SCHEDULER", f"Briefing sem web ({exc}).")
        # AGENDA LOCAL (#40): compromissos de HOJE entram no briefing. Computado ANTES do
        # LLM para ser falado mesmo se a inferência for adiada (usuário voltou / preempção).
        try:
            eventos = await asyncio.to_thread(
                calendario.ler_pasta, str(settings.dir_agenda), agora.date()
            )
        except Exception:
            eventos = []
        agenda_txt = ""
        if eventos:
            partes = "; ".join(f"{dt.strftime('%H:%M')} {t}" for dt, t in eventos)
            agenda_txt = f"Na sua agenda hoje: {partes}. "

        data_hoje = agora.strftime("%d/%m/%Y")
        await self.ctx.interactive_idle.wait()
        try:
            fala = await self.ctx.llama.collect(
                prompts.prompt_briefing(data_hoje, temas, contexto),
                max_tokens=settings.max_tokens_resposta,
                system_prompt=prompts.SYS_BRIEFING,
                preemptible=True,
            )
        except Exception as exc:
            telemetry.track("SCHEDULER", f"Briefing adiado ({type(exc).__name__}).")
            return agenda_txt.strip() or None
        return (agenda_txt + fala).strip() or None

    # -- push falado para as sessões vivas --------------------------------------
    def _ha_sessoes(self) -> bool:
        """Tem ALGUÉM usando a máquina? Pergunta de HARDWARE, não de privacidade.

        ⚠ Os chamadores deste método são gates de trabalho de fundo (ingestão, OCR,
        consolidação, colheita): o que eles querem saber é se há uma conversa viva
        disputando a GPU serializada. Trocar isto por "há sessão DESTE dono" seria uma
        regressão de LATÊNCIA — o job voltaria a atropelar a fala de outra pessoa, que é
        exatamente o que o gate existe para impedir.
        Para decidir A QUEM FALAR, o método é o `_sessoes_do_dono` abaixo.

        ⚠ ISTO NÃO É UM DETECTOR DE ÓCIO, e confundir os dois já custou uma função morta:
        a sessão só sai do conjunto quando o WebSocket DESCONECTA, e o app fica aberto o
        dia inteiro — então "há sessão" é verdade quase sempre, inclusive às 4 da manhã.
        Quem precisa saber se a MÁQUINA está parada usa `ctx.segundos_sem_uso()`
        (standby.py, e desde 2026-08-05 a pesquisa agendada)."""
        return bool(self.ctx.sessoes)

    def _sessoes_do_dono(self, dono: Optional[str]) -> list:
        """As sessões vivas que podem RECEBER este push. Pergunta de PRIVACIDADE.

        Com quatro pessoas, o broadcast de antes entregava o lembrete de um no celular
        do outro — vazamento de conteúdo pessoal por uma porta que nem parece porta.

        DESLIGADO o multiusuário devolve TODAS as sessões: é o contrato do interruptor
        (byte a byte o broadcast de sempre), e nesse modo todo mundo é o mesmo dono.

        `getattr(s, "usuario", None)` em vez de `s.usuario` porque o atributo está
        nascendo no `ws.py`/`main.py` em paralelo a este arquivo: enquanto ele não
        aterrissa, uma sessão sem o campo não pode derrubar um alarme com AttributeError.
        Ela também não recebe nada com a flag ligada — não há como adivinhar de quem é —,
        e esse silêncio é DITO no log, porque push que some calado é o defeito que este
        projeto mais combate."""
        sessoes = list(self.ctx.sessoes)
        if not settings.multiusuario_habilitado:
            return sessoes
        minhas, sem_dono = [], 0
        for s in sessoes:
            usuario = getattr(s, "usuario", None)
            if usuario is None:
                sem_dono += 1
            elif usuario == dono:
                minhas.append(s)
        if sem_dono:
            telemetry.warn(
                "SCHEDULER",
                f"{sem_dono} sessão(ões) sem usuário identificado não recebem o push "
                f"(alvo: {dono!r}).")
        return minhas

    async def _notificar_falado(self, texto: str, ack_id: Optional[str] = None) -> bool:
        """Envia texto (bolha 'proativo') + áudio TTS às sessões DO DONO DO CONTEXTO.
        Devolve True se ao menos uma recebeu (= aceitou no socket; a ENTREGA de verdade
        só o ack do cliente prova — ver confirmar_entrega). Sintetiza o áudio uma vez só.

        O dono sai do ContextVar e não de um parâmetro de propósito: quem chama aqui já
        está dentro do `usar_dono` do disparo, e um parâmetro a mais seria mais um lugar
        de onde esquecer — esquecimento que, aqui, fala com a pessoa errada."""
        texto = (texto or "").strip()
        if not texto:
            return False
        sessoes = self._sessoes_do_dono(identidade.dono_atual())
        if not sessoes:
            return False
        audio = None
        if self.ctx.tts is not None and getattr(self.ctx.tts, "ready", False):
            try:
                audio = await self.ctx.tts.synth_base64(texto)
            except Exception as exc:
                telemetry.warn("SCHEDULER", f"TTS do aviso falhou (segue só texto): {exc}")
        payload = {"tipo": "proativo", "texto": texto}
        if ack_id is not None:
            payload["ack_id"] = ack_id   # o front devolve {"tipo":"ack_proativo","id":ack_id}
        entregue = False
        for s in sessoes:
            ok = await s.safe_send(payload)
            if ok and audio:
                await s.safe_send({"tipo": "audio", "base64": audio})
            entregue = entregue or ok
        return entregue

    async def entregar_pendentes(self, dono: Optional[str] = None) -> None:
        """Chamado quando uma conexão nova abre: entrega já o que ficou pendente PARA
        QUEM acabou de conectar, sem esperar o próximo tick.

        Diferente do laço do `tick` (que varre todos os donos e roteia linha a linha),
        aqui já se sabe de quem é a conexão — o ws marca o dono no accept e o
        `create_task` copia o contexto —, então a consulta sai FILTRADA e a linha alheia
        nem é lida. O parâmetro existe para o chamador que tem o dado mas não o
        ContextVar (e para o teste); o default preserva a chamada de hoje, sem argumento.
        """
        alvo = dono or identidade.dono_atual()
        if alvo is None and settings.multiusuario_habilitado:
            # Com a segmentação ligada não há a quem entregar sem saber quem conectou, e
            # cair no dono padrão seria o vazamento. Alto, não calado: é sintoma de uma
            # conexão que subiu sem marcar identidade.
            telemetry.warn("SCHEDULER", "Reentrega pedida sem dono no contexto — nada a entregar.")
            return
        agora = datetime.now()
        # `usar_dono(None)` (multiusuário desligado, sem contexto) é o caminho de sempre:
        # o `_dono_para_consulta` do telemetry cai em DONO_PADRAO, que é o carimbo do
        # backfill — as mesmas linhas de antes da migração.
        with identidade.usar_dono(alvo):
            for ag in await asyncio.to_thread(db.get_agendamentos_pendentes):
                await self._entregar_pendente(ag, agora)

    # -- alerta de segurança para o MESTRE (2026-08-03) -------------------------
    def alertar_seguranca(self, titulo: str, detalhe: str, chave: str) -> None:
        """GANCHO SÍNCRONO para quem detecta abuso — hoje o `RegistroAparelhos`.

        Assinatura combinada com o lado do acesso: `Callable[[str, str, str], None]`.
        `titulo` = o que houve, curto e falável; `detalhe` = os números (ip, quantas
        falhas, em quanto tempo); `chave` = a unidade de estrangulamento, que para o
        rate limit de autenticação é o IP (`f"auth:{ip}"`).

        Nunca levanta e nunca bloqueia: quem chama está no meio de NEGAR um acesso, e uma
        falha de notificação não pode virar uma falha do gate. A janela é conferida AQUI,
        antes de criar a task — senão uma rajada de 500 falhas viraria 500 tasks que
        existiriam só para descobrir que não deviam falar.

        ⚠ O CAMINHO NORMAL É O DE FORA DO LOOP, e isso custou o defeito de 2026-08-04: o
        `main.py` chama `registro.autorizar` por `asyncio.to_thread` (ele abre SQLite e
        travava o event loop a cada requisição), então quem chega aqui vindo do gate está
        numa thread do pool — sem loop corrente. A primeira versão tratava isso como o
        caso raro do vigia e só logava, ou seja: TODO alerta nascido do gate — que é a
        origem de praticamente todos — morria no log, exatamente quando servia. Agora a
        thread agenda no loop do servidor (`track_task_threadsafe`), e o log de erro fica
        para o caso em que realmente não há loop nenhum a que recorrer."""
        if not self._alerta_devido(chave or titulo, datetime.now()):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:
            if self.ctx.track_task_threadsafe(self._alertar_mestre_agora(titulo, detalhe)):
                return
            # Sem loop capturado: processo do vigia (que nem tem scheduler), ctx montado
            # fora do lifespan, ou shutdown em curso. Não dá para agendar a fala — mas o
            # alerta não pode sumir só porque veio da thread errada, então fica no log.
            telemetry.error("SEGURANCA", f"Alerta sem loop para entregar: {titulo} — {detalhe}", exc)
            return
        self.ctx.track_task(self._alertar_mestre_agora(titulo, detalhe))

    async def alertar_mestre(self, titulo: str, detalhe: str,
                             chave: Optional[str] = None) -> bool:
        """Empurra um alerta de segurança ao MESTRE (identidade.MESTRE). True se entregue.

        Versão assíncrona do gancho acima, para quem já está no loop. `chave` é a unidade
        de estrangulamento; sem ela, o próprio título serve — dois alertas idênticos em
        seguida são o caso que a janela existe para conter."""
        if not self._alerta_devido(chave or titulo, datetime.now()):
            return False
        return await self._alertar_mestre_agora(titulo, detalhe)

    def _alerta_devido(self, chave: str, agora: datetime) -> bool:
        """Um alerta por chave por janela — ver JANELA_ALERTA_SEGURANCA_SEGUNDOS.
        Consome a janela ao devolver True (quem chama já pode falar).

        Sob a trava porque este é o único ponto do scheduler que roda em VÁRIAS threads
        (ver `_trava_alerta`). A trava não segura nada além de operações de dicionário —
        a fala em si acontece depois, no loop."""
        with self._trava_alerta:
            ultimo = self._alertado_em.get(chave)
            if ultimo is not None and (agora - ultimo).total_seconds() < JANELA_ALERTA_SEGURANCA_SEGUNDOS:
                return False
            if len(self._alertado_em) >= MAX_CHAVES_ALERTA:
                vencidas = [k for k, t in self._alertado_em.items()
                            if (agora - t).total_seconds() >= JANELA_ALERTA_SEGURANCA_SEGUNDOS]
                for k in vencidas:
                    self._alertado_em.pop(k, None)
            self._alertado_em[chave] = agora
            return True

    async def _alertar_mestre_agora(self, titulo: str, detalhe: str) -> bool:
        """A entrega em si, com a janela já decidida. Nunca propaga: isto roda como task
        de fundo disparada de dentro de um gate de acesso."""
        texto = f"Alerta de segurança: {titulo}. {detalhe}".strip()
        # O log vem ANTES da entrega: se o push falhar, o rastro do que aconteceu tem de
        # existir de qualquer jeito — o celular é o canal, não a fonte da verdade.
        telemetry.warn("SEGURANCA", f"{titulo} — {detalhe}")
        try:
            # O MESTRE é quem ADMINISTRA o acesso (ver identidade.py): é dele o celular
            # que recebe, e é como ele que este bloco fala — inclusive para gravar o
            # pendente na fila dele, e não na de quem estava conectado.
            with identidade.usar_dono(identidade.MESTRE):
                await self._empurrar_card_seguranca(titulo, detalhe, texto)
                # A ENTREGA é medida pela bolha falada, não pelo card: é ela que o front
                # de hoje exibe e acka. Card entregue sem bolha seria "chegou" para um
                # cliente que ainda não sabe desenhá-lo — e o alerta se perderia.
                entregue = await self._notificar_falado(texto)
                if not entregue:
                    await self._guardar_alerta_pendente(titulo, detalhe, texto)
                return entregue
        except Exception as exc:
            telemetry.error("SEGURANCA", f"Falha ao alertar o mestre sobre '{titulo}'", exc)
            return False

    async def _empurrar_card_seguranca(self, titulo: str, detalhe: str, texto: str) -> bool:
        """O payload {"tipo":"seguranca"} para as sessões do dono do contexto.

        Dois payloads (card + bolha falada) e não um, porque são dois públicos: o card
        carrega os campos separados, que é o que uma tela de segurança desenha; a bolha
        é o que o front de HOJE já sabe exibir e ACKAR — sem ela, um alerta reentregue
        nunca receberia ack e voltaria a cada tick para sempre.

        ⚑ PONTO DE EXTENSÃO — o push FCM entra AQUI, não no chamador: é a única função
        que sabe "este alerta precisa sair da máquina", e ela já roda sob a identidade
        do destinatário. Hoje, sem app aberto, o alerta cai na fila durável e espera."""
        rico = {"tipo": "seguranca", "titulo": titulo, "detalhe": detalhe, "texto": texto}
        entregue = False
        for s in self._sessoes_do_dono(identidade.dono_atual()):
            try:
                entregue = await s.safe_send(rico) or entregue
            except Exception as exc:            # noqa: BLE001 - sessão morrendo
                telemetry.warn("SEGURANCA", f"Não consegui enviar o card a uma sessão: {exc}")
        return entregue

    async def _guardar_alerta_pendente(self, titulo: str, detalhe: str, texto: str) -> None:
        """Sem ouvinte, o alerta entra na MESMA fila durável dos lembretes em vez de uma
        máquina nova: é ela que já sabe reentregar na próxima conexão, esperar o ack e
        não duplicar. O `payload` guarda os campos separados para o card ser remontado
        igual na reentrega."""
        agora = datetime.now().isoformat()
        ag_id = await asyncio.to_thread(
            db.criar_agendamento, "seguranca", texto, agora,
            payload=json.dumps({"titulo": titulo, "detalhe": detalhe}))
        if ag_id is None:
            telemetry.error("SEGURANCA", f"Alerta sem ouvinte NÃO pôde ser guardado: {texto}")
            return
        # Nasce 'ativo' (é o único status que o `criar_agendamento` escreve) e vira
        # pendente logo em seguida. Se um tick correr nessa fresta, o `_disparar_alerta`
        # cobre — por isso 'seguranca' também é um tipo conhecido do despacho.
        await asyncio.to_thread(db.atualizar_agendamento, ag_id, status="pendente_entrega")
        telemetry.track("SEGURANCA", "Alerta guardado para entrega na próxima conexão do mestre.")

    async def _disparar_alerta(self, ag: dict, agora: datetime) -> None:
        """Alerta que ficou 'ativo' (o update que o marcaria como pendente falhou, ou um
        tick correu no meio da gravação). Mesmo desenho pessimista do lembrete: marca
        pendente JÁ e só o ack conclui."""
        await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="pendente_entrega")
        await self._entregar_pendente(ag, agora)

    # -- mensageiro usuário <-> MESTRE (2026-08-05) -----------------------------
    async def entregar_mensagem(self, msg: mensageiro.Mensagem) -> bool:
        """Empurra uma mensagem ao DESTINATÁRIO dela. True se alguma sessão dele aceitou.

        Por que aqui e não num serviço novo: o problema "avisar alguém que pode não
        estar conectado" já foi resolvido uma vez neste arquivo, com fila durável,
        reentrega no accept do WebSocket e ack de aplicação. Um segundo mecanismo teria
        de reaprender as três coisas — e a que se esquece é sempre a mesma (o ack), o
        que devolve o push que "some" sem erro.

        A linha da tabela `mensagens` já foi gravada pelo chamador ANTES daqui: falhar a
        entrega não pode perder a mensagem, só adiar o aviso. Quem não recebeu push
        nenhum ainda vê tudo em `GET /api/mensagens`."""
        texto = mensageiro.falar(msg)
        try:
            # O `usar_dono` do DESTINATÁRIO é o que faz o `_sessoes_do_dono` escolher o
            # celular certo E o `criar_agendamento` do pendente cair na fila dele. Sem
            # ele, a mensagem para a Ana ficaria pendente na caixa de quem a enviou.
            with identidade.usar_dono(msg.destinatario):
                await self._empurrar_card_mensagem(self._card_mensagem(msg))
                # A ENTREGA é medida pela bolha falada, e não pelo card, pelo mesmo
                # motivo do alerta: é ela que o front de hoje exibe e ACKA. Card sem
                # bolha seria "chegou" para um cliente que ainda não sabe desenhá-lo.
                entregue = await self._notificar_falado(texto)
                if not entregue:
                    await self._guardar_mensagem_pendente(msg, texto)
                return entregue
        except Exception as exc:
            telemetry.error("MENSAGEIRO",
                            f"Falha ao entregar a mensagem {msg.id} a {msg.destinatario}", exc)
            return False

    @staticmethod
    def _card_mensagem(msg: mensageiro.Mensagem) -> dict:
        """O payload rico. Guardado no `payload` do pendente EXATAMENTE como vai para o
        socket, para a reentrega desenhar o mesmo card do ao vivo."""
        return {"tipo": "mensagem", **msg.para_json()}

    async def _empurrar_card_mensagem(self, card: dict) -> bool:
        """`{"tipo":"mensagem", ...}` para as sessões do dono do contexto.

        ⚑ PONTO DE EXTENSÃO — o push FCM entra AQUI, como no card de segurança: é a
        única função que sabe "esta mensagem precisa sair da máquina", e ela já roda sob
        a identidade do destinatário."""
        if not card:
            return False
        card = {**card, "tipo": "mensagem"}   # a reentrega remonta do payload gravado
        entregue = False
        for s in self._sessoes_do_dono(identidade.dono_atual()):
            try:
                entregue = await s.safe_send(card) or entregue
            except Exception as exc:            # noqa: BLE001 - sessão morrendo
                telemetry.warn("MENSAGEIRO", f"Não consegui enviar o card a uma sessão: {exc}")
        return entregue

    async def _guardar_mensagem_pendente(self, msg: mensageiro.Mensagem, texto: str) -> None:
        """Sem ouvinte, o aviso entra na MESMA fila durável dos lembretes e dos alertas.

        O que fica pendente é o AVISO, não a mensagem: ela já está gravada e visível na
        caixa. Por isso um pendente perdido é um incômodo (a pessoa descobre ao abrir o
        app), e não uma perda — o oposto do lembrete, onde a fila É o dado."""
        agora = datetime.now().isoformat()
        ag_id = await asyncio.to_thread(
            db.criar_agendamento, "mensagem", texto, agora,
            payload=json.dumps(self._card_mensagem(msg)))
        if ag_id is None:
            telemetry.error("MENSAGEIRO",
                            f"Mensagem {msg.id} sem ouvinte NÃO pôde ser enfileirada.")
            return
        # Nasce 'ativo' (único status que o `criar_agendamento` escreve) e vira pendente
        # em seguida. Um tick nessa fresta cai no `_disparar_mensagem`, que cobre.
        await asyncio.to_thread(db.atualizar_agendamento, ag_id, status="pendente_entrega")
        telemetry.track("MENSAGEIRO",
                        f"Mensagem {msg.id} guardada para a próxima conexão de {msg.destinatario!r}.")

    async def _disparar_mensagem(self, ag: dict, agora: datetime) -> None:
        """Aviso que ficou 'ativo' (o update que o marcaria pendente falhou, ou um tick
        correu no meio da gravação). Mesmo desenho pessimista do lembrete e do alerta."""
        await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="pendente_entrega")
        await self._entregar_pendente(ag, agora)

    @staticmethod
    def _payload(ag: dict) -> dict:
        try:
            return json.loads(ag.get("payload") or "{}")
        except (ValueError, TypeError):
            return {}
