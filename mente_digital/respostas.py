"""
As RESPOSTAS faladas — a metade "responde" do Agent.

Mixin com os geradores que falam com o usuário: resposta aterrada no contexto
local (_responder_contexto, com o guard anti-sentinela que segura o áudio),
resposta da web com filler (_responder_web), o streaming token->frase->TTS
(_responder_stream), a Síntese sob Demanda em map-reduce (_sintese_sob_demanda)
e os coadjuvantes (prefetch especulativo, consolidação de fontes usadas —
a promoção do #conhecimento_novo —, mensagens de status).

Extraído VERBATIM do agent.py na modularização — mixin de propósito: os métodos
continuam sendo o MESMO objeto Agent em runtime. Nada aqui roda sem um Agent.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from mente_digital import figuras_recorte
from mente_digital import prompts
from mente_digital import textutils
from mente_digital import verbosidade
import re

from mente_digital.audio import SentenceChunker
from mente_digital.config import settings
from mente_digital.otimizador import frase_citada
from mente_digital.rag import NENHUM
from mente_digital.state import SessionMemory
from mente_digital.telemetry import LatencyTracker, db, telemetry

Sender = Callable[[dict], Awaitable[bool]]


class _SegurarFraseIncompleta:
    """Emissor de TEXTO em fronteiras de frase — o corte gracioso do teto de tokens.

    Por que existe (teste real 2026-07-21): quando o decode bate no max_tokens, o
    último pedaço é meia-frase ("...sendo recomendados em") e ia parar ESCRITO no
    chat e FALADO no flush do chunker. Aqui o texto só é emitido até a última
    fronteira de frase; a cauda fica retida e, no fim do stream, é emitida apenas
    se o decode terminou por conta própria — se bateu no teto, é descartada (e o
    chamador loga). O que é emitido é substring EXATA do stream (nada é reescrito),
    então tela, voz e histórico ficam consistentes. A VOZ continua token-a-token no
    SentenceChunker (preserva o 1º chunk agressivo da consultoria #8 / TTFA).
    """

    # Fronteira: pontuação final (aspas/parêntese opcionais) seguida de espaço.
    # Vírgula decimal e abreviações não casam (exigem o espaço após a pontuação).
    _FRONTEIRA = re.compile(r"[.!?…]+[\"')\]]?\s")
    _FINAL_COMPLETO = re.compile(r"[.!?…][\"')\]]?\s*$")

    def __init__(self) -> None:
        self._cauda = ""

    def push(self, texto: str) -> str:
        """Acumula e devolve o que já pode ser emitido (até a última fronteira)."""
        self._cauda += texto
        ultimo = None
        for m in self._FRONTEIRA.finditer(self._cauda):
            ultimo = m
        if ultimo is None:
            return ""
        corte = ultimo.end()
        pronto, self._cauda = self._cauda[:corte], self._cauda[corte:]
        return pronto

    def flush(self, truncado: bool):
        """Fim do stream -> (texto_a_emitir, texto_descartado). A cauda só é
        descartada se o decode foi TRUNCADO e ela não fecha frase sozinha."""
        cauda, self._cauda = self._cauda, ""
        if not truncado or not cauda.strip() or self._FINAL_COMPLETO.search(cauda):
            return cauda, ""
        return "", cauda


class Respostas:
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
        tracker: Optional[LatencyTracker] = None,
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
        visivel = _SegurarFraseIncompleta()
        texto_final = ""
        buffer = ""
        decidido = False
        n_tokens = 0
        teto = max_tokens if max_tokens is not None else settings.max_tokens_resposta
        # Governador de verbosidade (#7): a pergunta define quanto a GPU decodifica e se
        # há instrução de brevidade. Sem nível (None) = comportamento de sempre.
        sistema = f"{system}\n{instrucao_extra}" if instrucao_extra else system

        async def _emitir(pedaco: str) -> None:
            # TEXTO em fronteira de frase (corte gracioso do teto); VOZ token-a-token
            # no chunker (preserva o 1º chunk agressivo da consultoria #8 — TTFA igual).
            bloco = visivel.push(pedaco)
            if bloco:
                await send({"tipo": "token", "texto": bloco})
            frases = chunker.push(pedaco)
            if frases:
                await self._falar(send, frases, tracker)

        async for token in self.ctx.llama.stream(
            prompt_fn(contexto, texto_usuario),
            max_tokens=teto,
            system_prompt=sistema,
            tracker=tracker,
        ):
            texto_final += token
            n_tokens += 1
            if decidido:
                await _emitir(token)
                continue

            buffer += token
            norm = textutils.normaliza(buffer)
            # FUZZY (2026-07-21): o 2507 PARAFRASEIA o sentinela ("não há átomos que
            # confirmem...") e a checagem por frase exata deixava a dúvida ser FALADA
            # em vez de escalar. Agora o guard também escala nas variantes; e só retém
            # o stream em ABERTURA suspeita ("não...", "infelizmente...") dentro de uma
            # janela curta — resposta que abre de outro jeito libera no 1º token.
            if prompts.parece_sentinela(norm):
                return None                       # insuficiência (exata ou variante)
            if prompts.abre_como_sentinela(norm):
                continue                          # ainda pode ser sentinela -> segura
            decidido = True                       # abertura inocente ou janela vencida
            if prefixo:
                await send({"tipo": "token", "texto": prefixo})
            await _emitir(buffer)

        if not decidido:
            # O stream acabou com o guard em dúvida. Se o que sobrou NÃO parece
            # sentinela, é resposta real curta que abriu negando (ex.: "Não.") —
            # libera; nunca engolir fala legítima. (Corrige de quebra o caso antigo
            # em que "Não" seco, prefixo do sentinela, era descartado.)
            if buffer and not prompts.parece_sentinela(textutils.normaliza(buffer)):
                decidido = True
                if prefixo:
                    await send({"tipo": "token", "texto": prefixo})
                await _emitir(buffer)
            else:
                return None                       # só saiu (variante de) sentinela

        truncado = n_tokens >= teto
        resto_visivel, descartado = visivel.flush(truncado)
        if resto_visivel:
            await send({"tipo": "token", "texto": resto_visivel})
        if descartado:
            # Bateu no teto no meio de uma frase: melhor calar a meia-frase do que
            # mostrá-la/falá-la. O classificador de verbosidade evita a maioria
            # destes casos; isto é a rede de segurança.
            telemetry.track(
                "RESPOSTA",
                f"Teto de {teto} tokens: frase incompleta retida ({len(descartado)} chars).",
            )
            texto_final = texto_final[: len(texto_final) - len(descartado)].rstrip()
        resto = chunker.flush()
        if resto and not truncado:
            await self._falar(send, [resto], tracker)
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
        tracker: Optional[LatencyTracker] = None,
    ) -> str:
        # priv-01 (painel 2026-07-24): a promessa falada do sigilo é "fica só nesta
        # sessão" — mas a escalada mandava a pergunta pro DuckDuckGo e abria páginas
        # de terceiros. Choke point único (TODA escalada passa aqui): em sigilo, não
        # sai nada da máquina; a frase vira o "parágrafo" da vez (RAM segue coerente,
        # e a persistência já é bloqueada pelos guards de confidencial).
        if mem.confidencial and settings.sigilo_bloqueia_web:
            fala = (
                "Não achei isso localmente e, em modo sigiloso, eu não busco na web. "
                "Diga 'mestre, modo normal' se quiser que eu pesquise."
            )
            await self._emitir_falado(send, fala)
            return fala
        # Query da WEB: se a pergunta CITA uma expressão/ditado, busca a frase citada —
        # ela é o alvo, e o extrator de 5 palavras a descartava ('saiu expressão pega
        # prato' em vez de 'pega um prato faz a linha dá um tiro na farinha'). Senão, a
        # query enxuta de sempre.
        query_web = frase_citada(consulta_rank or texto_usuario) or termos

        # A BUSCA PARTE ANTES do filler ser sintetizado (consultoria TTFT #7): a síntese
        # Piper do filler (~0,1-0,3s) rodava EM SÉRIE com a rede parada — agora o DDG/
        # deep-fetch trabalha por baixo da fala. O filler continua saindo primeiro (é
        # rápido); o short-circuit do NENHUM abaixo não muda — só o await mudou de lugar.
        # `query_web` faz o DDG; `consulta_rank` (pergunta natural crua) guia o ranking
        # dos trechos do deep-fetch — o embedding é simétrico, então a frase inteira
        # casa melhor com os parágrafos das páginas que 5 keywords.
        # COLHEITA (web_colheita): os perdedores do race que terminarem durante a fala
        # viram #conhecimento_novo na fila do idle — de graça, sem nova busca. MESMOS
        # guards do pre-fetch: nada de turno efêmero/confidencial vira átomo permanente.
        # A dedup (URL já vista) e o lifecycle do client ficam no WebSearcher.
        on_colheita = None
        if not efemero and not mem.confidencial and settings.web_colheita_habilitada:
            def on_colheita(_url: str, texto: str) -> None:
                mem.enfileirar_etl(query_web, texto)
        busca = asyncio.ensure_future(
            self.ctx.web.search(
                query_web, consulta=consulta_rank or termos, on_colheita=on_colheita
            )
        )
        try:
            # FILLER CONTÍNUO: o deep-fetch leva 3-12s; uma ponte fixa de ~3s deixava
            # silêncio. Após a carência (abaixo), fala a 1ª ponte (o _msg_web dinâmico, que
            # nomeia a query) e, ENQUANTO a busca não volta, emite pontes curtas adicionais
            # a cada filler_intervalo_s até a busca terminar OU bater filler_max_pontes — o
            # filler dura ~o tempo real do fetch. Não fala ponte inútil se a busca já
            # voltou (o break abaixo). Barge-in corta: cada await é ponto de cancelamento
            # e o except cancela a busca antes de propagar a CancelledError.
            # CARÊNCIA (correção pós-race): desde o race-first-K a web volta em ~3s (às
            # vezes <1,5s). A 1ª ponte falada NA HORA passou a atropelar a resposta —
            # "vou buscar..." e logo em cima o dado real. Antes de falar QUALQUER ponte,
            # dá esta carência de silêncio à busca; se ela terminar na janela, PULA o
            # filler inteiro (sem 1ª ponte, sem loop) e vai direto à resposta. Mesmo
            # shield de sempre (protege a busca do cancel do wait_for no timeout); o
            # barge-in propaga a CancelledError ao except externo, que cancela a busca.
            if settings.filler_carencia_s > 0:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(busca), timeout=settings.filler_carencia_s
                    )
                except asyncio.TimeoutError:
                    pass
            if not busca.done():
                # A busca furou a carência — agora vale mascarar a espera com filler.
                await self._falar_status(send, self._msg_web(query_web))
                pontes = 0
                while not busca.done() and pontes < settings.filler_max_pontes:
                    try:
                        # Espera o intervalo OU a busca terminar (o que vier antes). O shield
                        # protege a busca do cancelamento que o wait_for faz ao dar timeout.
                        await asyncio.wait_for(
                            asyncio.shield(busca), timeout=settings.filler_intervalo_s
                        )
                    except asyncio.TimeoutError:
                        pass
                    if busca.done():
                        break               # busca voltou dentro do intervalo -> sem ponte
                    await self._falar_status(send, prompts.ponte_continuacao(pontes))
                    pontes += 1
            dados_web = await busca
        except BaseException:
            busca.cancel()   # barge-in/erro no filler: a busca não fica órfã viva
            raise
        # Pre-fetch é "curiosidade": baixa contexto AMPLO do tema para virar átomo.
        # Não faz sentido nenhum sobre um dado que expira em horas — e era ele que
        # engordava o vault com dezenas de notas por pergunta sobre o tempo. Em modo
        # confidencial (#5) também não: a curiosidade viraria átomo permanente.
        # FORA DO HOT-PATH: é disparado em BACKGROUND (track_task, ref. forte retida) e
        # NUNCA é awaited no caminho da resposta — a fala do usuário nunca espera pelo
        # pre-fetch. Ele roda concorrente ao decode da resposta (web-only, sem GPU), então
        # não soma ao TTFA/TTFT deste turno; o resultado só serve à PRÓXIMA pergunta.
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
            tracker=tracker,
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

    async def _mostrar_figuras(self, send: Sender, fontes: List[str],
                               ja_dito: str = "") -> int:
        """Anexa à resposta as figuras que entraram no contexto. Devolve quantas.

        Por que o SERVIDOR monta isto, em vez de pedir ao LLM que copie o wikilink:
        medido no teste real de 2026-07-26, a pergunta caiu no nível `curto`
        (teto de 90 tokens), a resposta consumiu o teto INTEIRO e não sobrou espaço
        para um embed de ~105 chars — nenhuma imagem apareceu. Somam-se dois riscos
        que este caminho elimina de vez: o system prompt manda ser "BRUTALMENTE
        CONCISO" (empurra contra), e um caminho longo transcrito por um modelo local
        pode sair com um caractere trocado, quebrando a <img> em silêncio.

        Vai pelo canal VISÍVEL (`tipo: token`), nunca pelo chunker — então o TTS não
        vê nada disso e a fala continua sendo só a resposta.

        Confere o arquivo em disco antes: a nota pode seguir indexada depois de o
        .webp ter sido apagado (aconteceu com a p4 do Cervantes), e imagem quebrada
        na tela é pior do que figura ausente."""
        # `ja_dito` é a resposta que o modelo produziu: se ele copiou o embed mesmo
        # assim (o corpo da nota o contém, então há risco de imitação), não anexa de
        # novo — a duplicata mostraria a mesma imagem duas vezes.
        embeds = [
            e for e in
            figuras_recorte.bloco_de_figuras(fontes, settings.subpasta_figuras).split("\n")
            if e and e not in ja_dito
        ]
        if not embeds:
            return 0
        raiz = Path(settings.caminho_obsidian)

        def _existentes() -> List[str]:
            return [e for e in embeds if (raiz / e[3:-2]).is_file()]

        vivos = await asyncio.to_thread(_existentes)
        if not vivos:
            return 0
        await send({"tipo": "token", "texto": "\n\n" + "\n".join(vivos)})
        telemetry.track("FIGURAS", f"{len(vivos)} figura(s) anexada(s) à resposta.")
        return len(vivos)

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
        self, prompt_resposta: str, send: Sender, system: str = prompts.SYS_RESPOSTA,
        tracker: Optional[LatencyTracker] = None,
    ) -> str:
        chunker = SentenceChunker()
        visivel = _SegurarFraseIncompleta()
        texto_final = ""
        n_tokens = 0
        async for token in self.ctx.llama.stream(
            prompt_resposta,
            max_tokens=settings.max_tokens_resposta,
            system_prompt=system,
            tracker=tracker,
        ):
            texto_final += token
            n_tokens += 1
            bloco = visivel.push(token)
            if bloco:
                await send({"tipo": "token", "texto": bloco})
            frases = chunker.push(token)
            if frases:
                await self._falar(send, frases, tracker)
        truncado = n_tokens >= settings.max_tokens_resposta
        resto_visivel, descartado = visivel.flush(truncado)
        if resto_visivel:
            await send({"tipo": "token", "texto": resto_visivel})
        if descartado:
            telemetry.track(
                "RESPOSTA",
                f"Teto de {settings.max_tokens_resposta} tokens: frase incompleta retida "
                f"({len(descartado)} chars).",
            )
            texto_final = texto_final[: len(texto_final) - len(descartado)].rstrip()
        resto = chunker.flush()
        if resto and not truncado:
            await self._falar(send, [resto], tracker)
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
                    system=prompts.SYS_SINTESE_TEMA, tracker=tracker,
                )

        if texto_final:
            mem.registrar_turno(f"o que eu sei sobre {tema}", texto_final)
            if not mem.confidencial:
                await asyncio.to_thread(
                    db.save_chat, f"o que eu sei sobre {tema}", texto_final, mem.conversa_id
                )
        await self._registrar_latencia(tracker, "sintese")
