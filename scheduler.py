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

import agenda
import prompts
import textutils
from config import settings
from rag import NENHUM
from telemetry import db, telemetry

if TYPE_CHECKING:
    from state import AppContext


class SchedulerService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._parado = asyncio.Event()

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
        vencidos = await asyncio.to_thread(db.get_agendamentos_vencidos, agora.isoformat())
        for ag in vencidos:
            await self._disparar(ag, agora)
        # Reentrega o que ficou pendente por falta de ouvinte — mas só se agora há alguém.
        if self._ha_sessoes():
            for ag in await asyncio.to_thread(db.get_agendamentos_pendentes):
                await self._entregar_pendente(ag, agora)

    # -- despacho por tipo ------------------------------------------------------
    async def _disparar(self, ag: dict, agora: datetime) -> None:
        tipo = ag["tipo"]
        if tipo == "lembrete":
            await self._disparar_lembrete(ag, agora)
        elif tipo == "watcher":
            await self._checar_watcher(ag, agora)
        elif tipo == "briefing":
            await self._disparar_briefing(ag, agora)
        else:
            telemetry.warn("SCHEDULER", f"Tipo de agendamento desconhecido: {tipo!r} (id {ag['id']}).")
            await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="cancelado")

    async def _disparar_lembrete(self, ag: dict, agora: datetime) -> None:
        entregue = await self._notificar_falado(ag["mensagem"])
        if entregue:
            await self._reprogramar_ou_concluir(ag, agora)
        else:
            # Ninguém ouvindo: segura para a próxima conexão (não perde o lembrete).
            await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status="pendente_entrega")
            telemetry.track("SCHEDULER", f"Lembrete {ag['id']} sem ouvinte — pendente de entrega.")

    async def _entregar_pendente(self, ag: dict, agora: datetime) -> None:
        if not await self._notificar_falado(ag["mensagem"]):
            return  # ainda sem ouvinte real; tenta no próximo tick
        await self._reprogramar_ou_concluir(ag, agora)
        telemetry.track("SCHEDULER", f"Agendamento {ag['id']} entregue (estava pendente).")

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

    # -- watcher ("me avise quando X") ------------------------------------------
    async def _checar_watcher(self, ag: dict, agora: datetime) -> None:
        # Expira sozinho: não fica batendo na web para sempre.
        try:
            criado = datetime.fromisoformat(ag.get("proximo_disparo"))
        except (ValueError, TypeError):
            criado = agora
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
            entregue = await self._notificar_falado(msg)
            novo_status = "concluido" if entregue else "pendente_entrega"
            await asyncio.to_thread(db.atualizar_agendamento, ag["id"], status=novo_status)
            telemetry.track("SCHEDULER", f"Watcher {ag['id']} satisfeito -> {novo_status}.")
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
            return None
        return fala.strip() or None

    # -- push falado para as sessões vivas --------------------------------------
    def _ha_sessoes(self) -> bool:
        return bool(self.ctx.sessoes)

    async def _notificar_falado(self, texto: str) -> bool:
        """Envia texto (bolha 'proativo') + áudio TTS a TODA sessão viva. Devolve True
        se ao menos uma recebeu. Sintetiza o áudio uma vez só e reusa em todas."""
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
        entregue = False
        for s in sessoes:
            ok = await s.safe_send({"tipo": "proativo", "texto": texto})
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
