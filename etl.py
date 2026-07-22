"""
ETL Post-Chat / Idle — o trabalho que acontece quando ninguém está olhando.

EtlProcessor destila conhecimento novo em ÁTOMOS Zettelkasten no idle: as pesquisas
web da fila, o histórico da conversa (summarize_dump) e as lacunas que nem a RAM nem
o banco responderam (pesquisa_proativa) — sempre cedendo a GPU para a inferência
interativa (interactive_idle + preemptible; a conversa ao vivo passa na frente).
append_chat_dump vive aqui porque o dump é a FILA que o summarize_dump consome.

Extraído do agent.py na modularização. A estrutura do átomo em si (dividir/
normalizar) mora em atomos.py; este módulo decide QUANDO e O QUE atomizar.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import List, Tuple

import contradicao
import diapasao
import prompts
import textutils
from atomos import _slug_titulo, dividir_atomos, normalizar_atomo
from config import settings
from llm import InferenciaPreemptada
from otimizador import lacuna_pesquisavel
from rag import NENHUM, strip_frontmatter
from state import AppContext
from telemetry import db, telemetry


async def append_chat_dump(ator: str, texto: str) -> None:
    """Grava o dump bruto da conversa (Obsidian). IO em thread."""
    def _write() -> None:
        with open(settings.arquivo_chat_dump, "a", encoding="utf-8") as f:
            if ator == "User":
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n## [{ts}]\n**Usuário:** {texto}\n")
            else:
                f.write(f"**Mente Digital:** {texto}\n")

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        telemetry.error("DUMP", "Erro ao gravar chat dump", exc)


class EtlProcessor:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def _esperar_idle(self) -> None:
        """Cede a vez para a inferência interativa antes de cada tarefa pesada."""
        await self.ctx.interactive_idle.wait()

    def _max_fundo(self, base: int) -> int:
        """#29: aplica o orçamento de tokens de fundo (calibrado pela VRAM livre pelo
        scheduler). Sem leitura de VRAM (orcamento_fundo None), usa o `base` de sempre."""
        cap = getattr(self.ctx, "orcamento_fundo", None)
        return min(base, cap) if cap else base

    async def _salvar_atomos(self, texto: str, prefixo: str, tipo_log: str) -> int:
        """Salva UM ARQUIVO POR ÁTOMO (Zettelkasten puro). Assim a promoção fica
        precisa por ideia: só o átomo realmente reusado perde o #conhecimento_novo,
        não os vizinhos que calharam de estar no mesmo documento. Devolve quantos salvou.

        Todo bloco passa por `normalizar_atomo` ANTES de virar arquivo: o formato do
        átomo é imposto no código, não confiado ao LLM (ver a docstring de lá — o A/B
        mostrou que nenhum modelo o entrega de forma confiável, e sem as tags a
        promoção nunca acontece).

        Fallback: se o LLM não usou nenhum '##' (formato quebrado), salva o texto inteiro
        como 1 átomo em vez de descartar o conhecimento em silêncio."""
        blocos = dividir_atomos(texto)
        if not blocos and texto.strip():
            blocos = [texto.strip()]
        agora = datetime.now()
        salvos = 0
        duplicados = 0
        salvos_info: List[Tuple[str, str]] = []  # (caminho, corpo sem frontmatter) p/ #24
        for i, bloco in enumerate(blocos):
            bloco = normalizar_atomo(bloco, prefixo, agora)
            if not bloco.strip():
                continue
            # DEDUP contra o banco (pedido: "impeça a duplicação"). Um átomo quase
            # idêntico a um já indexado não vira arquivo novo — senão a base incha com
            # a mesma ideia e o rag_top_k recupera clones. Fail-open sem embeddings.
            if await self._ja_no_banco(strip_frontmatter(bloco)):
                duplicados += 1
                # Métricas do ciclo (painel): o descarte vira EVENTO persistido —
                # "quanto o dedup segura por dia" deixa de ser log transitório.
                await asyncio.to_thread(db.log_etl, "DEDUP", _slug_titulo(bloco), "descartado")
                continue
            nome = f"{prefixo}_{_slug_titulo(bloco)}_{int(time.time())}_{i}.md"
            caminho = os.path.join(str(self.ctx.settings.dir_conhecimento_novo), nome)

            def _save(c=caminho, body=bloco) -> None:
                with open(c, "w", encoding="utf-8") as f:
                    f.write(body + "\n")

            try:
                await asyncio.to_thread(_save)
                await asyncio.to_thread(db.log_etl, tipo_log, nome, "CONCLUIDO")
                salvos += 1
                salvos_info.append((caminho, strip_frontmatter(bloco)))
            except OSError as exc:
                telemetry.error(tipo_log, f"Falha ao salvar átomo {nome}", exc)
        if duplicados:
            telemetry.track(tipo_log, f"Dedup: {duplicados} átomo(s) já no banco, ignorados.")
        # #24: no idle, varre os átomos novos por CONTRADIÇÃO com a base existente.
        await self._varredura_contradicoes(salvos_info)
        return salvos

    async def _varredura_contradicoes(self, salvos_info: List[Tuple[str, str]]) -> None:
        """Para cada átomo recém-salvo, acha o vizinho semântico "relacionado mas
        distinto" (a banda onde mora a contradição) e pergunta ao LLM se se
        contradizem. Capado por ciclo, preemptível (a conversa passa na frente) e
        fail-open (sem loja/embeddings, não faz nada). Os pares achados vão para a
        tabela `contradicoes` e são reportados sob demanda ('mestre, alguma
        contradição?'). Detecção, não ação: nunca apaga nem edita nota."""
        if not settings.contradicao_detectar or not salvos_info:
            return
        store = self.ctx.vectorstore
        if store is None or getattr(store, "_store", None) is None:
            return
        checados = 0
        for caminho, corpo in salvos_info:
            if checados >= settings.contradicao_max_por_ciclo:
                break
            if not corpo.strip():
                continue
            viz = await self._vizinho_relacionado(corpo)
            if viz is None:
                continue
            doc, _dist = viz
            await self._esperar_idle()
            try:
                veredito = await self.ctx.llama.collect(
                    prompts.prompt_contradicao(corpo, doc.page_content),
                    max_tokens=settings.max_tokens_contradicao,
                    system_prompt=prompts.SYS_CONTRADICAO,
                    preemptible=True,   # background: a pergunta do usuário passa na frente
                )
            except InferenciaPreemptada:
                telemetry.track("IDLE", "Varredura de contradição cedeu a GPU.")
                return
            checados += 1
            motivo = contradicao.parse_veredito(veredito)
            if motivo:
                fonte_b = str(doc.metadata.get("source", "")) if doc.metadata else ""
                gravou = await asyncio.to_thread(
                    db.registrar_contradicao, caminho, fonte_b, motivo
                )
                if gravou:
                    telemetry.warn("CONTRADICAO", f"Possível contradição: {motivo[:80]}")

    async def _vizinho_relacionado(self, corpo: str):
        """Vizinho na banda [dedup_dist_max, contradicao_dist_max): próximo o bastante
        para ser o MESMO tema, longe o bastante para não ser duplicata. Devolve
        (doc, dist) ou None. Barato: 1 embedding + 1 vizinho. Fail-open."""
        store = self.ctx.vectorstore
        try:
            res = await asyncio.to_thread(store._store.similarity_search_with_score, corpo, 1)
        except Exception as exc:
            telemetry.warn("CONTRADICAO", f"Falha ao buscar vizinho (ignorando): {exc}")
            return None
        if not res:
            return None
        doc, dist = res[0]
        if settings.dedup_dist_max <= dist < settings.contradicao_dist_max:
            return doc, dist
        return None

    async def _ja_no_banco(self, corpo: str) -> bool:
        """True se um átomo quase idêntico já está indexado (distância < dedup_dist_max).

        Fail-open: sem embeddings/loja (testes) devolve False — dedup é uma trava de
        qualidade, não pode virar bloqueio de escrita. Barato: 1 embedding + 1 vizinho."""
        store = self.ctx.vectorstore
        if store is None or getattr(store, "_store", None) is None or not corpo.strip():
            return False
        try:
            res = await asyncio.to_thread(store._store.similarity_search_with_score, corpo, 1)
        except Exception as exc:
            telemetry.warn("DEDUP", f"Falha ao checar duplicata (seguindo com o save): {exc}")
            return False
        if not res:
            return False
        _doc, dist = res[0]
        return dist < settings.dedup_dist_max

    async def process_queue(self, itens: List[Tuple[str, str]]) -> None:
        if not itens:
            return
        telemetry.track("ETL_POST_CHAT", f"Sintetizando {len(itens)} pesquisas da sessão.")
        total = 0
        # Fila, não `for`: um item preemptado volta pro topo e é RETENTADO — a síntese
        # dele foi jogada fora no meio, e descartar o item silenciosamente seria perder
        # exatamente o conhecimento que este processo existe para reter.
        pendentes = list(itens)
        while pendentes:
            # Cede a vez ANTES de cada tentativa: com o usuário falando, isto bloqueia
            # aqui (barato) em vez de começar uma síntese que será cortada. É também o
            # que impede o retry de virar spin — só reacorda quando a GPU está livre.
            await self._esperar_idle()
            tema, dados = pendentes[0]
            try:
                conteudo = await self.ctx.llama.collect(
                    prompts.prompt_sintese(tema, dados),
                    max_tokens=self._max_fundo(settings.max_tokens_sintese),  # #29
                    system_prompt=prompts.SYS_SINTESE,
                    preemptible=True,   # background: a pergunta do usuário passa na frente
                )
            except InferenciaPreemptada:
                telemetry.track("ETL_POST_CHAT", f"'{tema}' cedeu a GPU — será retomado no idle.")
                continue                      # item continua em pendentes[0]
            except Exception as exc:
                telemetry.error("ETL_POST_CHAT", f"Falha ao sintetizar '{tema}'", exc)
                pendentes.pop(0)              # falha real: não retenta em loop
                continue
            pendentes.pop(0)
            try:
                total += await self._salvar_atomos(conteudo, "Sintese", "ETL_POST_CHAT")
            except Exception as exc:
                telemetry.error("ETL_POST_CHAT", f"Falha ao salvar átomos de '{tema}'", exc)

        await self.ctx.vectorstore.sync()
        telemetry.track("ETL_POST_CHAT", f"Banco Vetorial atualizado ({total} átomos).")

    async def summarize_dump(self) -> None:
        """Destila o histórico BRUTO da conversa (texto+voz) em NOTAS ATÔMICAS
        Zettelkasten — mesma regra da base — em vez do antigo 'Resumo_Sessao'
        estruturado. Cada ideia trocada vira um átomo recuperável, nascendo como
        #conhecimento_novo (consolida quando usado). O dump só é limpo se a síntese
        for salva com sucesso — senão a conversa fica pra próxima passada (nada se perde)."""
        path = settings.arquivo_chat_dump
        if not os.path.exists(path):
            return
        try:
            conteudo = await asyncio.to_thread(
                lambda: open(path, "r", encoding="utf-8").read().strip()
            )
        except OSError as exc:
            telemetry.error("IDLE", "Erro ao ler dump", exc)
            return
        if len(conteudo) < 50:
            return

        await self._esperar_idle()
        telemetry.track("IDLE", "Atomizando histórico da conversa (Zettelkasten)...")
        try:
            atomos = await self.ctx.llama.collect(
                prompts.prompt_sintese_conversa(conteudo),
                max_tokens=self._max_fundo(settings.max_tokens_resumo),  # #29
                system_prompt=prompts.SYS_SINTESE_CONVERSA,
                preemptible=True,   # background: a pergunta do usuário passa na frente
            )
        except InferenciaPreemptada:
            # Sair aqui é seguro E é o ponto: o dump só é limpo mais abaixo, depois de
            # salvar. Cedemos a GPU sem tocar em nada, e a conversa inteira continua no
            # arquivo para a próxima passada de idle.
            telemetry.track("IDLE", "Atomização cedeu a GPU — dump preservado p/ a próxima.")
            return
        atomos = atomos.strip()

        async def _limpar_dump() -> None:
            try:
                await asyncio.to_thread(lambda: open(path, "w").close())
            except OSError as exc:
                telemetry.error("IDLE", "Erro ao limpar dump", exc)

        # O prompt manda responder só 'NADA' quando não há conhecimento a reter
        # (conversa de small talk). Nesse caso não cria nota — mas limpa o dump.
        if not atomos or atomos.upper().strip(".!\n ") == "NADA":
            await _limpar_dump()
            telemetry.track("IDLE", "Conversa sem conhecimento novo a reter.")
            return

        # Um arquivo por átomo (mesma regra da fila web). O dump só é limpo se ALGO
        # foi retido — senão a conversa fica pra próxima passada (nada se perde).
        salvos = await self._salvar_atomos(atomos, "Conversa", "IDLE_CONVERSA")
        if salvos:
            await _limpar_dump()
            telemetry.track("IDLE", f"Conversa atomizada: {salvos} átomo(s).")
            await self.ctx.vectorstore.sync()
        else:
            telemetry.warn("IDLE", "Nenhum átomo salvo da conversa — dump preservado p/ retry.")

        # #36 Diapasão: refina o perfil de estilo do usuário DEPOIS da atomização —
        # assim a atomização mantém a 1ª claim na GPU (preempção cede a ela primeiro)
        # e o perfil é o extra oportunista. Preemptível e best-effort.
        await self._atualizar_perfil(conteudo)

    async def _atualizar_perfil(self, conteudo: str) -> None:
        """#36: destila da conversa uma diretriz de COMO responder ao usuário e a
        persiste (+ atualiza o cache em ctx, lido no hot-path). Preemptível; 'NADA'
        do LLM mantém o perfil atual. Best-effort — nunca derruba a atomização."""
        if not settings.diapasao_habilitado:
            return
        await self._esperar_idle()
        try:
            resp = await self.ctx.llama.collect(
                prompts.prompt_perfil_conversa(conteudo, self.ctx.perfil_conversa or ""),
                max_tokens=self._max_fundo(settings.max_tokens_perfil),  # #29
                system_prompt=prompts.SYS_DIAPASAO,
                preemptible=True,
            )
        except InferenciaPreemptada:
            return
        except Exception as exc:
            telemetry.warn("DIAPASAO", f"Falha ao refinar perfil (ignorando): {exc}")
            return
        novo = diapasao.parse_perfil(resp)
        if novo and novo != self.ctx.perfil_conversa:
            self.ctx.perfil_conversa = novo
            await asyncio.to_thread(db.salvar_perfil, novo)
            telemetry.track("DIAPASAO", f"Perfil de conversa atualizado: {novo[:60]}")

    async def pesquisa_proativa(self) -> None:
        """No idle, busca na web as maiores LACUNAS (perguntas que a RAM E o banco não
        responderam), atomiza e insere — para a próxima vez já achar local. É o que faz
        o app "sempre ter algo novo pronto" sobre as dúvidas reais do usuário.

        Anti-duplicação em DOIS níveis:
        1. ALVO: se o banco JÁ cobre a lacuna (relevante) — porque outra passada de idle
           já a trouxe, ou a base cresceu — não re-pesquisa; só marca como resolvida.
        2. ÁTOMO: `_salvar_atomos` descarta o que já está indexado (dedup_dist_max).

        Preempção: cada síntese é `preemptible`. Se o usuário volta, InferenciaPreemptada
        encerra a pesquisa — o idle acabou, e as lacunas não-tocadas ficam para a próxima."""
        if not settings.idle_pesquisa_proativa:
            return
        lacunas = await asyncio.to_thread(db.get_lacunas, settings.idle_pesquisa_max * 4)
        if not lacunas:
            return
        feitas = 0
        for lac in lacunas:
            if feitas >= settings.idle_pesquisa_max:
                break
            termos = lac["termos"]
            chave = textutils.normaliza(termos)
            # Backstop contra lacuna inútil já na tabela (legada, de antes do filtro na
            # escalada): 'ok' (trivial) e 'dolar 542' (sem núcleo) nunca viram pesquisa.
            if not lacuna_pesquisavel(termos):
                await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
                continue
            await self._esperar_idle()
            # Nível 1 — o banco já cobre? (cresceu desde que a lacuna foi vista)
            local = await self.ctx.vectorstore.search(termos, texto_busca=termos)
            if local.relevante:
                await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
                continue
            dados = await self.ctx.web.search(termos, consulta=termos)
            if not dados or dados == NENHUM:
                await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
                continue
            try:
                conteudo = await self.ctx.llama.collect(
                    prompts.prompt_sintese(termos, dados),
                    max_tokens=settings.max_tokens_sintese,
                    system_prompt=prompts.SYS_SINTESE,
                    preemptible=True,
                )
            except InferenciaPreemptada:
                telemetry.track("ETL_PROATIVO", "Usuário voltou — pesquisa proativa adiada.")
                return
            except Exception as exc:
                telemetry.error("ETL_PROATIVO", f"Falha ao sintetizar lacuna '{termos}'", exc)
                await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
                continue
            # Nível 2 (dedup por átomo) acontece dentro de _salvar_atomos.
            salvos = await self._salvar_atomos(conteudo, "Proativa", "ETL_PROATIVO")
            await asyncio.to_thread(db.marcar_lacuna_pesquisada, chave)
            if salvos:
                feitas += 1
                telemetry.track("ETL_PROATIVO", f"Lacuna '{termos}': {salvos} átomo(s) novos.")
        if feitas:
            await self.ctx.vectorstore.sync()
            telemetry.track("ETL_PROATIVO", f"Pesquisa proativa: {feitas} lacuna(s) trazida(s) ao banco.")

    async def run_idle(self, itens: List[Tuple[str, str]]) -> None:
        """Orquestra o idle: 1) atomiza as pesquisas da fila, 2) atomiza a conversa,
        3) PESQUISA PROATIVA das lacunas, 4) DESCARREGA o modelo, liberando a VRAM (o
        pilar pedido: a GPU volta pra outros trabalhos quando o chat para).

        A ordem importa e foi pedida assim: o ETL PRECISA do modelo, então o unload é o
        ÚLTIMO passo. E só descarrega se ninguém voltou a interagir no meio-tempo —
        `interactive_idle` está SETADO quando não há inferência interativa em voo; se o
        usuário mandou algo, o pipeline o limpou e o unload é pulado (o próprio pipeline
        religou/manteve o modelo). Se descarregar e a mensagem chegar logo depois,
        `ensure_loaded` (no stream) religa: seguro nas duas direções."""
        await self.process_queue(itens)
        await self.summarize_dump()
        await self.pesquisa_proativa()
        await self._sincronizar_vault_pendente()
        await self._snapshot_base()

        if not settings.idle_descarregar_modelo:
            return
        if not self.ctx.interactive_idle.is_set():
            telemetry.track("ETL_POST_CHAT", "Interação retomada no idle — modelo mantido.")
            return
        await self.ctx.llama.unload()

    async def _sincronizar_vault_pendente(self) -> None:
        """#38: flush das escritas via ferramenta (nota/lista/captura) que, DURANTE a
        conversa, só marcaram o vault "sujo" em vez de reindexar na hora (o sync re-embeda
        os chunks E reconstrói a malha sobre ~13k átomos na GPU serializada — inline isso
        congelava o próximo turno por ~46s). Aqui, no idle e com a GPU livre, o `sync`
        incremental (por mtime) leva as notas ao índice.

        Idempotente e barato: se um passo anterior do idle já sincronizou
        (process_queue/summarize/proativa reindexam a base inteira por mtime, então já
        pegaram esses arquivos), o `sync` acha "nada novo" e volta rápido; ou o `_esperar_idle`
        segura até a GPU estar livre se o usuário voltou no meio. A flag é limpa em todo caso —
        o snapshot da base logo abaixo passa a ver o índice fresco."""
        if not self.ctx.vault_pendente_sync:
            return
        await self._esperar_idle()
        await self.ctx.vectorstore.sync()
        self.ctx.vault_pendente_sync = False
        telemetry.track("IDLE", "Escritas do vault (nota/lista/captura) reindexadas no idle.")

    async def _snapshot_base(self) -> None:
        """Métricas do ciclo (painel 2026-07): retrato DIÁRIO da base — total de
        chunks, composição por origem (Local/Conversa/Web) e quantos ainda carregam
        a tag #conhecimento_novo (nunca usados numa resposta). 1x/dia, fora do
        hot-path (idle), e com try PRÓPRIO: falha de observabilidade nunca aborta a
        atomização. O scan completo custa poucos MB no tamanho atual (~13k chunks);
        se a base multiplicar, trocar por count() + amostragem."""
        try:
            store = getattr(self.ctx.vectorstore, "_store", None)
            if store is None:
                return
            if await asyncio.to_thread(db.snapshot_base_hoje):
                return
            dump = await asyncio.to_thread(
                lambda: store.get(include=["documents", "metadatas"])
            )
            metas = dump.get("metadatas") or []
            docs = dump.get("documents") or []
            por_origem: dict = {}
            for md in metas:
                o = (md or {}).get("origin", "?")
                por_origem[o] = por_origem.get(o, 0) + 1
            novos = sum(1 for d in docs if d and prompts.TAG_NOVO in d)
            await asyncio.to_thread(db.salvar_snapshot_base, len(docs), por_origem, novos)
            telemetry.track(
                "BASE",
                f"Snapshot diário: {len(docs)} chunks, {novos} ainda #novo, origem={por_origem}.",
            )
        except Exception as exc:
            telemetry.error("BASE", "Falha no snapshot da base (observabilidade)", exc)
