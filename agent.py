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
from comandos_mestre import ComandosMestre
from config import settings
# O ETL idle (EtlProcessor) e o dump da conversa (a fila que ele consome) moram em
# etl.py; re-exportados por aqui (main.py, ws.py e testes importam de `agent`).
from etl import EtlProcessor, append_chat_dump
from llm import InferenciaPreemptada, LlamaManager
# A interpretação da pergunta (QueryOptimizer + heurísticas puras: referência ao
# turno anterior, tema de síntese, frase citada, lacuna pesquisável) mora em
# otimizador.py; re-exportada por aqui pelos mesmos motivos de atomos.py.
from otimizador import (
    QueryOptimizer,
    e_declarativa,
    extrair_tema_sintese,
    frase_citada,
    lacuna_pesquisavel,
    referencia_contexto,
)
# O sentinela mudou-se para prompts.py (é camada de linguagem); eval/ importa daqui.
from prompts import SENTINELA_INSUF
from rag import NENHUM, LocalResult, strip_frontmatter
from respostas import Respostas
from state import AppContext, SessionMemory
# LatencyTracker mudou-se para telemetry.py (instrumentação mora com o save_latency).
from telemetry import LatencyTracker, db, telemetry

Sender = Callable[[dict], Awaitable[bool]]
# "Sem banco local" — usado no estágio RAM para reaproveitar _montar_contexto sem
# tocar no vetor (o builder ignora o local quando o texto é NENHUM).
NENHUM_LOCAL = LocalResult(NENHUM, None, False)


# ==========================================================================
# Pipeline principal
# ==========================================================================
# Os mixins carregam as duas metades grandes: ComandosMestre ("age" — o fluxo da
# palavra-mestre e todos os executores) e Respostas ("responde" — os geradores
# falados). Em runtime é o MESMO objeto Agent de sempre; só os arquivos mudaram.
class Agent(ComandosMestre, Respostas):
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
        stt_ms: Optional[int] = None, vad_ms: Optional[int] = None,
    ) -> None:
        # Instrumenta o timing por estágio: cada msg passa pelo tracker (F4).
        tracker = LatencyTracker()
        tracker.stt_ms = stt_ms   # transcrição (voz) medida no ws.py, antes daqui
        tracker.vad_ms = vad_ms   # janela do endpoint aplicada (ws) — waterfall (#1)

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
            # VETO DECLARATIVO (#33): uma AFIRMAÇÃO nunca é pedido de ferramenta, ainda
            # que trombe num gatilho de ação. "Vou viajar pra Salvador na sexta" disparava
            # talvez_acao ('salva' é substring de 'Salvador') e o roteador, vendo 'sexta',
            # tentava criar_lembrete — a frase não virava memória. Comando explícito ('me
            # lembra', 'adiciona', 'salva nota') começa por imperativo → e_declarativa=False
            # → segue roteando; só a afirmação é poupada e cai no pipeline como memória.
            if tools.talvez_acao(texto_usuario) and not e_declarativa(texto_usuario):
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

            # extrator_ms = wall-clock da interpretação INTEIRA; com a fase (b), a
            # recuperação vetorial corre DENTRO desta janela — e o busca_ms do estágio
            # Banco desaba, que é o overlap aparecendo no waterfall.
            _t_extrator = time.perf_counter()
            termos, recuperados = await self._otimizar_e_recuperar(texto_usuario, mem)
            tracker.extrator_ms = round((time.perf_counter() - _t_extrator) * 1000)
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
                    _t_busca = time.perf_counter()
                    texto_busca = await self._texto_busca(texto_usuario, termos)
                    # `recuperados` só entra quando a fase (b) especulou — assim os
                    # fakes/stores antigos (sem o kwarg) seguem funcionando intactos.
                    _extra = {"recuperados": recuperados} if recuperados is not None else {}
                    local = await self.ctx.vectorstore.search(
                        termos, texto_busca=texto_busca, economico=mem.economico, **_extra
                    )
                    tracker.busca_ms = round((time.perf_counter() - _t_busca) * 1000)
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
                if not paragrafos and e_declarativa(texto_usuario):
                    # DECLARATIVA (caso "Falcão", 2026-07-21): o usuário AFIRMOU um fato
                    # e o pipeline o tratava como pergunta — escalava pra web, achava um
                    # homônimo (o drone da Avibras) e o fato ALHEIO contaminava a RAM e
                    # virava átomo permanente. Afirmação sem âncora local é REGISTRO:
                    # reconhece, guarda na RAM da sessão (o follow-up enxerga) e deixa o
                    # dump/ETL atomizar a frase DO USUÁRIO. Web e lacuna ficam fora.
                    telemetry.track(
                        "AGENT", f"Declarativa sem âncora local — registro sem web: '{termos}'."
                    )
                    mem.lembrar(termos, texto_usuario)
                    fala = "Entendido, registrei."
                    await self._falar_status(send_medido, fala)
                    paragrafos.append(fala)
                    fontes.append("registro")
                elif not paragrafos:
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

    async def _otimizar_e_recuperar(
        self, texto_usuario: str, mem: SessionMemory
    ) -> Tuple[str, Optional[list]]:
        """FASE (b) do QueryOptimizer (consultoria TTFT #9): otimização e recuperação
        vetorial em PARALELO quando o extrator vai pagar um decode LLM.

        Por que é seguro: o texto embeddado pela busca é a pergunta CRUA (_texto_busca
        usa texto_usuario; os `termos` do extrator só alimentam o ATERRAMENTO, que roda
        depois, dentro do search) — então o resultado da especulação é idêntico ao da
        busca serial, só chega mais cedo. Cancelamento: barge-in cancela o pipeline →
        o except abaixo cancela a task especulativa (o to_thread do embedding termina
        órfão, mas é leitura pura, sem efeito colateral).

        Fica de fora (fail-open, devolve recuperados=None → busca serial de sempre):
        botão desligado; HyDE (também chama o LLM — a GPU é serializada, não haveria
        paralelismo); rota time-sensitive (vai direto pra web, nem consulta o Banco);
        e optimizer/store sem os métodos novos (fakes antigos dos testes)."""
        pagaria = getattr(self.optimizer, "pagaria_llm", None)
        recuperar = getattr(self.ctx.vectorstore, "recuperar", None)
        overlap = (
            settings.optimizer_overlap
            and not settings.rag_hyde
            and callable(pagaria) and callable(recuperar)
            and not tools.talvez_tempo_real(texto_usuario)
            and pagaria(texto_usuario, mem.chat_history)
        )
        if not overlap:
            return await self.optimizer.optimize(texto_usuario, mem.chat_history), None
        tarefa = asyncio.ensure_future(recuperar(texto_usuario))
        try:
            termos = await self.optimizer.optimize(texto_usuario, mem.chat_history)
            recuperados = await tarefa
        except BaseException:
            tarefa.cancel()          # barge-in/erro: a especulação não fica órfã viva
            raise
        if recuperados is not None:
            telemetry.track("EXTRATOR", "Fase (b): recuperação correu em paralelo com o LLM.")
        return termos, recuperados

    async def _registrar_latencia(self, tracker: LatencyTracker, rota: str) -> None:
        ttft, ttfa, total = (
            LatencyTracker._ms(tracker.ttft),
            LatencyTracker._ms(tracker.ttfa),
            LatencyTracker._ms(tracker.total()),
        )
        toks = tracker.decode_tok_s()
        telemetry.track(
            "LATENCIA",
            f"rota={rota} vad={tracker.vad_ms}ms stt={tracker.stt_ms}ms "
            f"extrator={tracker.extrator_ms}ms busca={tracker.busca_ms}ms "
            f"TTFT={ttft}ms tok/s={toks} TTFA={ttfa}ms total={total}ms n_tok={tracker.n_tokens}",
        )
        await asyncio.to_thread(
            db.save_latency, rota, ttft, ttfa, total, tracker.stt_ms, toks, tracker.n_tokens,
            vad_ms=tracker.vad_ms, extrator_ms=tracker.extrator_ms, busca_ms=tracker.busca_ms,
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
