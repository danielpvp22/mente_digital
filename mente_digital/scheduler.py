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

Pilares respeitados:
- GPU serializada: watcher e briefing usam o LLM só depois de `interactive_idle.wait()`
  e com `preemptible=True` — a conversa ao vivo sempre passa na frente (igual ao ETL).
- Nada de push sem ouvinte: se ninguém está conectado, o disparo vira 'pendente_entrega'
  e é entregue na próxima conexão (o próprio loop reentrega, e o WS chama no accept).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from pathlib import Path

from mente_digital import agenda
from mente_digital import backup
from mente_digital import calendario
from mente_digital import prompts
from mente_digital import textutils
from mente_digital import vram
from mente_digital.config import BASE_DIR, settings
from mente_digital.rag import NENHUM
from mente_digital.telemetry import db, telemetry

if TYPE_CHECKING:
    from mente_digital.state import AppContext


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
        vencidos = await asyncio.to_thread(db.get_agendamentos_vencidos, agora.isoformat())
        for ag in vencidos:
            await self._disparar(ag, agora)
        # Reentrega o que ficou pendente por falta de ouvinte — mas só se agora há alguém.
        if self._ha_sessoes():
            for ag in await asyncio.to_thread(db.get_agendamentos_pendentes):
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
        return pend.is_dir() and any(pend.glob("*.json"))

    async def _executar_ingestao(self) -> None:
        """Atomiza capítulos pendentes SEM sessão aberta. Mesmo contrato da
        pesquisa agendada: religa o modelo, cede a GPU a quem chegar (preemptible
        dentro do ETL) e devolve a VRAM ao fim. NUNCA propaga."""
        try:
            telemetry.track("INGESTAO", "Passada de ingestão de livro iniciada (sem sessão).")
            await self.ctx.llama.ensure_loaded()
            n = await self.ctx.etl.ingestao_livros()
            if (settings.idle_descarregar_modelo and self.ctx.interactive_idle.is_set()
                    and not self._ha_sessoes()):
                await self.ctx.llama.unload()
            telemetry.track("INGESTAO", f"Passada concluída ({n} capítulo(s)).")
        except Exception as exc:
            telemetry.error("INGESTAO", "Falha na ingestão de livro", exc)
        finally:
            self._ingestao_em_andamento = False

    # -- pesquisa proativa AGENDADA (#1): o "cresce a noite toda" ---------------
    def _pesquisa_idle_devida(self, agora: datetime) -> bool:
        """Decide se ESTE tick deve disparar uma passada de pesquisa proativa.

        Gatilho POR TEMPO (não por fim-de-sessão): o run_idle roda 1x quando o chat
        para; sem sessão a base não cresce. Guardas: desligado por default (intervalo
        0); nunca concorre com sessão viva (o run_idle dela já cobre e disputaria a GPU
        serializada); uma passada por vez; respeita o intervalo configurado."""
        if settings.pesquisa_agendada_intervalo_seconds <= 0:
            return False
        if self._pesquisa_em_andamento or self._ha_sessoes():
            return False
        ult = self._ultima_pesquisa_idle
        if ult is not None and (agora - ult).total_seconds() < settings.pesquisa_agendada_intervalo_seconds:
            return False
        return True

    async def _executar_pesquisa_idle(self) -> None:
        """Uma passada de pesquisa proativa + temas quentes, SEM sessão aberta.

        Religa o modelo (o idle o descarrega e `collect` NÃO auto-carrega — só `stream`)
        e devolve a VRAM ao fim. Os métodos do ETL já cedem a vez à conversa
        (interactive_idle + preemptible) e são auto-capados, então rodar em background é
        seguro. NUNCA propaga: é trabalho de fundo, não pode derrubar o scheduler."""
        try:
            telemetry.track("PESQUISA_IDLE", "Passada agendada iniciada (sem sessão).")
            await self.ctx.llama.ensure_loaded()
            await self.ctx.etl.pesquisa_proativa()
            await self.ctx.etl.pesquisa_temas_quentes()
            # Devolve a GPU entre as passadas (pilar do idle), mas só se ninguém voltou.
            if (settings.idle_descarregar_modelo and self.ctx.interactive_idle.is_set()
                    and not self._ha_sessoes()):
                await self.ctx.llama.unload()
            telemetry.track("PESQUISA_IDLE", "Passada agendada concluída.")
        except Exception as exc:
            telemetry.error("PESQUISA_IDLE", "Falha na pesquisa agendada", exc)
        finally:
            self._pesquisa_em_andamento = False

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
    async def _disparar(self, ag: dict, agora: datetime) -> None:
        tipo = ag["tipo"]
        if tipo == "lembrete":
            await self._disparar_lembrete(ag, agora)
        elif tipo == "watcher":
            await self._checar_watcher(ag, agora)
        elif tipo == "briefing":
            await self._disparar_briefing(ag, agora)
        elif tipo == "pomodoro":
            await self._disparar_pomodoro(ag, agora)
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
        if self._espera_ack(str(ag["id"]), agora):
            return  # enviado há pouco, aguardando o ack — não duplica a fala por tick
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
        ag, _enviado = item
        agora = datetime.now()
        if ag["tipo"] == "watcher":
            await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="concluido")
            await asyncio.to_thread(db.registrar_auditoria, "watcher_satisfeito", ag["mensagem"])
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
        return bool(self.ctx.sessoes)

    async def _notificar_falado(self, texto: str, ack_id: Optional[str] = None) -> bool:
        """Envia texto (bolha 'proativo') + áudio TTS a TODA sessão viva. Devolve True
        se ao menos uma recebeu (= aceitou no socket; a ENTREGA de verdade só o ack
        do cliente prova — ver confirmar_entrega). Sintetiza o áudio uma vez só."""
        texto = (texto or "").strip()
        if not texto:
            return False
        sessoes = list(self.ctx.sessoes)
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

    async def entregar_pendentes(self) -> None:
        """Chamado quando uma conexão nova abre: entrega já o que ficou pendente,
        sem esperar o próximo tick."""
        agora = datetime.now()
        for ag in await asyncio.to_thread(db.get_agendamentos_pendentes):
            await self._entregar_pendente(ag, agora)

    @staticmethod
    def _payload(ag: dict) -> dict:
        try:
            return json.loads(ag.get("payload") or "{}")
        except (ValueError, TypeError):
            return {}
