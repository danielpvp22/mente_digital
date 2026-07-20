"""
Agente Omni — orquestra o pipeline de resposta e o ETL idle.

Mantém intactos os pilares da arquitetura:
- GPU serializada (via LlamaManager).
- Streaming + chunking de TTS (via SentenceChunker) para baixar o TTFA.
- Speculative Pre-Fetch em background.
- ETL Post-Chat só no idle (end_session), com PRIORIDADE REAL para a inferência
  interativa. Antes esta linha mentia: o ETL esperava `interactive_idle` apenas
  ENTRE documentos, então a pergunta que chegasse no meio de uma síntese esperava o
  decode inteiro no lock da GPU (medido: 4,6s de TTFT, e o teto é max_tokens_sintese).
  Agora o pipeline marca a GPU como ocupada e PREEMPTA o decode de background em
  curso (llm.preempt), que morre em ~1 token e devolve o item pra fila — medido:
  TTFT de 4605ms -> 26ms.
- Anti-alucinação via system prompt.

O pipeline não conhece o WebSocket: recebe um callback `send(dict) -> bool`.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Deque, List, Optional, Tuple

import calendario
import contradicao
import diapasao
import fio
import habitos
import mestre
import prompts
import srs
import textutils
import tools
import verbosidade
# A atomização (um .md por ideia — Zettelkasten puro) mora em atomos.py; os nomes
# seguem re-exportados por aqui porque scripts/, eval/ e testes importam de `agent`.
from atomos import (
    _e_titulo,
    _parece_atomo,
    _slug_titulo,
    dividir_atomos,
    normalizar_atomo,
    normalizar_malha,
)
from audio import SentenceChunker
from config import settings
from llm import InferenciaPreemptada, LlamaManager
# A interpretação da pergunta (QueryOptimizer + heurísticas puras: referência ao
# turno anterior, tema de síntese, frase citada, lacuna pesquisável) mora em
# otimizador.py; re-exportada por aqui pelos mesmos motivos de atomos.py.
from otimizador import (
    QueryOptimizer,
    extrair_tema_sintese,
    frase_citada,
    lacuna_pesquisavel,
    referencia_contexto,
)
from rag import NENHUM, LocalResult, strip_frontmatter
from state import AppContext, SessionMemory
from telemetry import db, telemetry

Sender = Callable[[dict], Awaitable[bool]]
# "Sem banco local" — usado no estágio RAM para reaproveitar _montar_contexto sem
# tocar no vetor (o builder ignora o local quando o texto é NENHUM).
NENHUM_LOCAL = LocalResult(NENHUM, None, False)
# Sentinela anti-alucinação (normalizado) — REGRA 1 do system prompt de resposta.
SENTINELA_INSUF = "nao tenho informacoes suficientes"


class LatencyTracker:
    """Mede a latência por resposta, QUEBRADA POR ESTÁGIO (F4).

    Marca o 1º instante de cada tipo de msg que sai pelo `send` E conta os tokens para
    derivar o tok/s do DECODE (descontando o prefill, que já está no TTFT). O clock é
    injetável para teste determinístico. `stt_ms` é preenchido de FORA (a transcrição
    roda no ws.py, antes do pipeline) — por isso não é medido aqui.
    """

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self.t0 = clock()
        self.ttft: float | None = None
        self.ttfa: float | None = None
        self.n_tokens = 0
        self._t_ultimo_token: float | None = None
        self.stt_ms: int | None = None   # transcrição (voz), setado pelo ws.py

    def note(self, msg: dict) -> None:
        tipo = msg.get("tipo")
        if tipo == "token":
            agora = self._clock()
            if self.ttft is None:
                self.ttft = agora - self.t0
            self.n_tokens += 1
            self._t_ultimo_token = agora
        elif tipo == "audio" and self.ttfa is None:
            self.ttfa = self._clock() - self.t0

    def total(self) -> float:
        return self._clock() - self.t0

    def decode_tok_s(self) -> float | None:
        """tok/s do DECODE: (n-1) / (último token − 1º token). Desconta o prefill —
        senão um prompt RAG longo faria o modelo 'parecer lento'. None com < 2 tokens
        (sem janela de decode medível). Espelha eval/ab_modelos._gerar."""
        if self.n_tokens < 2 or self._t_ultimo_token is None or self.ttft is None:
            return None
        dur = self._t_ultimo_token - (self.t0 + self.ttft)
        return round((self.n_tokens - 1) / dur, 1) if dur > 0 else None

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
# Pipeline principal
# ==========================================================================
class Agent:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.optimizer = QueryOptimizer(ctx.llama)
        self.tools = tools.criar_registry()
        self._filler_i = 0  # rotaciona o texto do filler p/ não ficar repetitivo
        self._atalhos: Optional[dict] = None  # cache dos atalhos-mestre (#2), lazy do DB

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

    def _ram_relevante(self, termos: str, mem: SessionMemory) -> List[str]:
        """Só injeta memória da sessão cujo TEMA casa com a pergunta atual.

        Corrige o Cache Hit falso: antes as 2 últimas entradas entravam sempre,
        então uma busca velha sobre 'TensorFlow' contaminava toda pergunta seguinte.
        """
        chaves = textutils.palavras_chave(termos)
        if not chaves:
            return []
        out: List[str] = []
        for tema, dados in reversed(mem.conhecimento_sessao):
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
            # A RAM da sessão é SEMPRE prefetch da WEB (só o _prefetch chama lembrar).
            # Rotulá-la como GENÉRICA impede que a fusão apresente um kit qualquer da
            # web como se fosse a lista do PROJETO do usuário (medido: pergunta sobre
            # 'a lista de compra do meu projeto' respondida com um kit ESP32 aleatório).
            partes.append(
                "[Contexto amplo da WEB — informação GENÉRICA, pode não ser específica "
                "do caso do usuário]\n" + "\n\n".join(ram)
            )
        return "\n".join(partes)

    async def pipeline_resposta(
        self, texto_usuario: str, send: Sender, mem: SessionMemory,
        stt_ms: Optional[int] = None,
    ) -> None:
        # Instrumenta o timing por estágio: cada msg passa pelo tracker (F4).
        tracker = LatencyTracker()
        tracker.stt_ms = stt_ms   # transcrição (voz) medida no ws.py, antes daqui

        async def send_medido(msg: dict) -> bool:
            tracker.note(msg)
            return await send(msg)

        # A ORDEM É O MECANISMO, e ela tem que ser esta:
        #   1) `interativo()` faz o clear() -> o ETL que acordar agora volta a dormir;
        #   2) `preempt()` corta o decode de ETL que JÁ estava rodando.
        # Invertido, existe uma janela entre o preempt e o clear em que o ETL vê
        # "GPU livre", pega o lock e começa OUTRA síntese — e a pergunta espera de novo.
        async with self.ctx.interativo():
            self.ctx.llama.preempt()
            # Origem de voz: `stt_ms` só é preenchido quando o turno veio de TRANSCRIÇÃO
            # (ws._check_silence). Turno de voz será OUVIDO -> estilo falado (aplicar_fala).
            await self._pipeline(
                texto_usuario, send_medido, tracker, mem, origem_voz=stt_ms is not None
            )

    async def _pipeline(
        self, texto_usuario: str, send_medido: Sender, tracker: LatencyTracker, mem: SessionMemory,
        origem_voz: bool = False,
    ) -> None:
        rota = "web"
        try:
            # PALAVRA-MESTRE: se a mensagem começa por ela, é um COMANDO de agente e vai
            # por um fluxo ISOLADO (determinístico primeiro, LLM só se necessário). Não
            # cai no pipeline de conhecimento. Sem a palavra-mestre, tudo segue como hoje.
            if settings.palavra_mestre_habilitada:
                comando = mestre.separar(texto_usuario, settings.palavra_mestre)
                if comando is not None:
                    await self._fluxo_mestre(comando, send_medido, tracker, mem)
                    return

            # EFEMERIDADE — decidida UMA vez, no topo, porque governa os DOIS caminhos
            # de ingestão: a fila do ETL (web) e o dump da conversa (que o idle atomiza).
            # Medido no vault: 45 dos 48 átomos-lixo vieram da fila, 3 do dump.
            efemero = tools.e_efemero(texto_usuario)

            # SÍNTESE SOB DEMANDA (#23): "o que eu sei sobre X" tem FLUXO PRÓPRIO
            # (map-reduce sobre o vault), separado do pipeline de resposta pontual —
            # é o que evita estourar o contexto num tema grande.
            tema_sintese = extrair_tema_sintese(texto_usuario)
            if tema_sintese:
                telemetry.track("AGENT", f"Síntese sob demanda: '{tema_sintese}'.")
                await self._sintese_sob_demanda(tema_sintese, send_medido, mem, tracker)
                return

            # ROTEAMENTO DE AÇÃO (aditivo): só mensagens que parecem AÇÃO chamam o
            # roteador LLM. Pergunta de conhecimento nem paga essa chamada — cai
            # direto no pipeline afinado abaixo (TTFA preservado).
            if tools.talvez_acao(texto_usuario):
                decisao = await self._rotear(texto_usuario)
                if decisao and decisao.tool != "responder" and self.tools.get(decisao.tool):
                    telemetry.track("AGENT", f"Ação -> ferramenta '{decisao.tool}'.")
                    tool = self.tools.get(decisao.tool)
                    texto_final = await self._pipeline_tools(texto_usuario, send_medido, decisao, auditar=not mem.confidencial, mem=mem)
                    if texto_final:
                        # Ações de AGENDA/LISTA (registra_conhecimento=False) não vão para o
                        # dump: "lembrete #3 criado" não é conhecimento a eternizar no vault.
                        # Modo confidencial (#5) também não persiste nada (só a RAM).
                        if not efemero and tool.registra_conhecimento and not mem.confidencial:
                            await append_chat_dump("IA", texto_final)
                        mem.registrar_turno(texto_usuario, texto_final)
                        if not mem.confidencial:
                            await asyncio.to_thread(db.save_chat, texto_usuario, texto_final, mem.conversa_id)
                        await self._registrar_latencia(tracker, f"tool:{decisao.tool}")
                    return

            # "FONTE?" (painel): zera a proveniência JÁ — se o turno for cancelado no
            # meio (barge-in), "fonte?" nunca responde sobre a resposta errada.
            mem.ultimas_fontes = []

            termos = await self.optimizer.optimize(texto_usuario, mem.chat_history)
            # Pergunta enriquecida com o histórico p/ o GERADOR da resposta (não a
            # recuperação, que já resolve o pronome via QueryOptimizer). Sem isso, um
            # follow-up cru como "poderia explicar melhor?" chegava ao LLM SEM antecedente
            # → ele dizia sentinela mesmo com os átomos certos na mão. Dump/memória/busca
            # seguem com o texto_usuario ORIGINAL; só o prompt de resposta recebe o contexto.
            pergunta_resp = self._pergunta_com_contexto(texto_usuario, mem)

            # FUSÃO EM CASCATA: cada fonte com átomos relevantes contribui com UM
            # parágrafo (passada de inferência própria), na ordem memória > banco > web.
            # A web só entra se o local não produziu NADA real (preserva TTFA e o
            # anti-alucinação local-first). Parágrafos separados por linha em branco.
            paragrafos: List[str] = []
            fontes: List[str] = []

            # Verbosidade (#7): a pergunta define o tamanho da resposta (e a latência).
            nivel = verbosidade.classificar(texto_usuario)
            nivel = verbosidade.aplicar_tutor(nivel, mem.tutor)   # #44: modo tutor sobrepõe
            # Estilo falado: por último, porque COMPÕE com qualquer nível (inclusive o
            # tutor) em vez de substituir — muda o registro do texto, não o tamanho.
            nivel = verbosidade.aplicar_fala(nivel, origem_voz)
            if nivel.nome != "normal":
                telemetry.track("VERBOSIDADE", f"nível={nivel.nome} max_tokens={nivel.max_tokens}")

            async def passada(contexto: str, fonte: str) -> None:
                # prefixo só quando JÁ há parágrafo antes (separa as passadas); é enviado
                # dentro do _responder_contexto, na 1ª emissão real (não vaza se der sentinela).
                p = await self._responder_contexto(
                    contexto, pergunta_resp, send_medido,
                    prompt_fn=prompts.prompt_resposta_atomos,
                    system=prompts.SYS_FUSAO,
                    prefixo="\n\n" if paragrafos else "",
                    max_tokens=nivel.max_tokens,
                    instrucao_extra=self._instrucao_com_perfil(nivel.instrucao),
                )
                if p:
                    paragrafos.append(p)
                    fontes.append(fonte)

            # DEFINICIONAL DE CONHECIMENTO GERAL (Part A + lever B): "o que é X", "quem foi
            # Y", "me explica Z". O vault do dono é PESSOAL — deixá-lo responder "o que é
            # RAG" devolvia a nota-piada ("RAG = base do Tarkov"). LEVER B (não mais rota
            # cega): a pergunta SEGUE a cascata local, mas o estágio Banco só é aceito se o
            # vault cobrir o tema com FORÇA (>= definicional_min_atomos átomos); vault fraco
            # escala pra web. Assim uma definição bem coberta responde local (rápido), e só
            # o tema raso/piada vai à web. Pergunta pessoal é excluída em
            # tools.pergunta_definicional. Botão MENTE_ROTEAR_DEFINICIONAL_WEB.
            definicional = (
                settings.rotear_definicional_web
                and tools.pergunta_definicional(texto_usuario)
            )
            # ATALHO TIME-SENSITIVE: cotação/preço agora, notícias/clima de hoje. O banco
            # é inútil e desatualizado nesses casos — pula RAM+Banco e vai DIRETO pra web
            # (fresco, e sem pagar a passada local morta). Fora isso, cascata normal.
            if tools.talvez_tempo_real(texto_usuario):
                telemetry.track("AGENT", f"time-sensitive — direto pra web: '{termos}'.")
                web = await self._responder_web(
                    termos, pergunta_resp, send_medido, mem,
                    consulta_rank=texto_usuario, efemero=efemero, nivel=nivel,
                )
                if web:
                    paragrafos.append(web)
                    fontes.append("web")
            else:
                ram = self._ram_relevante(termos, mem)

                # ESTÁGIO 1 — RAM (memória fresca da sessão): a mais fresca, já por tema.
                if ram:
                    telemetry.track("AGENT", f"Fusão: passada RAM ({len(ram)} tópico(s)).")
                    antes_ram = len(paragrafos)
                    await passada(self._montar_contexto(NENHUM_LOCAL, ram), "ram")
                    if len(paragrafos) > antes_ram:
                        mem.ultimas_fontes.append("memoria")   # proveniência ("fonte?")

                # EARLY-STOP (#3): se uma fonte já respondeu com confiança (passada
                # não-sentinela), PARA a cascata — não roda o Banco (nem a busca vetorial,
                # nem sua passada de inferência). Economiza um decode na GPU serializada.
                # Botão MENTE_EARLY_STOP_CASCATA; desligado, volta à fusão RAM+Banco.
                if settings.early_stop_cascata and paragrafos:
                    telemetry.track("AGENT", "Early-stop: RAM respondeu — pula Banco/Web.")
                else:
                    # ESTÁGIO 2 — Banco vetorial: query atomizada (mesmo formato da base)
                    # colhe dezenas de átomos Zettelkasten e os funde num parágrafo.
                    texto_busca = await self._texto_busca(texto_usuario, termos)
                    local = await self.ctx.vectorstore.search(
                        termos, texto_busca=texto_busca, economico=mem.economico
                    )
                    telemetry.track(
                        "LOCAL",
                        f"melhor_dist={local.melhor_dist} relevante={local.relevante} ram={len(ram)}",
                    )
                    # LEVER B: numa pergunta definicional, o Banco só é aceito se o vault
                    # cobre o tema com FORÇA (>= definicional_min_atomos átomos DISTINTOS
                    # em `fontes`). O Tarkov era 1 átomo-piada → abaixo do mínimo → o Banco
                    # é descartado e a resposta cai na escalada web (fonte autoritativa).
                    # Tema bem coberto passa e responde local, sem pagar web.
                    vault_fraco = (
                        definicional
                        and len(local.fontes) < settings.definicional_min_atomos
                    )
                    if local.relevante and vault_fraco:
                        telemetry.track(
                            "AGENT",
                            f"Definicional com vault fraco ({len(local.fontes)} < "
                            f"{settings.definicional_min_atomos} átomos) — escala pra web.",
                        )
                    elif local.relevante:
                        telemetry.track("AGENT", "Fusão: passada Banco.")
                        antes = len(paragrafos)
                        await passada(self._montar_contexto(local, []), "banco")
                        # PROMOÇÃO: se o Banco de fato contribuiu (passada não-sentinela),
                        # os átomos usados "amadureceram" — tira o #conhecimento_novo deles.
                        # Em background: não pesa no TTFA da resposta atual.
                        if len(paragrafos) > antes and local.fontes:
                            self.ctx.track_task(self._consolidar_fontes(local.fontes))
                        if len(paragrafos) > antes:
                            # Proveniência ("fonte?"): os MESMOS chunks da promoção.
                            mem.ultimas_fontes.extend(f"nota:{f}" for f in local.fontes)

                # ESTÁGIO 3 — Web (só SE NECESSÁRIO: nenhuma fonte local produziu algo real).
                if not paragrafos:
                    telemetry.track("AGENT", f"Local insuficiente para '{termos}'. Escalando para a web.")
                    # LACUNA: nem a RAM nem o banco tinham. Registra para a pesquisa
                    # proativa do idle trazer isto pronto na próxima vez. `efemero` (da
                    # pergunta original) barra 'clima amanhã'; `lacuna_pesquisavel` barra
                    # o trivial ('ok') e o sem-núcleo ('dolar 542') — ver a função.
                    # Modo confidencial (#5): a lacuna é um artefato DERIVADO da pergunta
                    # sigilosa — persisti-la vazaria o assunto; fica de fora.
                    if not efemero and not mem.confidencial and lacuna_pesquisavel(termos):
                        await asyncio.to_thread(
                            db.save_lacuna, textutils.normaliza(termos), termos
                        )
                    # `efemero` também aqui: é POR ESTE caminho que "clima em lisboa
                    # amanhã" chega (o gate de rota não o pega), escala pra web e hoje
                    # é ingerido. Sem isto o bloqueio vazaria os casos reais medidos.
                    web = await self._responder_web(
                        termos, pergunta_resp, send_medido, mem,
                        consulta_rank=texto_usuario, efemero=efemero, nivel=nivel,
                    )
                    if web:
                        paragrafos.append(web)
                        fontes.append("web")

            texto_final = "\n\n".join(paragrafos) if paragrafos else None
            rota = "+".join(fontes) if fontes else "web"

            if texto_final:
                # Dump só do que pode virar conhecimento: o idle atomiza este arquivo,
                # então um turno efêmero aqui vira "## Hora atual" no Zettelkasten. O
                # turno segue no SQLite (histórico do usuário) e na RAM (follow-up).
                # Modo confidencial (#5): nada é persistido — vive só na RAM da sessão.
                if not efemero and not mem.confidencial:
                    await append_chat_dump("IA", texto_final)
                mem.registrar_turno(texto_usuario, texto_final)
                if not mem.confidencial:
                    await asyncio.to_thread(db.save_chat, texto_usuario, texto_final, mem.conversa_id)
                await self._registrar_latencia(tracker, rota)
        except asyncio.CancelledError:
            raise  # barge-in: propaga para o LlamaManager parar o decode
        except Exception as exc:
            telemetry.error("PIPELINE", "Erro no pipeline de resposta", exc)
        # Sem `finally` liberando o idle: quem faz isso é o `interativo()` do chamador,
        # e só quando o ÚLTIMO pipeline em voo sair (ver AppContext.interativo).

    async def _registrar_latencia(self, tracker: LatencyTracker, rota: str) -> None:
        ttft, ttfa, total = (
            LatencyTracker._ms(tracker.ttft),
            LatencyTracker._ms(tracker.ttfa),
            LatencyTracker._ms(tracker.total()),
        )
        toks = tracker.decode_tok_s()
        telemetry.track(
            "LATENCIA",
            f"rota={rota} stt={tracker.stt_ms}ms TTFT={ttft}ms tok/s={toks} "
            f"TTFA={ttfa}ms total={total}ms n_tok={tracker.n_tokens}",
        )
        await asyncio.to_thread(
            db.save_latency, rota, ttft, ttfa, total, tracker.stt_ms, toks, tracker.n_tokens
        )

    async def _rotear(self, texto_usuario: str, observacoes: str = ""):
        """Pergunta ao LLM qual ferramenta usar; devolve uma `tools.Decisao` ou None."""
        bruto = await self.ctx.llama.collect(
            prompts.prompt_router(self.tools.menu(), texto_usuario, observacoes),
            max_tokens=settings.max_tokens_router,
            system_prompt=prompts.SYS_ROUTER,
            temperature=0.0,
        )
        return tools.parse_decisao(bruto)

    async def _pipeline_tools(
        self, texto_usuario: str, send: Sender, primeira, auditar: bool = True,
        mem: Optional[SessionMemory] = None,
    ) -> str:
        """Loop agêntico CAPADO: executa ferramentas e então fala a resposta final.

        Ferramentas terminais (calcular/hora/salvar) encerram no 1º passo. As não
        terminais (buscar_web/ler_nota/listar_notas) podem encadear até
        `max_tool_steps`, mas o loop para assim que o roteador devolve 'responder'.
        A resposta final vai por streaming + chunking (TTS), preservando o pilar.

        `auditar` (False em modo confidencial) controla o registro na trilha (#27).
        `mem` (quando dado) recebe a reversão da mutação p/ o "mestre, desfaça" (#8) —
        cobre o caminho LLM (ex.: "me lembra de ligar amanhã", que o parse_rapido defere)."""
        observacoes: List[str] = []
        executadas: List[tuple] = []   # (Decisao, resultado) p/ computar a reversão (#8)
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
            if auditar and tool.auditavel:
                await asyncio.to_thread(db.registrar_auditoria, decisao.tool, obs)
            observacoes.append(f"[{decisao.tool}] {obs}")
            executadas.append((decisao, obs))
            passos += 1
            if tool.terminal:
                break
            decisao = await self._rotear(texto_usuario, "\n".join(observacoes))
        self._lembrar_reversao(mem, executadas)

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

    async def _emitir_falado(self, send: Sender, texto: str) -> None:
        """Emite um texto pronto: token (para a tela) + áudio (TTS). Sem LLM."""
        await send({"tipo": "token", "texto": texto})
        await self._falar_texto(send, texto)

    async def _falar_fontes(self, send: Sender, mem: SessionMemory) -> tuple:
        """"Fonte?" (painel 2026-07): FALA a proveniência da última resposta de
        conhecimento. Por template — os títulos vêm do stem do arquivo (legível no
        Zettelkasten, que nomeia por ideia) e a web vira DOMÍNIO (URL é inaudível).
        Top-3 por categoria para a fala não virar ladainha."""
        fontes = list(mem.ultimas_fontes or [])
        if not fontes:
            fala = (
                "Não tenho registro de fonte para a última resposta — ou ainda não "
                "respondi nada nesta conversa, ou a resposta veio do meu conhecimento base."
            )
            await self._emitir_falado(send, fala)
            return fala, "mestre:fonte"

        def _titulo(caminho: str) -> str:
            nome = caminho.replace("\\", "/").rsplit("/", 1)[-1]
            if nome.lower().endswith(".md"):
                nome = nome[:-3]
            return nome.replace("_", " ").strip()

        notas = [_titulo(f[len("nota:"):]) for f in fontes if f.startswith("nota:")]
        webs = list(dict.fromkeys(f[len("web:"):] for f in fontes
                                  if f.startswith("web:") and f != "web:"))
        partes = []
        if "memoria" in fontes:
            partes.append("da memória fresca desta conversa")
        if notas:
            extra = f", e mais {len(notas) - 3} nota(s)" if len(notas) > 3 else ""
            partes.append("das suas notas: " + "; ".join(notas[:3]) + extra)
        if webs:
            partes.append("da web: " + ", ".join(webs[:3]))
        elif any(f == "web:" for f in fontes):
            partes.append("da web")
        fala = "A última resposta veio " + " e ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:fonte"

    async def _executar_acoes_rapidas(
        self, acoes: List["tools.Decisao"], send: Sender, auditar: bool = True,
        mem: Optional[SessionMemory] = None, prefixo: str = "",
    ) -> str:
        """Executa uma ou mais ferramentas determinísticas e FALA o resultado direto —
        sem passar pelo LLM (o texto de retorno da ferramenta já é amigável). É o que
        torna 'mestre, põe pão na lista' instantâneo e barato.

        `auditar` (False em modo confidencial) controla o registro na trilha (#27).
        `mem` (quando dado) recebe a reversão desta ação p/ o "mestre, desfaça" (#8);
        `prefixo` prefixa a fala (ex.: "Desfeito. " ao executar a própria reversão)."""
        resultados: List[str] = []
        executadas: List[tuple] = []   # (Decisao, resultado) p/ computar a reversão
        for dec in acoes:
            tool = self.tools.get(dec.tool)
            if tool is None:
                continue
            try:
                obs = await tool.executar(dec.args, self.ctx)
            except Exception as exc:
                telemetry.error("MESTRE", f"Falha na ferramenta '{dec.tool}'", exc)
                obs = f"não consegui executar {dec.tool}"
            if auditar and tool.auditavel:
                await asyncio.to_thread(db.registrar_auditoria, dec.tool, obs)
            resultados.append(obs)
            executadas.append((dec, obs))
        self._lembrar_reversao(mem, executadas)
        final = prefixo + (" ".join(r for r in resultados if r) or "Pronto.")
        await self._emitir_falado(send, final)
        # GATILHOS (#11): um item adicionado à lista é um EVENTO interno — dispara as regras
        # que casam. Rodam DEPOIS da fala principal e por um caminho direto (sem re-emitir).
        for dec, obs in executadas:
            if dec.tool == "adicionar_item" and textutils.normaliza(obs or "").startswith("adicionei"):
                await self._disparar_gatilhos("lista_add", str(dec.args.get("item", "")), send)
        return final

    def _lembrar_reversao(self, mem: Optional[SessionMemory], executadas: List[tuple]) -> None:
        """Guarda na sessão as ações que DESFAZEM o que acabou de rodar (#8).

        Só sobrescreve o alvo de undo quando há de fato algo reversível — assim uma
        leitura (ler_lista) ou uma ação que falhou não apaga o undo pendente anterior."""
        if mem is None or not executadas:
            return
        rev = mestre.reverter(executadas)
        if rev:
            mem.ultima_reversivel = rev
            # guarda também as ações FORWARD p/ o "corrige" refazer com o valor certo (#9).
            mem.ultima_acao = [dec for dec, _ in executadas]

    async def _desfazer(self, send: Sender, mem: SessionMemory, auditar: bool) -> tuple:
        """Executa a reversão da última ação da sessão (#8). Devolve (fala, rota)."""
        revs = mem.ultima_reversivel
        if not revs:
            fala = "Não tenho nenhuma ação recente para desfazer."
            await self._emitir_falado(send, fala)
            return fala, "mestre:desfazer_vazio"
        # Consome ANTES de executar: a própria reversão não vira novo alvo de undo
        # (mem=None abaixo), então "desfaça, desfaça" não fica num ping-pong.
        mem.ultima_reversivel = None
        mem.ultima_acao = None
        fala = await self._executar_acoes_rapidas(
            revs, send, auditar=auditar, mem=None, prefixo="Desfeito. "
        )
        return fala, "mestre:desfazer"

    async def _corrigir(self, comando: str, send: Sender, mem: SessionMemory, auditar: bool) -> tuple:
        """Corta-e-corrige (#9): desfaz a última adição e refaz com o valor certo.

        "mestre, corrige para leite" (após "adiciona pão") = remover pão + adicionar leite.
        Reaproveita a reversão do #8 (undo) e refaz a ação forward trocando o valor."""
        certo = mestre.parse_correcao(comando)
        if not certo:
            fala = "Não entendi a correção. Diga, por exemplo, 'corrige para leite'."
            await self._emitir_falado(send, fala)
            return fala, "mestre:corrige_incompleto"
        redo = mestre.refazer_com(mem.ultima_acao or [], certo)
        if not redo:
            fala = "Não tenho uma ação recente de lista para corrigir."
            await self._emitir_falado(send, fala)
            return fala, "mestre:corrige_vazio"
        undo = list(mem.ultima_reversivel or [])
        mem.ultima_reversivel = None
        mem.ultima_acao = None
        # Executa undo + redo SEM registrar reversão automática (mem=None): abaixo
        # definimos o alvo de undo manualmente como só o REDO — assim correções
        # ENCADEADAS ("corrige para leite", depois "para água") ficam limpas, sem
        # ressuscitar o valor original a cada nova correção.
        fala = await self._executar_acoes_rapidas(
            undo + list(redo), send, auditar=auditar, mem=None, prefixo="Corrigido. "
        )
        mem.ultima_acao = list(redo)
        mem.ultima_reversivel = [tools.Decisao("remover_item", dict(d.args)) for d in redo]
        return fala, "mestre:corrige"

    async def _atalhos_cache(self) -> dict:
        """Atalhos-mestre (#2) carregados do DB uma vez por processo (apelido -> comando)."""
        if self._atalhos is None:
            self._atalhos = await asyncio.to_thread(db.listar_atalhos)
        return self._atalhos

    async def _criar_atalho(self, nome: str, send: Sender, mem: SessionMemory) -> tuple:
        """Grava um atalho nomeado para o ÚLTIMO comando-mestre resolvido (#2)."""
        alvo = mem.ultimo_comando_mestre
        if not alvo:
            fala = "Não tenho um comando recente para transformar em atalho."
            await self._emitir_falado(send, fala)
            return fala, "mestre:atalho_vazio"
        chave = textutils.normaliza(nome)
        await asyncio.to_thread(db.salvar_atalho, chave, alvo)
        # Já tem atalho -> não ofereça atalho de novo para essa intenção.
        await asyncio.to_thread(db.marcar_sugerido, textutils.normaliza(alvo))
        (await self._atalhos_cache())[chave] = alvo
        fala = f"Pronto. Agora é só dizer '{settings.palavra_mestre}, {nome}' que eu faço isso."
        await self._emitir_falado(send, fala)
        return fala, "mestre:atalho_criado"

    async def _talvez_sugerir_atalho(self, comando: str, send: Sender) -> Optional[str]:
        """Conta a intenção e, se ela virou hábito (e ainda não foi sugerida), OFERECE um
        atalho — uma vez só (#2). Devolve a fala da sugestão (p/ anexar ao histórico) ou None."""
        minimo = settings.atalho_sugestao_min
        if minimo <= 0:
            return None
        assinatura = textutils.normaliza(comando)
        n, ja_sugerido = await asyncio.to_thread(db.registrar_frequencia, assinatura, comando)
        if n < minimo or ja_sugerido:
            return None
        await asyncio.to_thread(db.marcar_sugerido, assinatura)
        fala = (
            f"Aliás, você já pediu isso {n} vezes. Se quiser um atalho, diga "
            f"'{settings.palavra_mestre}, atalho' e um apelido curto."
        )
        await self._emitir_falado(send, fala)
        return fala

    def _instrucao_com_perfil(self, base: str) -> str:
        """#36 Diapasão: anexa a diretriz de estilo do usuário (COMO ele prefere ser
        respondido) à instrução de resposta. No-op se desligado ou sem perfil."""
        if not settings.diapasao_habilitado:
            return base
        extra = diapasao.instrucao_do_perfil(self.ctx.perfil_conversa)
        if not extra:
            return base
        return f"{base}\n{extra}" if base else extra

    async def _descobrir_conexoes(self, send: Sender) -> tuple:
        """G8 (descobridor de conexões): acha PONTES no vault — notas que ligam dois temas
        ESTABELECIDOS que quase nunca co-ocorrem — e as fala. Sob demanda, sem push. O grafo
        roda em thread (só CPU/RAM) para não travar o loop. Malha vazia -> nada a conectar."""
        malha = self.ctx.vectorstore.malha
        pontes = await asyncio.to_thread(
            malha.pontes,
            settings.conexao_df_min,
            settings.conexao_coocorrencia_max,
            settings.conexao_limite,
        )
        if not pontes:
            fala = "Não achei conexões novas no seu vault por enquanto."
        else:
            def _titulo(src: str) -> str:
                base = src.replace("\\", "/").rsplit("/", 1)[-1]
                return base[:-3] if base.endswith(".md") else base

            partes = [
                f"sua nota '{_titulo(p.source)}' liga {p.conceito_a} e {p.conceito_b}"
                for p in pontes
            ]
            fala = "Achei algumas conexões: " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:conexoes"

    async def _reportar_contradicoes(self, send: Sender) -> tuple:
        """#24: fala as contradições que o idle já achou entre notas do vault. Sob
        demanda, sem push, sem LLM aqui (só lê a tabela — a detecção rodou no idle)."""
        achadas = await asyncio.to_thread(db.contradicoes_abertas, settings.conexao_limite)
        if not achadas:
            fala = "Não encontrei contradições entre as suas notas por enquanto."
        else:
            def _titulo(src: str) -> str:
                base = str(src or "").replace("\\", "/").rsplit("/", 1)[-1]
                return base[:-3] if base.endswith(".md") else base

            partes = [
                f"entre '{_titulo(c['a'])}' e '{_titulo(c['b'])}': {c['resumo']}"
                for c in achadas
            ]
            fala = "Achei possíveis contradições — " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:contradicoes"

    async def _navegar(self, nav: dict, send: Sender, mem: SessionMemory) -> tuple:
        """#14: executa uma ação de navegação da UI. Manda {tipo:"navegar"} para o
        front (que faz a mudança de tela) e uma confirmação falada curta. Para
        'carregar_conversa', resolve o TEMA falado num id de conversa antes."""
        acao = nav["acao"]
        if acao == "carregar_conversa":
            conversas = await asyncio.to_thread(db.get_conversations, 30)
            alvo = fio.casar_conversa(conversas, nav.get("tema", ""))
            if alvo is None:
                fala = f"Não achei uma conversa sobre '{nav.get('tema', '')}'."
                await self._emitir_falado(send, fala)
                return fala, "mestre:nav_nao_achou"
            await send({"tipo": "navegar", "acao": "carregar_conversa", "id": alvo["id"]})
            fala = f"Abrindo a conversa sobre '{alvo['titulo'].strip().rstrip('?.!')}'."
            await self._emitir_falado(send, fala)
            return fala, "mestre:nav_carregar"
        await send({"tipo": "navegar", "acao": acao})
        fala = {
            "nova_conversa": "Pronto, comecei uma conversa nova.",
            "abrir_historico": "Abri o seu histórico de conversas.",
            "fechar_historico": "Fechei o histórico.",
        }.get(acao, "Feito.")
        await self._emitir_falado(send, fala)
        return fala, f"mestre:nav_{acao}"

    async def _retomar_fio(self, send: Sender, mem: SessionMemory) -> tuple:
        """#35: resgata o assunto de uma conversa ANTERIOR (a mais recente que não é a
        atual e teve substância) e oferece continuar. Depende do #34 (Malha): se o tema
        casa um conceito estabelecido do vault, enriquece a deixa com o que já se sabe."""
        conversas = await asyncio.to_thread(db.get_conversations, 10)
        escolhido = fio.escolher_fio(conversas, mem.conversa_id, settings.fio_min_turnos)
        if escolhido is None:
            fala = "Não achei uma conversa anterior pra retomar — acho que estamos começando agora."
            await self._emitir_falado(send, fala)
            return fala, "mestre:fio_vazio"
        titulo = escolhido["titulo"].strip().rstrip("?.!")
        # Toque da Malha (#34): um conceito relacionado ao tema, se o vault o conhece.
        relacionado = ""
        try:
            malha = self.ctx.vectorstore.malha
            chaves = textutils.palavras_chave(titulo)
            concs = [k for k in chaves if malha.idf_palavra(k) >= settings.aterramento_idf_min]
            if concs:
                relacionado = f" A gente pode puxar pelo lado de {concs[0]}, que você já tem anotado."
        except Exception:
            relacionado = ""
        fala = f"Da última vez a gente estava falando sobre '{titulo}'. Quer continuar daí?{relacionado}"
        await self._emitir_falado(send, fala)
        return fala, "mestre:fio"

    async def _reportar_perfil(self, send: Sender) -> tuple:
        """#36: fala o perfil de conversa aprendido (só lê o cache em ctx)."""
        perfil = self.ctx.perfil_conversa
        if not perfil:
            fala = "Ainda estou te conhecendo — não formei um perfil de como você gosta de conversar."
        else:
            fala = f"Pelo que percebi de como você gosta de conversar: {perfil}"
        await self._emitir_falado(send, fala)
        return fala, "mestre:perfil"

    # -- SRS (#43): repetição espaçada, manual + sob demanda --------------------
    async def _srs_marcar(self, send: Sender, mem: SessionMemory) -> tuple:
        """"mestre, revisa isso": cria um card da ÚLTIMA troca (pergunta->resposta). O card
        nasce VENCIDO (revisável já), então "mestre, revisão" o pega; o acerto o empurra."""
        if not mem.chat_history:
            fala = "Não há nada recente pra marcar pra revisão."
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_marca_vazia"
        pergunta, resposta = mem.chat_history[-1]
        await asyncio.to_thread(db.srs_criar_card, pergunta, resposta, datetime.now().isoformat())
        fala = "Guardei pra revisão. Diga 'mestre, revisão' quando quiser revisar."
        await self._emitir_falado(send, fala)
        return fala, "mestre:srs_marca"

    async def _srs_iniciar(self, send: Sender, mem: SessionMemory) -> tuple:
        """"mestre, revisão": puxa os cards vencidos e abre a sessão de revisão na RAM."""
        cards = await asyncio.to_thread(
            db.srs_vencidos, datetime.now().isoformat(), settings.srs_max_por_sessao
        )
        if not cards:
            fala = "Você não tem nada pra revisar agora. Bom trabalho!"
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_vazio"
        mem.revisao = {"pendentes": cards, "atual": None, "revelado": False}
        fala = self._srs_proximo_card(mem, abertura=f"Você tem {len(cards)} pra revisar. ")
        await self._emitir_falado(send, fala)
        return fala, "mestre:srs_inicio"

    def _srs_proximo_card(self, mem: SessionMemory, abertura: str = "") -> str:
        """Avança para o próximo card da fila (ou encerra). Puro em relação à fala."""
        rev = mem.revisao
        if not rev["pendentes"]:
            mem.revisao = None
            return abertura + "Revisão concluída. Até a próxima!"
        rev["atual"] = rev["pendentes"].pop(0)
        rev["revelado"] = False
        return abertura + (
            f"Lembra disto? {rev['atual']['frente']}. "
            "Diga 'mestre, mostra' pra ver a resposta, ou 'acertei' / 'errei'."
        )

    async def _srs_responder(self, comando: str, send: Sender, mem: SessionMemory) -> tuple:
        """Conduz uma revisão EM ANDAMENTO: mostra a resposta, ou registra acerto/erro
        (reagenda pela Leitner) e vai ao próximo card. Só chamado com mem.revisao ativa."""
        rev = mem.revisao
        if mestre.comando_srs_parar(comando):
            mem.revisao = None
            fala = "Ok, parei a revisão. Voltamos quando quiser."
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_parar"
        if mestre.comando_srs_mostrar(comando) and not rev["revelado"]:
            rev["revelado"] = True
            fala = f"{rev['atual']['verso']} — acertou? Diga 'acertei' ou 'errei'."
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_mostra"
        if mestre.comando_srs_acertei(comando) or mestre.comando_srs_errei(comando):
            acertou = mestre.comando_srs_acertei(comando)
            novo_estagio, dias = srs.proximo(
                rev["atual"].get("estagio", 0), acertou, settings.srs_intervalos_dias
            )
            proxima = (datetime.now() + timedelta(days=dias)).isoformat()
            await asyncio.to_thread(db.srs_reagendar, rev["atual"]["id"], novo_estagio, proxima)
            fala = self._srs_proximo_card(
                mem, abertura=("Boa! " if acertou else "Sem problema, ela volta em breve. ")
            )
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_resposta"
        # sub-comando não acionável agora (ex.: "mostra" com a resposta já revelada)
        fala = "Diga 'acertei' ou 'errei' pra esta, ou 'mestre, mostra' pra ver a resposta."
        await self._emitir_falado(send, fala)
        return fala, "mestre:srs_reprompt"

    async def _agenda_hoje(self, send: Sender, mem: SessionMemory) -> tuple:
        """#40: lê os .ics locais e fala os compromissos de HOJE. Só leitura, 100% local."""
        from datetime import date

        eventos = await asyncio.to_thread(calendario.ler_pasta, str(settings.dir_agenda), date.today())
        if not eventos:
            fala = "Você não tem compromissos na agenda para hoje."
        else:
            partes = [f"{dt.strftime('%H:%M')} {titulo}" for dt, titulo in eventos]
            fala = "Hoje você tem: " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:agenda"

    # -- Ajuda (/help falável): lista de capacidades ---------------------------
    async def _ajuda(self, send: Sender) -> tuple:
        """Fala um resumo das capacidades — o /help do assistente."""
        fala = (
            "Posso te ajudar de várias formas, sempre começando com 'mestre'. "
            "Conhecimento: eu respondo perguntas com as suas notas e a web, e faço a síntese "
            "de 'o que eu sei sobre um tema'. "
            "Listas e lembretes: 'adiciona pão na lista', 'me lembra de algo às 8 horas', "
            "'me avise quando tal coisa acontecer'. "
            "Agenda: 'o que tenho hoje' lê o seu calendário local. "
            "Estudo: 'revisa isso' e depois 'revisão' pra repetição espaçada; 'modo tutor' "
            "pra eu te ensinar fazendo perguntas. "
            "Produtividade: 'inicia um pomodoro', 'fiz treino' pra marcar hábitos, "
            "'resumo do dia' pro fechamento, e 'rotina manhã: comando e comando' pra criar macros. "
            "Automação: 'quando eu adicionar algo na lista, faça tal ação'. "
            "Conexões: 'alguma conexão nova' acha pontes entre os seus temas. "
            "E ainda: 'desfaça', 'corrige para', 'modo confidencial' e 'atalho' pra encurtar comandos. "
            "É só pedir."
        )
        await self._emitir_falado(send, fala)
        return fala, "mestre:ajuda"

    # -- Revisão diária (#21): fechamento do dia --------------------------------
    @staticmethod
    def _contar_inbox() -> int:
        """Itens pendentes na Inbox de captura (linhas com conteúdo, ignora cabeçalhos)."""
        caminho = str(settings.arquivo_inbox)
        if not os.path.exists(caminho):
            return 0
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                return sum(1 for l in f if l.strip() and not l.lstrip().startswith("#"))
        except Exception:
            return 0

    async def _revisao_diaria(self, send: Sender, mem: SessionMemory) -> tuple:
        """#21: fala um fechamento do dia — o que você fez (auditoria), a inbox a
        processar e o que vem amanhã (agenda .ics + lembretes). Só leitura/agregação."""
        from datetime import date

        hoje = date.today()
        amanha = hoje + timedelta(days=1)
        inicio_dia = datetime(hoje.year, hoje.month, hoje.day).isoformat()
        acoes = await asyncio.to_thread(db.get_auditoria, inicio_dia, 50)
        inbox_n = await asyncio.to_thread(self._contar_inbox)
        eventos = await asyncio.to_thread(calendario.ler_pasta, str(settings.dir_agenda), amanha)
        lembretes = await asyncio.to_thread(db.listar_agendamentos, ("lembrete",))
        amanha_iso = amanha.isoformat()
        lembretes_amanha = [l for l in lembretes if (l.get("proximo_disparo") or "").startswith(amanha_iso)]

        partes = [f"hoje você fez {len(acoes)} ação(ões) registrada(s)" if acoes
                  else "hoje você não registrou ações"]
        if inbox_n:
            partes.append(f"tem {inbox_n} item(ns) na inbox pra processar")
        if eventos:
            partes.append("amanhã na agenda: " + "; ".join(f"{dt.strftime('%H:%M')} {t}" for dt, t in eventos))
        if lembretes_amanha:
            partes.append("lembretes de amanhã: " + "; ".join(l["mensagem"] for l in lembretes_amanha))
        fala = "Fechamento do dia. " + ". ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:revisao_diaria"

    # -- Pomodoro (#19): ciclo foco/pausa via scheduler ------------------------
    async def _pomodoro_iniciar(self, send: Sender, mem: SessionMemory) -> tuple:
        """Cria o agendamento 'pomodoro' (1ª transição = fim do foco). O SchedulerService
        cuida do ciclo foco<->pausa. Só um ativo por vez: cancela outro antes."""
        ativos = await asyncio.to_thread(db.listar_agendamentos, ("pomodoro",))
        for a in ativos:
            await asyncio.to_thread(db.cancelar_agendamento, a["id"])
        prox = (datetime.now() + timedelta(minutes=settings.pomodoro_foco_min)).isoformat()
        await asyncio.to_thread(
            db.criar_agendamento, "pomodoro", "Pomodoro", prox, None,
            json.dumps({"fase": "foco"}), mem.conversa_id,
        )
        fala = (f"Pomodoro iniciado! Foco por {settings.pomodoro_foco_min} minutos — "
                "eu aviso quando for a pausa.")
        await self._emitir_falado(send, fala)
        return fala, "mestre:pomodoro_inicia"

    async def _pomodoro_parar(self, send: Sender) -> tuple:
        ativos = await asyncio.to_thread(db.listar_agendamentos, ("pomodoro",))
        for a in ativos:
            await asyncio.to_thread(db.cancelar_agendamento, a["id"])
        fala = "Pomodoro encerrado." if ativos else "Você não tem um pomodoro ativo."
        await self._emitir_falado(send, fala)
        return fala, "mestre:pomodoro_para"

    # -- Rotinas compostas (#10): macros nomeadas ------------------------------
    async def _criar_rotina(self, nome: str, comando: str, send: Sender) -> tuple:
        await asyncio.to_thread(db.rotina_salvar, nome, comando)
        fala = f"Rotina '{nome}' salva. Diga 'mestre, rotina {nome}' pra executar."
        await self._emitir_falado(send, fala)
        return fala, "mestre:rotina_criada"

    async def _rotinas_listar(self, send: Sender) -> tuple:
        rs = await asyncio.to_thread(db.rotinas_listar)
        if not rs:
            fala = "Você não tem rotinas salvas."
        else:
            fala = "Suas rotinas: " + "; ".join(f"{r['nome']} ({r['comando']})" for r in rs) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:rotinas_listar"

    async def _rotina_remover(self, nome: str, send: Sender) -> tuple:
        ok = await asyncio.to_thread(db.rotina_remover, nome)
        fala = f"Rotina '{nome}' removida." if ok else f"Não achei a rotina '{nome}'."
        await self._emitir_falado(send, fala)
        return fala, "mestre:rotina_removida"

    # -- Diário de hábitos (#37): fluxo independente ---------------------------
    async def _habito_marcar(self, nome: str, send: Sender, mem: SessionMemory) -> tuple:
        """Marca o hábito HOJE e fala a sequência (streak) atual."""
        from datetime import date

        hoje = date.today()
        await asyncio.to_thread(db.habito_marcar, nome, hoje.isoformat())
        datas_iso = await asyncio.to_thread(db.habito_datas, nome)
        datas = {datetime.fromisoformat(d).date() for d in datas_iso}
        seq = habitos.streak(datas, hoje)
        fala = f"Marquei '{nome}'. {seq} dia(s) seguido(s)!" if seq > 1 else f"Marquei '{nome}'. Começou a sequência!"
        await self._emitir_falado(send, fala)
        return fala, "mestre:habito_marca"

    async def _habitos_listar(self, send: Sender, mem: SessionMemory) -> tuple:
        from datetime import date

        hoje = date.today()
        nomes = await asyncio.to_thread(db.habitos_nomes)
        if not nomes:
            fala = "Você ainda não registrou nenhum hábito."
        else:
            partes = []
            for nome in nomes:
                datas_iso = await asyncio.to_thread(db.habito_datas, nome)
                datas = {datetime.fromisoformat(d).date() for d in datas_iso}
                partes.append(f"{nome}: {habitos.streak(datas, hoje)} dia(s)")
            fala = "Seus hábitos: " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:habitos_listar"

    # -- Gatilhos condicionais internos (#11) ----------------------------------
    async def _criar_gatilho(self, evento: str, filtro: str, acao_txt: str, send: Sender, mem: SessionMemory) -> tuple:
        """Cria a regra "quando <evento/filtro>, <ação>". A ação é parseada agora (rápido
        ou roteador LLM) e ARMAZENADA como JSON de Decisões, para reexecutar no disparo."""
        decisoes = mestre.parse_composto(acao_txt, datetime.now())
        if not decisoes:
            decisao = await self._rotear(acao_txt)
            if decisao and decisao.tool != "responder" and self.tools.get(decisao.tool):
                decisoes = [decisao]
        if not decisoes:
            fala = ("Entendi a condição, mas não a ação. Tente algo simples, como adicionar "
                    "a outra lista ou criar um lembrete.")
            await self._emitir_falado(send, fala)
            return fala, "mestre:gatilho_acao_invalida"
        acao_json = json.dumps([{"tool": d.tool, "args": d.args} for d in decisoes], ensure_ascii=False)
        descricao = f"quando adicionar '{filtro}' à lista → {mestre.descrever_acao(decisoes[0])}"
        await asyncio.to_thread(db.gatilho_criar, evento, filtro, acao_json, descricao)
        fala = f"Combinado: {descricao}."
        await self._emitir_falado(send, fala)
        return fala, "mestre:gatilho_criado"

    async def _disparar_gatilhos(self, evento: str, contexto: str, send: Sender) -> None:
        """Executa as regras que casam um EVENTO. As ações rodam DIRETO (não via
        _executar_acoes_rapidas), então NÃO re-emitem eventos — sem risco de loop."""
        gatilhos = await asyncio.to_thread(db.gatilhos_por_evento, evento)
        ctx_norm = textutils.normaliza(contexto)
        for g in gatilhos:
            filtro = (g.get("filtro") or "").strip()
            if filtro and textutils.normaliza(filtro) not in ctx_norm:
                continue
            try:
                decs = json.loads(g["acao"])
            except (ValueError, TypeError):
                continue
            for d in decs:
                tool = self.tools.get(d.get("tool"))
                if tool is None:
                    continue
                try:
                    obs = await tool.executar(d.get("args") or {}, self.ctx)
                except Exception as exc:
                    telemetry.error("GATILHO", f"Falha na ação do gatilho {g['id']}", exc)
                    continue
                await self._emitir_falado(send, f"Gatilho automático: {obs}")

    async def _gatilhos_listar(self, send: Sender) -> tuple:
        gs = await asyncio.to_thread(db.gatilhos_listar)
        if not gs:
            fala = "Você não tem gatilhos configurados."
        else:
            partes = [f"{g['id']}: {g['descricao']}" for g in gs]
            fala = "Seus gatilhos: " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:gatilhos_listar"

    async def _gatilho_remover(self, gatilho_id: int, send: Sender) -> tuple:
        ok = await asyncio.to_thread(db.gatilho_remover, gatilho_id)
        fala = f"Gatilho {gatilho_id} removido." if ok else f"Não achei o gatilho {gatilho_id}."
        await self._emitir_falado(send, fala)
        return fala, "mestre:gatilho_removido"

    def _acao_confirmavel(self, acoes: List["tools.Decisao"]) -> Optional["tools.Decisao"]:
        """A 1ª ação DESTRUTIVA que exige confirmação (#25), ou None. Respeita o botão
        `confirmacao_habilitada` — desligado, nada é gateado (executa direto)."""
        if not settings.confirmacao_habilitada:
            return None
        for dec in acoes:
            tool = self.tools.get(dec.tool)
            if tool is not None and tool.confirmavel:
                return dec
        return None

    async def _fluxo_mestre(
        self, comando: str, send: Sender, tracker: LatencyTracker, mem: SessionMemory
    ) -> None:
        """Fluxo ISOLADO da palavra-mestre: comando de agente, não pergunta.

        1) `parse_rapido` resolve os comandos regulares SEM LLM. 2) Senão, o roteador
        LLM tenta mapear numa ferramenta. 3) Se nem o roteador reconhece uma AÇÃO, o
        comando é RECUSADO (isolamento rígido, decidido com o dono) e REGISTRADO como
        melhoria a revisar — nunca vira uma resposta de conhecimento."""
        if not comando.strip():
            fala = f"Sim, {settings.palavra_mestre.capitalize()}? Pode falar."
            await self._emitir_falado(send, fala)
            return

        # ATALHO (#2): se o comando INTEIRO casa um apelido salvo, expande para o comando
        # original antes de qualquer coisa — daí segue o fluxo normal como se o usuário
        # tivesse dito o comando completo.
        atalhos = await self._atalhos_cache()
        expandido = atalhos.get(textutils.normaliza(comando))
        if expandido:
            telemetry.track("MESTRE", f"Atalho '{comando}' -> '{expandido}'.")
            comando = expandido

        # ROTINAS (#10): "rotina <nome>" (sem ':') EXPANDE para o comando composto salvo, e
        # o fluxo normal (parse_composto abaixo) executa os passos. Criar/listar/remover não
        # expandem (têm ':'/'=' ou verbo de remoção — ver parse_rotina_rodar).
        nome_rotina = mestre.parse_rotina_rodar(comando)
        if nome_rotina:
            salva = await asyncio.to_thread(db.rotina_get, nome_rotina)
            if salva:
                telemetry.track("MESTRE", f"Rotina '{nome_rotina}' -> '{salva}'.")
                comando = salva

        # COFRE DE CONFIRMAÇÃO (#25): se há uma ação destrutiva PENDENTE, "confirma" a
        # executa e "não/deixa" a aborta (abort tem precedência — na dúvida, não faz).
        # Qualquer OUTRO comando ABANDONA a pendência e segue normal (não prende o
        # usuário). Os gatilhos só valem com algo pendente (#15: sem gatilho global).
        pend = mem.confirmacao_pendente
        abortando = pend is not None and mestre.comando_abortar(comando)
        confirmando = pend is not None and not abortando and mestre.comando_confirmar(comando)
        if pend is not None and not abortando and not confirmando:
            mem.confirmacao_pendente = None   # comando novo supera a pendência

        # SRS (#43): um comando NÃO-relacionado durante uma revisão em andamento a abandona
        # (não prende o usuário) — mesmo espírito da pendência de confirmação acima. Os
        # sub-comandos (mostra/acertei/errei/parar) seguem para o handler de revisão abaixo.
        if mem.revisao is not None and not mestre.comando_srs_sub(comando):
            mem.revisao = None

        # GATILHOS (#11): "quando eu adicionar X na lista, <ação>". Só vira gatilho se a
        # condição casa um evento CONHECIDO; senão None e segue o fluxo normal (o watcher
        # "me avise quando ..." e o roteador continuam intactos).
        gatilho_spec = mestre.parse_gatilho(comando, datetime.now())
        # HÁBITOS (#37): nome do hábito a marcar ("fiz X" / "marca que ..."), ou None.
        habito_nome = mestre.parse_habito_marcar(comando)
        # ROTINAS (#10): criação "rotina <nome>: <cmd>" (a execução já foi expandida acima).
        rotina_nova = mestre.parse_rotina_criar(comando)

        # MODO CONFIDENCIAL (#5): meta-comando que mexe no estado da SESSÃO (por isso é
        # tratado aqui, não numa ferramenta). Liga/desliga e NÃO registra o próprio turno.
        modo = mestre.modo_confidencial(comando)
        if modo is not None:
            mem.confidencial = modo
            fala = (
                "Modo confidencial ativado. O que falarmos agora fica só nesta sessão — "
                "não salvo nada nem transformo em conhecimento."
                if modo else
                "Voltando ao normal. As conversas voltam a ser salvas e aprendidas."
            )
            await self._emitir_falado(send, fala)
            return

        # TUTOR SOCRÁTICO (#44): meta-comando de SESSÃO (como o confidencial). Liga/desliga
        # o modo em que as respostas viram perguntas guiadas (via verbosidade.aplicar_tutor).
        tutor = mestre.comando_tutor(comando)
        if tutor is not None:
            mem.tutor = tutor
            fala = (
                "Modo tutor ativado. Vou te fazer perguntas pra você raciocinar, em vez de "
                "dar a resposta pronta. Quando quiser parar, diga 'mestre, sai do tutor'."
                if tutor else
                "Modo tutor desligado. Volto a responder direto."
            )
            await self._emitir_falado(send, fala)
            return

        # MODO ECONÔMICO (#30): meta-comando de SESSÃO (como confidencial/tutor). Liga/
        # desliga o bypass do gate de relevância (responde local em vez de escalar web).
        economico = mestre.comando_economico(comando)
        if economico is not None:
            mem.economico = economico and settings.modo_economico_habilitada
            fala = (
                "Modo econômico ligado. Vou responder do que já tenho no vault sempre que "
                "der, evitando a web — mais rápido, porém menos preciso em temas que não domino."
                if mem.economico else
                "Modo econômico desligado. Volto a escalar pra web quando o local não bastar."
            )
            await self._emitir_falado(send, fala)
            return

        # REVISÃO EM ANDAMENTO (#43): se há uma revisão ativa, o comando aqui é um
        # sub-comando dela (mostra/acertei/errei/parar) — os não-relacionados já a
        # abandonaram acima. Tem prioridade sobre o resto.
        if mem.revisao is not None:
            texto_final, rota = await self._srs_responder(comando, send, mem)
        # CONFIRMAÇÃO PENDENTE (#25) tem prioridade sobre um comando novo.
        elif confirmando:
            dec = mem.confirmacao_pendente
            mem.confirmacao_pendente = None
            texto_final = await self._executar_acoes_rapidas(
                [dec], send, auditar=not mem.confidencial, mem=mem
            )
            rota = "mestre:confirmado"
        elif abortando:
            mem.confirmacao_pendente = None
            texto_final = "Ok, não vou fazer isso."
            await self._emitir_falado(send, texto_final)
            rota = "mestre:confirmacao_abortada"
        # ATALHO (#2): "mestre, atalho <nome>" nomeia o último comando resolvido.
        elif (nome_atalho := mestre.parse_atalho(comando)) is not None:
            texto_final, rota = await self._criar_atalho(nome_atalho, send, mem)
        # DESFAZER (#8): meta-comando que reverte a última ação reversível da sessão.
        # Vem antes do parse_rapido: "desfaça" não é uma ação nova a rotear.
        elif mestre.comando_desfazer(comando):
            texto_final, rota = await self._desfazer(send, mem, auditar=not mem.confidencial)
        elif mestre.comando_conexoes(comando):
            # DESCOBRIDOR DE CONEXÕES (G8): "mestre, alguma conexão nova?" — insight sob
            # demanda, não ação. Lê a malha em ctx (como o desfazer lê o estado da sessão).
            texto_final, rota = await self._descobrir_conexoes(send)
        elif mestre.comando_fonte(comando):
            # "FONTE?" (painel 2026-07): proveniência da última resposta, por TEMPLATE —
            # auditoria não paga decode na GPU serializada (nada de LLM aqui).
            texto_final, rota = await self._falar_fontes(send, mem)
        elif mestre.comando_contradicoes(comando):
            # DETECTOR DE CONTRADIÇÃO (#24): "mestre, alguma contradição?" — só lê a
            # tabela; a detecção rodou no idle. Insight sob demanda, não ação.
            texto_final, rota = await self._reportar_contradicoes(send)
        elif mestre.comando_perfil(comando):
            # DIAPASÃO (#36): "mestre, como você me vê?" — diz o perfil de estilo
            # aprendido no idle. Só lê o cache em ctx; insight sob demanda.
            texto_final, rota = await self._reportar_perfil(send)
        elif settings.navegacao_voz_habilitada and (nav := mestre.parse_navegacao(comando)):
            # NAVEGAÇÃO POR VOZ (#14): opera a UI (nova conversa, histórico, abrir uma
            # conversa por tema). ANTES do #35: "retoma a conversa SOBRE X" é o pedido
            # específico (abrir X) e vence o genérico "onde paramos" — parse_navegacao
            # só casa carregar quando há o tema, então "retoma o fio" ainda cai no #35.
            texto_final, rota = await self._navegar(nav, send, mem)
        elif mestre.comando_retomar_fio(comando):
            # FIO DA CONVERSA (#35): "mestre, onde paramos?" — resgata o assunto de
            # uma conversa anterior. Momento oportuno = o usuário pediu.
            texto_final, rota = await self._retomar_fio(send, mem)
        elif mestre.comando_revisao_diaria(comando):
            # #21: "resumo/fechamento do dia" — ANTES do SRS, pois "revisão do dia" conteria
            # "revisão" e cairia na revisão de cards.
            texto_final, rota = await self._revisao_diaria(send, mem)
        elif mestre.comando_srs_marcar(comando):
            # SRS (#43): "revisa isso" — ANTES do iniciar, pois a frase contém "revisa".
            texto_final, rota = await self._srs_marcar(send, mem)
        elif mestre.comando_srs_iniciar(comando):
            texto_final, rota = await self._srs_iniciar(send, mem)
        elif mestre.comando_agenda(comando):
            # #40: "o que tenho hoje" — leitura da agenda .ics local.
            texto_final, rota = await self._agenda_hoje(send, mem)
        elif mestre.comando_pomodoro_parar(comando):
            # #19: parar ANTES de iniciar (a frase de parar contém "pomodoro").
            texto_final, rota = await self._pomodoro_parar(send)
        elif mestre.comando_pomodoro_iniciar(comando):
            texto_final, rota = await self._pomodoro_iniciar(send, mem)
        elif gatilho_spec is not None:
            # #11: cria a regra "quando eu adicionar X na lista, <ação>".
            evento, filtro, acao_txt = gatilho_spec
            texto_final, rota = await self._criar_gatilho(evento, filtro, acao_txt, send, mem)
        elif (gid := mestre.comando_gatilho_remover(comando)) is not None:
            texto_final, rota = await self._gatilho_remover(gid, send)
        elif mestre.comando_gatilhos_listar(comando):
            texto_final, rota = await self._gatilhos_listar(send)
        elif rotina_nova is not None:
            # #10: cria a rotina "rotina <nome>: <comando composto>".
            texto_final, rota = await self._criar_rotina(rotina_nova[0], rotina_nova[1], send)
        elif (rot_rem := mestre.comando_rotina_remover(comando)) is not None:
            texto_final, rota = await self._rotina_remover(rot_rem, send)
        elif mestre.comando_rotinas_listar(comando):
            texto_final, rota = await self._rotinas_listar(send)
        elif mestre.comando_habitos_listar(comando):
            texto_final, rota = await self._habitos_listar(send, mem)
        elif habito_nome is not None:
            # #37: marca um hábito cumprido hoje.
            texto_final, rota = await self._habito_marcar(habito_nome, send, mem)
        elif mestre.tem_correcao(comando):
            # CORTA-E-CORRIGE (#9): "corrige para X" — antes do parse_rapido, pois um
            # "corrige ... na lista" tem gatilho de lista mas é correção, não add.
            texto_final, rota = await self._corrigir(comando, send, mem, auditar=not mem.confidencial)
        elif acoes := mestre.parse_composto(comando, datetime.now()):
            # #12 já pode ter devolvido VÁRIAS ações (comando encadeado "faz X e faz Y").
            # COFRE (#25): se alguma é destrutiva não-desfazível, executa as SEGURAS já e
            # deixa a destrutiva aguardando "confirma".
            confirmavel = self._acao_confirmavel(acoes)
            if confirmavel is not None:
                seguras = [a for a in acoes if a is not confirmavel]
                if seguras:
                    await self._executar_acoes_rapidas(
                        seguras, send, auditar=not mem.confidencial, mem=mem
                    )
                mem.confirmacao_pendente = confirmavel
                texto_final = f"Confirma que quer {mestre.descrever_acao(confirmavel)}? Diga 'mestre, confirma'."
                await self._emitir_falado(send, texto_final)
                rota = "mestre:aguarda_confirmacao"
            else:
                texto_final = await self._executar_acoes_rapidas(
                    acoes, send, auditar=not mem.confidencial, mem=mem
                )
                rota = "mestre:rapido"
        elif mestre.comando_ajuda(comando):
            # /help falável: descoberta de comandos. TARDE no fluxo (depois do parse_composto)
            # pra os comandos estruturados ganharem de um "me ajuda a adicionar na lista".
            texto_final, rota = await self._ajuda(send)
        else:
            decisao = await self._rotear(comando)
            if decisao and decisao.tool != "responder" and self.tools.get(decisao.tool):
                tool = self.tools.get(decisao.tool)
                if settings.confirmacao_habilitada and tool.confirmavel:
                    mem.confirmacao_pendente = decisao
                    texto_final = f"Confirma que quer {mestre.descrever_acao(decisao)}? Diga 'mestre, confirma'."
                    await self._emitir_falado(send, texto_final)
                    rota = "mestre:aguarda_confirmacao"
                else:
                    texto_final = await self._pipeline_tools(
                        comando, send, decisao, auditar=not mem.confidencial, mem=mem
                    )
                    rota = f"mestre:tool:{decisao.tool}"
            else:
                # Não é ação reconhecida: recusa + registra para revisão.
                await asyncio.to_thread(db.registrar_comando_desconhecido, comando)
                fala = (
                    "Não reconheci um comando de agente aí. Posso, por exemplo, adicionar "
                    "itens a uma lista, criar um lembrete ou avisar quando algo acontecer."
                )
                await self._emitir_falado(send, fala)
                texto_final = fala
                rota = "mestre:desconhecido"

        # ATALHO DE INTENÇÃO FREQUENTE (#2): só AÇÕES resolvidas contam como "intenção"
        # (meta-comandos como desfazer/confirmar/atalho não). Guarda o último comando p/
        # o "atalho <nome>" e, se a intenção virou hábito, OFERECE um atalho (uma vez).
        if rota == "mestre:rapido" or rota.startswith("mestre:tool:"):
            mem.ultimo_comando_mestre = comando
            if not mem.confidencial:
                sugestao = await self._talvez_sugerir_atalho(comando, send)
                if sugestao and texto_final:
                    texto_final = f"{texto_final} {sugestao}"   # anexa ao histórico

        # Comando de agente NÃO alimenta o dump (não é conhecimento). Segue no SQLite
        # (histórico) e na RAM (contexto de follow-up), como as demais ações — salvo
        # em modo confidencial, onde nada é persistido (só a RAM).
        if texto_final:
            mem.registrar_turno(comando, texto_final)
            if not mem.confidencial:
                await asyncio.to_thread(db.save_chat, comando, texto_final, mem.conversa_id)
            await self._registrar_latencia(tracker, rota)

    def _pergunta_com_contexto(self, texto_usuario: str, mem: SessionMemory) -> str:
        """Prefixa o histórico recente à pergunta, só para o LLM de RESPOSTA.

        A recuperação já resolve pronomes (QueryOptimizer), mas o gerador recebia o
        texto cru e ficava cego a follow-ups ("explique melhor", "e sobre isso") — daí
        respondia sentinela mesmo com os átomos certos. Damos 2 turnos recentes (resposta
        anterior truncada) e marcamos [PERGUNTA ATUAL] para manter o foco. Sem histórico,
        devolve a pergunta intacta (custo zero na 1ª pergunta da sessão)."""
        hist = mem.chat_history
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
        max_tokens: int | None = None,
        instrucao_extra: str = "",
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
        # Governador de verbosidade (#7): a pergunta define quanto a GPU decodifica e se
        # há instrução de brevidade. Sem nível (None) = comportamento de sempre.
        sistema = f"{system}\n{instrucao_extra}" if instrucao_extra else system
        async for token in self.ctx.llama.stream(
            prompt_fn(contexto, texto_usuario),
            max_tokens=max_tokens if max_tokens is not None else settings.max_tokens_resposta,
            system_prompt=sistema,
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

    async def _responder_web(
        self, termos: str, texto_usuario: str, send: Sender, mem: SessionMemory,
        consulta_rank: str | None = None, efemero: bool = False,
        nivel: "verbosidade.Nivel | None" = None,
    ) -> str:
        # Query da WEB: se a pergunta CITA uma expressão/ditado, busca a frase citada —
        # ela é o alvo, e o extrator de 5 palavras a descartava ('saiu expressão pega
        # prato' em vez de 'pega um prato faz a linha dá um tiro na farinha'). Senão, a
        # query enxuta de sempre.
        query_web = frase_citada(consulta_rank or texto_usuario) or termos

        # Filler específico mascara a latência da busca web (diz o que está fazendo).
        await self._falar_status(send, self._msg_web(query_web))

        # `query_web` faz o DDG; `consulta_rank` (pergunta natural crua) guia o ranking
        # dos trechos do deep-fetch — o embedding é simétrico, então a frase inteira
        # casa melhor com os parágrafos das páginas que 5 keywords.
        dados_web = await self.ctx.web.search(query_web, consulta=consulta_rank or termos)
        # Pre-fetch é "curiosidade": baixa contexto AMPLO do tema para virar átomo.
        # Não faz sentido nenhum sobre um dado que expira em horas — e era ele que
        # engordava o vault com dezenas de notas por pergunta sobre o tempo. Em modo
        # confidencial (#5) também não: a curiosidade viraria átomo permanente.
        if not efemero and not mem.confidencial:
            self.ctx.track_task(self._prefetch(termos, mem))  # background, web-only (ref. retida)

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

        # RAM SEMPRE: é memória de SESSÃO, morre com ela, e é o que faz o follow-up
        # ("e amanhã?") enxergar o dado. O que não pode é virar átomo permanente.
        mem.lembrar(termos, dados_web)
        if efemero:
            telemetry.track("ETL", f"'{termos}' é efêmero — fora da fila (não vira átomo).")
        elif mem.confidencial:
            telemetry.track("ETL", "Modo confidencial — resultado fora da fila (não vira átomo).")
        else:
            mem.enfileirar_etl(termos, dados_web)

        # A web VOLTOU dados, mas eles podem não responder (projeto privado, snippet
        # sem o número etc.). Passa pelo MESMO guard anti-sentinela do local: se o LLM
        # concluir insuficiência, NÃO fala o sentinela cru — devolve None e caímos num
        # retorno gracioso. Antes usava _responder_stream (sem guard) e o sentinela
        # "Não tenho informações suficientes" vazava falado para o usuário.
        resposta = await self._responder_contexto(
            dados_web, texto_usuario, send,
            prompt_fn=prompts.prompt_resposta_web, system=prompts.SYS_RESPOSTA_WEB,
            max_tokens=nivel.max_tokens if nivel else None,
            instrucao_extra=self._instrucao_com_perfil(nivel.instrucao if nivel else ""),
        )
        if resposta is not None:
            # Proveniência ("fonte?"): domínios que o deep-fetch desta busca abriu;
            # cache/snippets vêm sem domínio -> "web:" genérico (nunca domínio velho).
            dominios = list(getattr(self.ctx.web, "ultimos_dominios", []) or [])
            if dominios:
                mem.ultimas_fontes.extend(f"web:{d}" for d in dominios)
            else:
                mem.ultimas_fontes.append("web:")
            return resposta
        fala = "Procurei, mas não achei uma resposta clara sobre isso nas suas notas nem na web."
        await send({"tipo": "token", "texto": fala})
        audio = await self.ctx.tts.synth_base64(fala)
        if audio:
            await send({"tipo": "audio", "base64": audio})
        return fala

    async def _prefetch(self, tema: str, mem: SessionMemory) -> None:
        ctx_amplo = await self.ctx.web.prefetch(tema)
        if ctx_amplo:
            mem.lembrar(tema, ctx_amplo)
            # A "curiosidade" — contexto amplo que veio junto mas NÃO era necessário
            # falar agora — também é enfileirada pro ETL: vira átomos #conhecimento_novo
            # e engorda a base sobre o que o usuário demonstrou interesse.
            mem.enfileirar_etl(tema, ctx_amplo)

    async def _consolidar_fontes(self, fontes: List[str]) -> None:
        """Promoção: tira a tag #conhecimento_novo dos arquivos-fonte que foram usados
        numa resposta local. Best-effort e idempotente — só reescreve se a tag existir
        (evita bumps de mtime inúteis que disparariam reindexação à toa). O reindex do
        texto no Chroma acontece na próxima sync (fim de sessão); o arquivo no vault já
        reflete a mudança na hora, que é o que o usuário vê no Obsidian."""
        tag = prompts.TAG_NOVO
        promovidos = 0
        for src in fontes:
            try:
                def _promover(caminho=src) -> bool:
                    with open(caminho, "r", encoding="utf-8") as f:
                        conteudo = f.read()
                    if tag not in conteudo:
                        return False
                    novo = textutils.remover_tag(conteudo, tag)
                    with open(caminho, "w", encoding="utf-8") as f:
                        f.write(novo)
                    return True

                if await asyncio.to_thread(_promover):
                    promovidos += 1
                    # Métricas do ciclo (painel): a promoção também é evento persistido.
                    await asyncio.to_thread(db.log_etl, "PROMOCAO", src, "tag_removida")
            except OSError as exc:
                telemetry.warn("PROMOCAO", f"Não consegui consolidar {src}: {exc}")
        if promovidos:
            telemetry.track("PROMOCAO", f"{promovidos} nota(s) consolidada(s) (tirado {tag}).")

    async def _responder_stream(
        self, prompt_resposta: str, send: Sender, system: str = prompts.SYS_RESPOSTA
    ) -> str:
        chunker = SentenceChunker()
        texto_final = ""
        async for token in self.ctx.llama.stream(
            prompt_resposta,
            max_tokens=settings.max_tokens_resposta,
            system_prompt=system,
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

    @staticmethod
    def _lotes_por_chars(itens: List[str], budget: int) -> List[List[str]]:
        """Agrupa átomos em lotes cujo texto somado cabe em `budget` (protege o n_ctx)."""
        lotes: List[List[str]] = []
        atual: List[str] = []
        tam = 0
        for it in itens:
            if atual and tam + len(it) > budget:
                lotes.append(atual)
                atual, tam = [], 0
            atual.append(it)
            tam += len(it)
        if atual:
            lotes.append(atual)
        return lotes

    async def _sintese_sob_demanda(
        self, tema: str, send: Sender, mem: SessionMemory, tracker: LatencyTracker
    ) -> None:
        """Síntese sob Demanda (#23): "o que eu sei sobre X". Fluxo SEPARADO em map-reduce
        para não estourar o n_ctx: recupera MUITOS átomos, resume em lotes (map) e combina
        os parciais numa fala coerente (reduce). Só o banco local — é "o que EU sei"."""
        await self._falar_status(send, f"Deixa eu reunir o que você tem sobre {tema}.")
        atomos = await self.ctx.vectorstore.buscar_conteudos(tema, settings.sintese_top_k)

        if not atomos:
            texto_final = f"Não encontrei nada sobre {tema} nas suas notas."
            await self._emitir_falado(send, texto_final)
        else:
            lotes = self._lotes_por_chars(atomos, settings.sintese_lote_chars)
            parciais: List[str] = []
            for lote in lotes:
                p = await self.ctx.llama.collect(
                    prompts.prompt_sintese_tema_map(tema, "\n\n".join(lote)),
                    max_tokens=settings.max_tokens_sintese_tema,
                    system_prompt=prompts.SYS_SINTESE_TEMA,
                )
                if p.strip():
                    parciais.append(p.strip())
            telemetry.track("SINTESE", f"'{tema}': {len(atomos)} átomos em {len(lotes)} lote(s).")
            if not parciais:
                texto_final = f"Tenho notas sobre {tema}, mas não consegui resumir agora."
                await self._emitir_falado(send, texto_final)
            else:
                # REDUCE: combina os parciais numa fala (streaming). Um lote só também passa
                # pelo reduce — ele limpa/encurta o resumo bruto do map numa fala coerente.
                juntos = "\n\n".join(f"- {p}" for p in parciais)
                texto_final = await self._responder_stream(
                    prompts.prompt_sintese_tema_reduce(tema, juntos), send,
                    system=prompts.SYS_SINTESE_TEMA,
                )

        if texto_final:
            mem.registrar_turno(f"o que eu sei sobre {tema}", texto_final)
            if not mem.confidencial:
                await asyncio.to_thread(
                    db.save_chat, f"o que eu sei sobre {tema}", texto_final, mem.conversa_id
                )
        await self._registrar_latencia(tracker, "sintese")


# ==========================================================================
# ETL Post-Chat / Idle
# ==========================================================================
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
        await self._snapshot_base()

        if not settings.idle_descarregar_modelo:
            return
        if not self.ctx.interactive_idle.is_set():
            telemetry.track("ETL_POST_CHAT", "Interação retomada no idle — modelo mantido.")
            return
        await self.ctx.llama.unload()

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
