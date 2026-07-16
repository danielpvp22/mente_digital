"""
Agente Omni — orquestra o pipeline de resposta e o ETL idle.

Mantém intactos os pilares da arquitetura:
- GPU serializada (via LlamaManager).
- Streaming + chunking de TTS (via SentenceChunker) para baixar o TTFA.
- Speculative Pre-Fetch em background.
- ETL Post-Chat só no idle (end_session), agora com PRIORIDADE para a inferência
  interativa: o ETL espera `interactive_idle` entre documentos, então uma pergunta
  do usuário não compete com a síntese pesada.
- Anti-alucinação via system prompt.

O pipeline não conhece o WebSocket: recebe um callback `send(dict) -> bool`.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from typing import Awaitable, Callable, Deque, List, Tuple

import prompts
import textutils
import tools
from audio import SentenceChunker
from config import settings
from llm import LlamaManager
from rag import NENHUM, LocalResult
from state import AppContext
from telemetry import db, telemetry

Sender = Callable[[dict], Awaitable[bool]]
STOP_WORDS = {"não", "nao", "sim", "nada", "tudo", "pode falar", "continue", "isso", "nenhum"}
# "Sem banco local" — usado no estágio RAM para reaproveitar _montar_contexto sem
# tocar no vetor (o builder ignora o local quando o texto é NENHUM).
NENHUM_LOCAL = LocalResult(NENHUM, None, False)
# Sentinela anti-alucinação (normalizado) — REGRA 1 do system prompt de resposta.
SENTINELA_INSUF = "nao tenho informacoes suficientes"


class LatencyTracker:
    """Mede o pilar de latência: TTFT (1º token) e TTFA (1º áudio) por resposta.

    Marca o primeiro instante em que cada tipo de mensagem sai pelo `send`. O clock
    é injetável para permitir teste determinístico (sem depender do relógio real).
    """

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self.t0 = clock()
        self.ttft: float | None = None
        self.ttfa: float | None = None

    def note(self, msg: dict) -> None:
        tipo = msg.get("tipo")
        if tipo == "token" and self.ttft is None:
            self.ttft = self._clock() - self.t0
        elif tipo == "audio" and self.ttfa is None:
            self.ttfa = self._clock() - self.t0

    def total(self) -> float:
        return self._clock() - self.t0

    @staticmethod
    def _ms(seg: float | None) -> int | None:
        return round(seg * 1000) if seg is not None else None


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


# ==========================================================================
# Extrator de query (resolve pronomes cruzados)
# ==========================================================================
class QueryOptimizer:
    def __init__(self, llama: LlamaManager) -> None:
        self._llama = llama

    async def optimize(self, pergunta: str, historico: Deque[Tuple[str, str]]) -> str:
        limpa = pergunta.lower().strip().replace(".", "").replace(",", "")
        if limpa in STOP_WORDS or len(limpa) < 4:
            if historico:
                # continuação: reaproveita a última pergunta, já enxuta
                return textutils.limpar_query(historico[-1][0]) or historico[-1][0]
            return limpa

        contexto = "NENHUM"
        if historico:
            turnos = []
            for q, a in list(historico)[-2:]:
                resumo = a[:150] + "..." if len(a) > 150 else a
                turnos.append(f"U: {q}\nIA: {resumo}")
            contexto = "\n".join(turnos)

        bruto = await self._llama.collect(
            prompts.prompt_extrator(contexto, pergunta),
            max_tokens=settings.max_tokens_query,
            system_prompt=prompts.SYS_EXTRATOR,
        )
        bruto = re.sub(r"[\"',.!?:*\[\]\n\r]", "", bruto).strip()
        # Rede contra o modelo que "ecoa" a frase inteira: tira saudação/fillers e capa
        # a ~6 palavras. Assim "Olá gostaria de entender... TensorFlow RT" -> "TensorFlow RT".
        termos = textutils.limpar_query(bruto) or textutils.limpar_query(pergunta) or limpa
        telemetry.track("EXTRATOR", f"Query final: [{termos}]")
        return termos


# ==========================================================================
# Pipeline principal
# ==========================================================================
class Agent:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.optimizer = QueryOptimizer(ctx.llama)
        self.tools = tools.criar_registry()
        self._filler_i = 0  # rotaciona o texto do filler p/ não ficar repetitivo

    async def _falar(self, send: Sender, frases: List[str]) -> None:
        """Envia frases prontas para o TTS conforme o chunker fecha sentenças."""
        for frase in frases:
            audio = await self.ctx.tts.synth_base64(frase)
            if audio:
                await send({"tipo": "audio", "base64": audio})

    async def _falar_texto(self, send: Sender, texto: str) -> None:
        """Fatia um texto já pronto em frases e sintetiza cada uma."""
        chunker = SentenceChunker()
        for frase in chunker.push(texto):
            await self._falar(send, [frase])
        resto = chunker.flush()
        if resto:
            await self._falar(send, [resto])

    def _ram_relevante(self, termos: str) -> List[str]:
        """Só injeta memória da sessão cujo TEMA casa com a pergunta atual.

        Corrige o Cache Hit falso: antes as 2 últimas entradas entravam sempre,
        então uma busca velha sobre 'TensorFlow' contaminava toda pergunta seguinte.
        """
        chaves = textutils.palavras_chave(termos)
        if not chaves:
            return []
        out: List[str] = []
        for tema, dados in reversed(self.ctx.memory.conhecimento_sessao):
            if textutils.tem_sobreposicao(chaves, textutils.palavras_chave(tema)):
                out.append(dados)
            if len(out) >= 2:
                break
        return out

    @staticmethod
    def _montar_contexto(local: LocalResult, ram: List[str]) -> str:
        partes = []
        if local.texto != NENHUM:
            partes.append(f"[Banco Local]\n{local.texto}")
        if ram:
            partes.append("[Memória Fresca da Sessão]\n" + "\n\n".join(ram))
        return "\n".join(partes)

    async def pipeline_resposta(self, texto_usuario: str, send: Sender) -> None:
        self.ctx.interactive_idle.clear()  # sinaliza: GPU ocupada com interação
        # Instrumenta o TTFT/TTFA sem tocar no resto: cada msg passa pelo tracker.
        tracker = LatencyTracker()

        async def send_medido(msg: dict) -> bool:
            tracker.note(msg)
            return await send(msg)

        rota = "web"
        try:
            # ROTEAMENTO DE AÇÃO (aditivo): só mensagens que parecem AÇÃO chamam o
            # roteador LLM. Pergunta de conhecimento nem paga essa chamada — cai
            # direto no pipeline afinado abaixo (TTFA preservado).
            if tools.talvez_acao(texto_usuario):
                decisao = await self._rotear(texto_usuario)
                if decisao and decisao.tool != "responder" and self.tools.get(decisao.tool):
                    telemetry.track("AGENT", f"Ação -> ferramenta '{decisao.tool}'.")
                    texto_final = await self._pipeline_tools(texto_usuario, send_medido, decisao)
                    if texto_final:
                        await append_chat_dump("IA", texto_final)
                        self.ctx.memory.registrar_turno(texto_usuario, texto_final)
                        await asyncio.to_thread(db.save_chat, texto_usuario, texto_final)
                        await self._registrar_latencia(tracker, f"tool:{decisao.tool}")
                    return

            termos = await self.optimizer.optimize(texto_usuario, self.ctx.memory.chat_history)
            # Pergunta enriquecida com o histórico p/ o GERADOR da resposta (não a
            # recuperação, que já resolve o pronome via QueryOptimizer). Sem isso, um
            # follow-up cru como "poderia explicar melhor?" chegava ao LLM SEM antecedente
            # → ele dizia sentinela mesmo com os átomos certos na mão. Dump/memória/busca
            # seguem com o texto_usuario ORIGINAL; só o prompt de resposta recebe o contexto.
            pergunta_resp = self._pergunta_com_contexto(texto_usuario)

            # FUSÃO EM CASCATA: cada fonte com átomos relevantes contribui com UM
            # parágrafo (passada de inferência própria), na ordem memória > banco > web.
            # A web só entra se o local não produziu NADA real (preserva TTFA e o
            # anti-alucinação local-first). Parágrafos separados por linha em branco.
            paragrafos: List[str] = []
            fontes: List[str] = []

            async def passada(contexto: str, fonte: str) -> None:
                # prefixo só quando JÁ há parágrafo antes (separa as passadas); é enviado
                # dentro do _responder_contexto, na 1ª emissão real (não vaza se der sentinela).
                p = await self._responder_contexto(
                    contexto, pergunta_resp, send_medido,
                    prompt_fn=prompts.prompt_resposta_atomos,
                    system=prompts.SYS_FUSAO,
                    prefixo="\n\n" if paragrafos else "",
                )
                if p:
                    paragrafos.append(p)
                    fontes.append(fonte)

            # ATALHO TIME-SENSITIVE: cotação/preço agora, notícias/clima de hoje. O banco
            # é inútil e desatualizado nesses casos — pula RAM+Banco e vai DIRETO pra web
            # (fresco, e sem pagar a passada local morta). Fora isso, cascata normal.
            if tools.talvez_tempo_real(texto_usuario):
                telemetry.track("AGENT", f"Time-sensitive — direto pra web: '{termos}'.")
                web = await self._responder_web(termos, pergunta_resp, send_medido)
                if web:
                    paragrafos.append(web)
                    fontes.append("web")
            else:
                ram = self._ram_relevante(termos)

                # ESTÁGIO 1 — RAM (memória fresca da sessão): a mais fresca, já por tema.
                if ram:
                    telemetry.track("AGENT", f"Fusão: passada RAM ({len(ram)} tópico(s)).")
                    await passada(self._montar_contexto(NENHUM_LOCAL, ram), "ram")

                # ESTÁGIO 2 — Banco vetorial: query atomizada (mesmo formato da base) colhe
                # dezenas de átomos Zettelkasten e os funde num parágrafo.
                texto_busca = await self._texto_busca(texto_usuario, termos)
                local = await self.ctx.vectorstore.search(termos, texto_busca=texto_busca)
                telemetry.track(
                    "LOCAL",
                    f"melhor_dist={local.melhor_dist} relevante={local.relevante} ram={len(ram)}",
                )
                if local.relevante:
                    telemetry.track("AGENT", "Fusão: passada Banco.")
                    await passada(self._montar_contexto(local, []), "banco")

                # ESTÁGIO 3 — Web (só SE NECESSÁRIO: nenhuma fonte local produziu algo real).
                if not paragrafos:
                    telemetry.track("AGENT", f"Local insuficiente para '{termos}'. Escalando para a web.")
                    web = await self._responder_web(termos, pergunta_resp, send_medido)
                    if web:
                        paragrafos.append(web)
                        fontes.append("web")

            texto_final = "\n\n".join(paragrafos) if paragrafos else None
            rota = "+".join(fontes) if fontes else "web"

            if texto_final:
                await append_chat_dump("IA", texto_final)
                self.ctx.memory.registrar_turno(texto_usuario, texto_final)
                await asyncio.to_thread(db.save_chat, texto_usuario, texto_final)
                await self._registrar_latencia(tracker, rota)
        except asyncio.CancelledError:
            raise  # barge-in: propaga para o LlamaManager parar o decode
        except Exception as exc:
            telemetry.error("PIPELINE", "Erro no pipeline de resposta", exc)
        finally:
            self.ctx.interactive_idle.set()  # GPU livre de novo

    async def _registrar_latencia(self, tracker: LatencyTracker, rota: str) -> None:
        ttft, ttfa, total = (
            LatencyTracker._ms(tracker.ttft),
            LatencyTracker._ms(tracker.ttfa),
            LatencyTracker._ms(tracker.total()),
        )
        telemetry.track("LATENCIA", f"rota={rota} TTFT={ttft}ms TTFA={ttfa}ms total={total}ms")
        await asyncio.to_thread(db.save_latency, rota, ttft, ttfa, total)

    async def _rotear(self, texto_usuario: str, observacoes: str = ""):
        """Pergunta ao LLM qual ferramenta usar; devolve uma `tools.Decisao` ou None."""
        bruto = await self.ctx.llama.collect(
            prompts.prompt_router(self.tools.menu(), texto_usuario, observacoes),
            max_tokens=settings.max_tokens_router,
            system_prompt=prompts.SYS_ROUTER,
            temperature=0.0,
        )
        return tools.parse_decisao(bruto)

    async def _pipeline_tools(self, texto_usuario: str, send: Sender, primeira) -> str:
        """Loop agêntico CAPADO: executa ferramentas e então fala a resposta final.

        Ferramentas terminais (calcular/hora/salvar) encerram no 1º passo. As não
        terminais (buscar_web/ler_nota/listar_notas) podem encadear até
        `max_tool_steps`, mas o loop para assim que o roteador devolve 'responder'.
        A resposta final vai por streaming + chunking (TTS), preservando o pilar.
        """
        observacoes: List[str] = []
        decisao = primeira
        passos = 0
        while decisao and decisao.tool != "responder" and passos < settings.max_tool_steps:
            tool = self.tools.get(decisao.tool)
            if tool is None:
                break
            try:
                obs = await tool.executar(decisao.args, self.ctx)
            except Exception as exc:
                telemetry.error("TOOL", f"Falha na ferramenta '{decisao.tool}'", exc)
                obs = f"erro ao executar {decisao.tool}"
            observacoes.append(f"[{decisao.tool}] {obs}")
            passos += 1
            if tool.terminal:
                break
            decisao = await self._rotear(texto_usuario, "\n".join(observacoes))

        resultados = "\n".join(observacoes) if observacoes else "nenhum resultado"
        resposta = await self._responder_stream(
            prompts.prompt_resposta_ferramentas(resultados, texto_usuario), send
        )
        if resposta.strip():
            return resposta
        # Caso raro: o phrasing final saiu vazio. Não deixe o usuário no silêncio —
        # fala o resultado bruto da ferramenta. Só executa quando vazio, então não
        # pesa no TTFA do caminho normal.
        fala = resultados[:300] if observacoes else "Pronto."
        await send({"tipo": "token", "texto": fala})
        audio = await self.ctx.tts.synth_base64(fala)
        if audio:
            await send({"tipo": "audio", "base64": audio})
        return fala

    def _pergunta_com_contexto(self, texto_usuario: str) -> str:
        """Prefixa o histórico recente à pergunta, só para o LLM de RESPOSTA.

        A recuperação já resolve pronomes (QueryOptimizer), mas o gerador recebia o
        texto cru e ficava cego a follow-ups ("explique melhor", "e sobre isso") — daí
        respondia sentinela mesmo com os átomos certos. Damos 2 turnos recentes (resposta
        anterior truncada) e marcamos [PERGUNTA ATUAL] para manter o foco. Sem histórico,
        devolve a pergunta intacta (custo zero na 1ª pergunta da sessão)."""
        hist = self.ctx.memory.chat_history
        if not hist:
            return texto_usuario
        turnos = []
        for q, a in list(hist)[-2:]:
            resumo = (a[:200] + "…") if len(a) > 200 else a
            turnos.append(f"Usuário: {q}\nVocê: {resumo}")
        return "[CONVERSA RECENTE]\n" + "\n".join(turnos) + f"\n\n[PERGUNTA ATUAL] {texto_usuario}"

    async def _texto_busca(self, texto_usuario: str, termos: str) -> str:
        """Texto que vai ao EMBEDDING da busca vetorial (o aterramento léxico segue
        usando `termos`, a query enxuta).

        Base (grátis): a pergunta natural inteira. O modelo de embedding é simétrico —
        uma frase completa casa muito melhor com os parágrafos do banco do que a query
        de 5 palavras. Com MENTE_RAG_HYDE=true, o LLM gera uma passagem hipotética no
        estilo das notas (HyDE) e a anexamos à pergunta: recall melhor ao custo de UMA
        chamada extra ao LLM — e só neste estágio, nunca quando a RAM já resolveu.
        """
        base = texto_usuario.strip() or termos
        if not settings.rag_hyde:
            return base
        try:
            hyde = await self.ctx.llama.collect(
                prompts.prompt_hyde(texto_usuario),
                max_tokens=settings.max_tokens_hyde,
                system_prompt=prompts.SYS_HYDE,
                temperature=0.0,
            )
            hyde = hyde.strip()
            if hyde:
                telemetry.track("HYDE", f"passagem: {hyde[:90]!r}")
                return f"{base}\n{hyde}"  # ancora na pergunta e enriquece o vetor
        except Exception as exc:
            telemetry.error("HYDE", "Falha ao gerar passagem hipotética", exc)
        return base

    async def _responder_contexto(
        self,
        contexto: str,
        texto_usuario: str,
        send: Sender,
        *,
        prompt_fn: Callable[[str, str], str] = prompts.prompt_resposta_cache,
        system: str = prompts.SYS_RESPOSTA,
        prefixo: str = "",
    ):
        """Responde por um contexto (RAM ou Banco) COM streaming, segurando o áudio até
        ter certeza de que não é o sentinela 'Não tenho informações suficientes'.

        Enquanto os tokens iniciais forem prefixo do sentinela, o buffer fica retido.
        Se o sentinela se confirmar, devolve None (o pipeline escala) e NADA é falado.
        Se divergir, libera o buffer e segue em streaming normal (TTFA preservado).

        `prompt_fn`/`system` permitem reusar este guard na FUSÃO por fonte (SYS_FUSAO).
        `prefixo` (ex.: '\\n\\n') é emitido só na 1ª emissão REAL — separa parágrafos das
        passadas sem vazar quebra de linha quando a passada acaba em sentinela.
        """
        chunker = SentenceChunker()
        texto_final = ""
        buffer = ""
        decidido = False
        async for token in self.ctx.llama.stream(
            prompt_fn(contexto, texto_usuario),
            max_tokens=settings.max_tokens_resposta,
            system_prompt=system,
        ):
            texto_final += token
            if decidido:
                await send({"tipo": "token", "texto": token})
                frases = chunker.push(token)
                if frases:
                    await self._falar(send, frases)
                continue

            buffer += token
            norm = textutils.normaliza(buffer)
            if SENTINELA_INSUF in norm:
                return None                       # confirmou insuficiência
            if not SENTINELA_INSUF.startswith(norm):
                decidido = True                   # divergiu do sentinela -> resposta real
                if prefixo:
                    await send({"tipo": "token", "texto": prefixo})
                await send({"tipo": "token", "texto": buffer})
                frases = chunker.push(buffer)
                if frases:
                    await self._falar(send, frases)
            # senão: ainda é prefixo do sentinela -> segura o buffer

        if not decidido:
            return None                           # só saiu (prefixo do) sentinela
        resto = chunker.flush()
        if resto:
            await self._falar(send, [resto])
        return texto_final

    async def _falar_status(self, send: Sender, texto: str) -> None:
        """Filler ESPECÍFICO: diz em poucas palavras o que está sendo feito (texto+voz).
        Só na escalada web (a única espera longa) — o local já streama rápido, sem filler.
        Sem chamada extra ao LLM: é template, então não pesa no TTFA."""
        await send({"tipo": "token", "texto": texto + " "})
        audio = await self.ctx.tts.synth_base64(texto)
        if audio:
            await send({"tipo": "audio", "base64": audio})

    def _msg_web(self, termos: str) -> str:
        # Rotaciona para não ficar repetitivo/chato quando várias perguntas escalam.
        variantes = [
            f"Isso não está nas suas notas — vou buscar sobre {termos} na web.",
            f"Não tenho isso salvo. Procurando {termos} online.",
            f"Vou complementar com a web sobre {termos}.",
        ]
        msg = variantes[self._filler_i % len(variantes)]
        self._filler_i += 1
        return msg

    async def _responder_web(self, termos: str, texto_usuario: str, send: Sender) -> str:
        # Filler específico mascara a latência da busca web (diz o que está fazendo).
        await self._falar_status(send, self._msg_web(termos))

        dados_web = await self.ctx.web.search(termos)
        self.ctx.track_task(self._prefetch(termos))  # background, web-only (ref. retida)

        # Sem dados locais NEM web (ex.: pergunta não-buscável): NÃO deixe o LLM falar
        # o sentinela cru "Não tenho informações suficientes". Fala um retorno gracioso
        # e pula o decode (mais rápido). O sentinela é sinal interno, não fala de UX.
        if NENHUM in dados_web:
            fala = "Não encontrei isso nas suas notas nem na web."
            await send({"tipo": "token", "texto": fala})
            audio = await self.ctx.tts.synth_base64(fala)
            if audio:
                await send({"tipo": "audio", "base64": audio})
            return fala

        self.ctx.memory.lembrar(termos, dados_web)
        self.ctx.memory.enfileirar_etl(termos, dados_web)

        # A web VOLTOU dados, mas eles podem não responder (projeto privado, snippet
        # sem o número etc.). Passa pelo MESMO guard anti-sentinela do local: se o LLM
        # concluir insuficiência, NÃO fala o sentinela cru — devolve None e caímos num
        # retorno gracioso. Antes usava _responder_stream (sem guard) e o sentinela
        # "Não tenho informações suficientes" vazava falado para o usuário.
        resposta = await self._responder_contexto(
            dados_web, texto_usuario, send,
            prompt_fn=prompts.prompt_resposta_web, system=prompts.SYS_RESPOSTA_WEB,
        )
        if resposta is not None:
            return resposta
        fala = "Procurei, mas não achei uma resposta clara sobre isso nas suas notas nem na web."
        await send({"tipo": "token", "texto": fala})
        audio = await self.ctx.tts.synth_base64(fala)
        if audio:
            await send({"tipo": "audio", "base64": audio})
        return fala

    async def _prefetch(self, tema: str) -> None:
        ctx_amplo = await self.ctx.web.prefetch(tema)
        if ctx_amplo:
            self.ctx.memory.lembrar(tema, ctx_amplo)

    async def _responder_stream(self, prompt_resposta: str, send: Sender) -> str:
        chunker = SentenceChunker()
        texto_final = ""
        async for token in self.ctx.llama.stream(
            prompt_resposta,
            max_tokens=settings.max_tokens_resposta,
            system_prompt=prompts.SYS_RESPOSTA,
        ):
            texto_final += token
            await send({"tipo": "token", "texto": token})
            frases = chunker.push(token)
            if frases:
                await self._falar(send, frases)
        resto = chunker.flush()
        if resto:
            await self._falar(send, [resto])
        return texto_final


# ==========================================================================
# ETL Post-Chat / Idle
# ==========================================================================
class EtlProcessor:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def _esperar_idle(self) -> None:
        """Cede a vez para a inferência interativa antes de cada tarefa pesada."""
        await self.ctx.interactive_idle.wait()

    async def process_queue(self, itens: List[Tuple[str, str]]) -> None:
        if not itens:
            return
        telemetry.track("ETL_POST_CHAT", f"Sintetizando {len(itens)} pesquisas da sessão.")
        for tema, dados in itens:
            await self._esperar_idle()
            try:
                conteudo = await self.ctx.llama.collect(
                    prompts.prompt_sintese(tema, dados),
                    max_tokens=settings.max_tokens_sintese,
                    system_prompt=prompts.SYS_SINTESE,
                )
                nome = f"Sintese_{tema[:15].replace(' ', '_')}_{int(time.time())}.md"
                caminho = os.path.join(str(self.ctx.settings.dir_conhecimento_novo), nome)

                def _save(c=caminho, t=tema, body=conteudo) -> None:
                    with open(c, "w", encoding="utf-8") as f:
                        f.write(f"# {t}\n\n{body}")

                await asyncio.to_thread(_save)
                await asyncio.to_thread(db.log_etl, "ETL_POST_CHAT", nome, "CONCLUIDO")
            except Exception as exc:
                telemetry.error("ETL_POST_CHAT", f"Falha ao sintetizar '{tema}'", exc)

        await self.ctx.vectorstore.sync()
        telemetry.track("ETL_POST_CHAT", "Banco Vetorial atualizado.")

    async def summarize_dump(self) -> None:
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
        telemetry.track("IDLE", "Analisando histórico bruto da conversa...")
        resumo = await self.ctx.llama.collect(
            prompts.prompt_resumo_sessao(conteudo),
            max_tokens=settings.max_tokens_resumo,
            system_prompt=prompts.SYS_RESUMO,
        )
        nome = f"Resumo_Sessao_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        caminho = os.path.join(str(self.ctx.settings.dir_conhecimento_novo), nome)

        def _save_and_clear() -> None:
            with open(caminho, "w", encoding="utf-8") as f:
                f.write(resumo)
            open(path, "w").close()

        try:
            await asyncio.to_thread(_save_and_clear)
            telemetry.track("IDLE", f"Resumo da sessão salvo: {nome}")
            await self.ctx.vectorstore.sync()
        except Exception as exc:
            telemetry.error("IDLE", "Erro ao salvar resumo", exc)

    async def run_idle(self, itens: List[Tuple[str, str]]) -> None:
        """Orquestra o idle: 1) sínteses da fila, 2) mega-resumo do dump."""
        await self.process_queue(itens)
        await self.summarize_dump()
