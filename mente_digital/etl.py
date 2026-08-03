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
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Tuple

from mente_digital import contradicao
from mente_digital import diapasao
from mente_digital import identidade
from mente_digital import obras
from mente_digital import prompts
from mente_digital import textutils
from mente_digital import academico
from mente_digital import antiinjecao
from mente_digital import consolidacao
from mente_digital import figuras as figuras_mod
from mente_digital import imagem_web
from mente_digital import rag
from mente_digital import livro as livro_mod
from mente_digital import ocr as ocr_mod
from mente_digital import triagem
from mente_digital.atomos import _slug_titulo, dividir_atomos, normalizar_atomo
from mente_digital.config import Settings, settings
from mente_digital.llm import InferenciaPreemptada
from mente_digital.otimizador import lacuna_pesquisavel
from mente_digital.rag import NENHUM, multiusuario_ligado, strip_frontmatter
from mente_digital.state import AppContext
from mente_digital.telemetry import db, telemetry

# Leitura da nota de acervo web (ver `EtlProcessor.revalidar_acervo_web`). O prefixo vem
# de `imagem_web.ORIGEM_PREFIXO` e não é recopiado aqui: é CONTRATO de texto com quem
# escreve a nota, e duas cópias divergem no primeiro dia em que alguém mudar uma delas.
_TERMO_DO_ACERVO = re.compile(re.escape(imagem_web.ORIGEM_PREFIXO) + r"\s*'([^']+)'")
_TITULO_H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
_CAMPO_CONFERE = re.compile(r"^acervo_confere: (\w+)\s*$", re.M)
# Linhas do corpo DERIVADAS do próprio termo buscado. Precisam sair antes de julgar: a
# de crédito repete o termo em prosa e a Malha o repete como wikilink, então mantê-las
# faz o termo casar consigo mesmo. Medido: com o corpo inteiro a revalidação pegou 0 das
# 4 imagens erradas; subtraindo estas duas, pega 4.
_DERIVADAS_DO_TERMO = ("**Malha Neural:**", imagem_web.CREDITO_PREFIXO)


def _evidencia_independente(texto: str) -> str:
    """O que a nota diz que NÃO foi copiado do termo buscado. Puro/testável.

    Sobra o que é evidência de verdade: a descrição do VLM, quando existe, e qualquer
    texto que a página tenha trazido. Subtrai em vez de listar o que serve porque o
    formato da nota ainda muda — linha nova entra como evidência por padrão, e o
    esquecimento erra para o lado seguro (aceitar), não para o de condenar nota boa.
    """
    return "\n".join(
        ln for ln in strip_frontmatter(texto).splitlines()
        if not any(d in ln for d in _DERIVADAS_DO_TERMO)
    )


# ==========================================================================
# Em QUE pasta este ETL escreve — e por conta de QUEM ele roda
# ==========================================================================
# A fronteira de privacidade é a PASTA/COLEÇÃO (ver `multiusuario_ligado` em rag.py),
# então quem grava tem de ESCOLHER onde. Todo caminho de escrita deste módulo passa
# pelas duas funções abaixo; nenhuma rotina monta pasta de vault por conta própria.
#
# ⚠ O idle é o lugar onde isso é mais fácil de errar, porque ele roda SEM NINGUÉM
# OLHANDO: o `SchedulerService` chama `pesquisa_proativa`/`pesquisa_temas_quentes`/
# `consolidar_atomos` a partir do RELÓGIO, sem sessão e portanto sem dono no contexto.
# Antes do multiusuário isso era irrelevante (havia um vault só); agora, um átomo
# colhido "de ninguém" ou vai parar na pasta errada ou não é escrito. Por isso as
# rotinas de idle rodam POR DONO (`_por_dono`), e não uma vez para a máquina.
#
# ⚠ TODAS RECEBEM O `Settings` POR PARÂMETRO, e isso não é preciosismo de estilo. A 1ª
# versão lia o `settings` de MÓDULO, e o `EtlProcessor` recebe o dele por INJEÇÃO
# (`ctx.settings`): as duas coisas normalmente são o mesmo objeto, mas em teste não são
# — o `test_preempcao` aponta `ctx.settings` para um `tmp_path` e o código escrevia no
# vault GLOBAL. Custou duas notas de fixture despejadas no `Conhecimento_Novo` REAL do
# dono, que o próximo `sync` teria indexado como conhecimento de verdade. Injetar o
# `Settings` é o que torna essa divergência impossível de acontecer em silêncio.
def raiz_dos_atomos(st: Settings, acervo: bool = False) -> Path:
    """A pasta onde um átomo colhido AGORA é gravado. Ponto único da decisão.

    Um usuário só (o default): `Conhecimento_Novo/` na raiz do vault — byte a byte
    o de hoje. Vários: o átomo colhido de conversa/web/pre-fetch é do DONO daquele
    turno (`Pessoal/<dono>/Conhecimento_Novo/`), e só a ingestão de OBRA vai para o
    acervo comum — ela é ato do dono da máquina, rodada offline, e o resultado é
    biblioteca, não memória de alguém.

    Sem dono no contexto e fora do acervo isto FALHA (`exigir_dono`). É deliberado:
    herdar "o último dono" escreveria a memória de uma pessoa na pasta de outra, e
    isso não tem desfazer."""
    if not multiusuario_ligado():
        return Path(st.dir_conhecimento_novo)
    raiz = (st.caminho_acervo if acervo
            else st.caminho_pessoal(identidade.exigir_dono()))
    return raiz / st.subpasta_conhecimento_novo


def caminho_chat_dump(st: Optional[Settings] = None) -> str:
    """O dump bruto da conversa DESTE dono.

    Com quatro pessoas, um arquivo único misturaria as conversas e o `summarize_dump`
    atomizaria a conversa de A dentro do vault de B — o vazamento seria PERMANENTE
    (vira nota no Zettelkasten). O nome do arquivo deriva do de sempre, então com o
    multiusuário desligado o caminho é idêntico ao de hoje e nada precisa migrar.

    `st` é opcional aqui, e só aqui, porque `append_chat_dump` é uma função de MÓDULO
    chamada de `agent`/`ws` sem `ctx` à mão — é o caminho que já lia o global antes."""
    base = Path((st or settings).arquivo_chat_dump)
    if not multiusuario_ligado():
        return str(base)
    dono = identidade.exigir_dono()
    return str(base.with_name(f"{base.stem}_{dono}{base.suffix}"))


def _donos_com_dump(st: Settings) -> List[str]:
    """Quem tem conversa esperando atomização. Complementa as pastas de `Pessoal/`:
    um usuário NOVO conversa antes de ter pasta pessoal (ela nasce na 1ª escrita), e
    sem isto a primeira conversa dele ficaria no disco para sempre, nunca atomizada."""
    base = Path(st.arquivo_chat_dump)
    prefixo = f"{base.stem}_"
    donos: List[str] = []
    try:
        achados = list(base.parent.glob(f"{prefixo}*{base.suffix}"))
    except OSError:
        return []
    for p in achados:
        nome = p.name[len(prefixo): -len(base.suffix)] if base.suffix else p.name[len(prefixo):]
        if identidade.valido(nome):
            donos.append(identidade.normalizar(nome))
    return donos


def _donos_do_vault(st: Settings) -> List[str]:
    """As pastas de `Pessoal/` que são nome de dono válido.

    Mesma régua do `VectorStore._escopos_de_indexacao`: a PASTA é a fronteira, então
    é ela também quem sabe quem existe — não um campo de config que alguém esqueceria
    de atualizar ao criar o quarto usuário. Nome que `identidade.normalizar` recusa
    não vira dono: adivinhar aqui seria o servidor decidindo de quem é a memória."""
    raiz = Path(st.caminho_obsidian) / st.subpasta_pessoal
    try:
        nomes = [e.name for e in os.scandir(raiz) if e.is_dir()]
    except OSError:
        return []       # pasta ainda não criada: não há vault pessoal nenhum
    return [n for n in nomes if identidade.valido(n) and identidade.normalizar(n) == n]


def donos_do_ciclo(st: Optional[Settings] = None) -> List[str]:
    """Por conta de quem o idle roda nesta passada: quem tem vault pessoal OU
    conversa pendente. Ordenado (passada reprodutível) e sem repetição.

    Nunca devolve lista vazia: com o multiusuário recém-ligado e nada migrado ainda,
    cai no `DONO_PADRAO` — o dono das 14.492 notas que já existem. O pior desfecho
    aqui seria o idle não rodar para NINGUÉM e a base parar de crescer em silêncio,
    que é exatamente a família de falha que este projeto já pagou (`..._SEGUNDOS`)."""
    st = st or settings
    return sorted(set(_donos_do_vault(st)) | set(_donos_com_dump(st))) \
        or [identidade.DONO_PADRAO]


async def append_chat_dump(ator: str, texto: str) -> None:
    """Grava o dump bruto da conversa (Obsidian). IO em thread."""
    caminho = caminho_chat_dump()

    def _write() -> None:
        with open(caminho, "a", encoding="utf-8") as f:
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

    async def _por_dono(self, tarefa: Callable[[], Awaitable[None]]) -> None:
        """Roda `tarefa` uma vez POR DONO, cada passada sob o contexto dele.

        É o que torna o idle correto SEM depender de quem o disparou: o scheduler o
        chama a partir do relógio (sem sessão, sem dono) e a sessão o chama no fim da
        conversa (com dono). Antes, essas duas origens levariam a resultados
        diferentes — uma escreveria a memória de todos na pasta de uma pessoa, a
        outra falharia por falta de dono.

        Com o multiusuário DESLIGADO é uma chamada direta, no contexto que chegou:
        nem `usar_dono` entra em cena, e o comportamento é byte a byte o de hoje.

        A falha de um dono não pode matar a passada dos outros — um vault pessoal
        corrompido deixaria os outros três sem idle para sempre. Por isso o `except`
        é POR DONO, e vai alto no log (nada engolido)."""
        if not multiusuario_ligado():
            await tarefa()
            return
        for dono in await asyncio.to_thread(donos_do_ciclo, self.ctx.settings):
            with identidade.usar_dono(dono):
                try:
                    await tarefa()
                except Exception as exc:
                    telemetry.error("IDLE", f"Passada de idle de '{dono}' falhou", exc)

    def _max_fundo(self, base: int) -> int:
        """#29: aplica o orçamento de tokens de fundo (calibrado pela VRAM livre pelo
        scheduler). Sem leitura de VRAM (orcamento_fundo None), usa o `base` de sempre."""
        cap = getattr(self.ctx, "orcamento_fundo", None)
        return min(base, cap) if cap else base

    async def _salvar_atomos(self, texto: str, prefixo: str, tipo_log: str,
                             origem: Optional[str] = None, subpasta: str = "",
                             acervo: bool = False) -> int:
        """Salva UM ARQUIVO POR ÁTOMO (Zettelkasten puro). Assim a promoção fica
        precisa por ideia: só o átomo realmente reusado perde o #conhecimento_novo,
        não os vizinhos que calharam de estar no mesmo documento. Devolve quantos salvou.

        `acervo=True` manda o átomo para a biblioteca COMUM em vez do vault pessoal do
        dono do turno — só a ingestão de obra usa isso (ver `raiz_dos_atomos`).

        Todo bloco passa por `normalizar_atomo` ANTES de virar arquivo: o formato do
        átomo é imposto no código, não confiado ao LLM (ver a docstring de lá — o A/B
        mostrou que nenhum modelo o entrega de forma confiável, e sem as tags a
        promoção nunca acontece).

        Fallback: se o LLM não usou nenhum '##' (formato quebrado), salva o texto inteiro
        como 1 átomo em vez de descartar o conhecimento em silêncio."""
        blocos = dividir_atomos(texto)
        if not blocos and texto.strip():
            blocos = [texto.strip()]
        # A pasta é resolvida UMA vez, ANTES do laço: sem dono no contexto isto levanta
        # `DonoIndefinido`, e é melhor que aconteça antes de gravar o primeiro átomo do
        # que no meio — meia atomização salva é o pior dos dois desfechos possíveis.
        raiz = str(raiz_dos_atomos(self.ctx.settings, acervo))
        agora = datetime.now()
        salvos = 0
        duplicados = 0
        substituidos = 0        # átomo antigo aposentado por uma edição preferida
        salvos_info: List[Tuple[str, str]] = []  # (caminho, corpo sem frontmatter) p/ #24
        for i, bloco in enumerate(blocos):
            # `origem` (opcional) é a proveniência RICA do frontmatter (ex.: livro/
            # capítulo/página) — separada do `prefixo` porque este também vira nome
            # de arquivo, e "p. 12-30" tem caracteres inválidos no Windows.
            bloco = normalizar_atomo(bloco, origem or prefixo, agora)
            if not bloco.strip():
                continue
            # DEDUP contra o banco (pedido: "impeça a duplicação"). Um átomo quase
            # idêntico a um já indexado não vira arquivo novo — senão a base incha com
            # a mesma ideia e o rag_top_k recupera clones. Fail-open sem embeddings.
            viz = await self._vizinho_proximo(strip_frontmatter(bloco))
            if viz is not None:
                dup, dist = viz
                # DUAS RÉGUAS, uma medição só (ver `obras_dedup_dist_max`):
                #  - `dedup_dist_max` (0,01) decide DESCARTAR o átomo que chega. É
                #    a régua do "mesmo átomo reatomizado" e não se mexe nela.
                #  - `obras_dedup_dist_max` (0,08) decide SUBSTITUIR uma edição pela
                #    outra. Precisa ser mais frouxa porque a mesma ideia dita por
                #    outra edição fica a 0,042-0,128 (medido no cap. 18: com a régua
                #    do dedup, a precedência de obra nunca disparava — 0 de 249).
                #    Este caminho nunca joga conhecimento fora: ele TROCA.
                # Antes, quem saía era sempre o que CHEGA — e quem chega é a edição
                # nova, então importar a Cannabis Encyclopedia por cima do Cervantes
                # antigo jogaria fora, fato a fato, o que se queria preferir.
                limiar_troca = max(settings.dedup_dist_max, settings.obras_dedup_dist_max)
                venceu = dist < limiar_troca and obras.substitui(
                    origem or prefixo,
                    str((getattr(dup, "metadata", None) or {}).get("origem") or ""),
                    obras.marcas(settings.obras_preferidas),
                    obras.marcas(settings.obras_substituidas),
                )
                if venceu and await self._aposentar(dup):
                    substituidos += 1
                elif dist < settings.dedup_dist_max:
                    duplicados += 1
                    # Métricas do ciclo (painel): o descarte vira EVENTO persistido —
                    # "quanto o dedup segura por dia" deixa de ser log transitório.
                    await asyncio.to_thread(db.log_etl, "DEDUP", _slug_titulo(bloco), "descartado")
                    continue
            nome = f"{prefixo}_{_slug_titulo(bloco)}_{int(time.time())}_{i}.md"
            # UMA PASTA POR OBRA (ordem do dono, 2026-07-28): tudo caía solto em
            # Conhecimento_Novo — 26 mil arquivos de 4 livros mais conversa e web
            # no mesmo diretório, impossível de aposentar um livro sem varrer o
            # frontmatter de todos. A busca não muda (o índice varre `**/*.md`
            # recursivo e as figuras já viviam em subpasta); o que muda é poder
            # mover uma obra inteira movendo uma pasta.
            destino = os.path.join(raiz, subpasta) if subpasta else raiz
            caminho = os.path.join(destino, nome)

            def _save(c=caminho, body=bloco, d=destino) -> None:
                os.makedirs(d, exist_ok=True)
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
        if substituidos:
            telemetry.track(tipo_log, f"Edição preferida: {substituidos} átomo(s) antigo(s) "
                                      f"aposentado(s) em favor do novo.")
        # #24: no idle, varre os átomos novos por CONTRADIÇÃO com a base existente.
        await self._varredura_contradicoes(salvos_info, origem or prefixo)
        return salvos

    async def _varredura_contradicoes(self, salvos_info: List[Tuple[str, str]],
                                      origem_nova: str = "") -> None:
        """Para cada átomo recém-salvo, acha o vizinho semântico "relacionado mas
        distinto" (a banda onde mora a contradição) e pergunta ao LLM se se
        contradizem. Capado por ciclo, preemptível (a conversa passa na frente) e
        fail-open (sem loja/embeddings, não faz nada). Os pares achados vão para a
        tabela `contradicoes` e são reportados sob demanda ('mestre, alguma
        contradição?').

        Detecção, não ação — com UMA exceção declarada (ordem do dono,
        2026-07-27): "se duas notas dizem o contrário uma da outra, vale a
        informação nova; a antiga sai da base". Isso só vale dentro da relação
        `preferida x superada` (mesma trava de `obras.substitui`), então uma
        contradição entre a enciclopédia nova e um livro de botânica continua
        sendo REPORTADA, nunca resolvida por decreto — as duas podem estar
        certas em contextos diferentes, e não há edição velha ali para aposentar.
        A nota que sai vai para a quarentena, como toda aposentadoria."""
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
            except Exception as exc:
                # Único sítio do projeto onde uma falha do LLM escaparia para cima (o
                # `_salvar_atomos` que chama isto não tem try). Encerra a varredura em
                # vez de seguir: se o modelo quebrou, o próximo par quebra igual — e a
                # varredura é bônus, nunca pode derrubar a atomização que a hospeda.
                telemetry.error("IDLE", "Varredura de contradição falhou", exc)
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
                # A informação NOVA prevalece — mas só sobre a edição que ela
                # declaradamente supera. O par fica registrado de qualquer forma,
                # então uma resolução errada continua auditável.
                if settings.contradicao_resolver_por_obra and obras.substitui(
                    origem_nova,
                    str((doc.metadata or {}).get("origem") or ""),
                    obras.marcas(settings.obras_preferidas),
                    obras.marcas(settings.obras_substituidas),
                ) and await self._aposentar(doc, "contradiz a edicao nova"):
                    telemetry.warn(
                        "CONTRADICAO",
                        f"Nota da edição superada aposentada: {os.path.basename(fonte_b)}")

    async def _vizinho_relacionado(self, corpo: str):
        """Vizinho na banda [dedup_dist_max, contradicao_dist_max): próximo o bastante
        para ser o MESMO tema, longe o bastante para não ser duplicata. Devolve
        (doc, dist) ou None. Barato: 1 embedding + 1 vizinho. Fail-open.

        Delega a busca ao `_vizinho_proximo` — é a MESMA consulta (vizinho nº 1), só
        com outra régua em cima. Eram duas cópias, e cada uma abria o Chroma cru por
        conta própria; unificar deixou UM ponto sabendo com que coleções falar."""
        viz = await self._vizinho_proximo(corpo)
        if viz is None:
            return None
        doc, dist = viz
        if settings.dedup_dist_max <= dist < settings.contradicao_dist_max:
            return doc, dist
        return None

    async def _ja_no_banco(self, corpo: str) -> bool:
        """True se um átomo quase idêntico já está indexado (distância < dedup_dist_max)."""
        return await self._duplicata(corpo) is not None

    async def _duplicata(self, corpo: str):
        """O átomo quase idêntico já indexado (distância < dedup_dist_max), ou None."""
        viz = await self._vizinho_proximo(corpo)
        return viz[0] if viz and viz[1] < settings.dedup_dist_max else None

    async def _vizinho_proximo(self, corpo: str):
        """`(doc, distância)` do vizinho mais próximo no banco, SEM limiar. Ou None.

        Sem limiar de propósito: as duas decisões que dependem dele usam réguas
        diferentes — descartar o átomo novo exige `dedup_dist_max` (0,01, o mesmo
        átomo reatomizado), enquanto trocar uma edição pela outra usa
        `obras_dedup_dist_max` (a mesma ideia dita com outras palavras). Medir uma
        vez e decidir duas vezes também poupa um embedding por átomo.

        Fail-open: sem embeddings/loja (testes) devolve None — dedup é uma trava de
        qualidade, não pode virar bloqueio de escrita. Barato: 1 embedding + 1 vizinho.

        ⚠ ESTE É O ÚNICO PONTO DO ETL QUE CONSULTA O ÍNDICE PARA DEDUP, e por isso é o
        único lugar onde a pergunta "contra QUAL base eu comparo?" existe. Com o
        multiusuário ligado, ir direto ao `_store` compararia o átomo de um dono só
        contra o ACERVO — o dedup deixaria de ver o vault pessoal dele e a mesma ideia
        entraria de novo a cada passada de idle. `recuperar()` é a porta pública que
        faz o fan-in (acervo + coleção pessoal do dono do contexto).

        TODO(rag.py): trocar os dois ramos por um `VectorStore.dedup_candidato(corpo)
        -> Optional[tuple[Doc, float]]` — o vizinho nº 1 do escopo, SEM o filtro
        `{"tipo": "texto"}` que o `recuperar` aplica. Enquanto ele não existe, o ramo
        desligado segue no caminho cru para preservar o comportamento de hoje byte a
        byte (com o filtro, uma nota de FIGURA deixaria de poder ser a duplicata
        encontrada — provavelmente melhor, mas é mudança não medida)."""
        store = self.ctx.vectorstore
        if store is None or getattr(store, "_store", None) is None or not corpo.strip():
            return None
        try:
            if multiusuario_ligado():
                res = await store.recuperar(corpo, k=1) or []
            else:
                res = await asyncio.to_thread(
                    store._store.similarity_search_with_score, corpo, 1)
        except Exception as exc:
            telemetry.warn("DEDUP", f"Falha ao checar duplicata (seguindo com o save): {exc}")
            return None
        return res[0] if res else None

    async def _aposentar(self, doc, motivo: str = "substituido por edicao preferida") -> bool:
        """Tira do vault a nota SUPERADA por uma edição mais nova. Best-effort.

        Ordem do dono (2026-07-27): "se tiver átomo duplicado, apague o átomo
        antigo, não o novo". Ela é MOVIDA para `dir_aposentados`, não destruída —
        o vault não tem backup (achado do painel de 13 especialistas), e fora do
        vault ela já some da busca: a purga de órfãos do `sync` apaga os chunks de
        toda fonte que não existe mais ali. Configurar `dir_aposentados` vazio
        volta ao unlink de verdade.

        Idempotente e silenciosa quanto ao alvo faltante: a nota pode já ter sido
        removida à mão entre a indexação e agora."""
        src = str((getattr(doc, "metadata", None) or {}).get("source") or "")
        if not src:
            return False
        origem = Path(src)

        def _mover() -> bool:
            if not origem.is_file():
                return False
            destino_dir = (settings.dir_aposentados or "").strip()
            if not destino_dir:
                origem.unlink()
                return True
            pasta = Path(destino_dir)
            pasta.mkdir(parents=True, exist_ok=True)
            alvo = pasta / origem.name
            # Dois átomos de nomes iguais vindos de pastas diferentes não podem se
            # sobrescrever aqui: isto é o arquivo morto, perder nele é perder de vez.
            if alvo.exists():
                alvo = pasta / f"{origem.stem}_{int(time.time())}{origem.suffix}"
            origem.replace(alvo)
            return True

        try:
            if not await asyncio.to_thread(_mover):
                return False
        except OSError as exc:
            telemetry.error("DEDUP", f"Falha ao aposentar nota superada: {src}", exc)
            return False
        store = self.ctx.vectorstore
        if store is not None and hasattr(store, "remover_fontes"):
            # Tira do índice AGORA em vez de esperar a purga de órfãos do próximo
            # sync: entre uma coisa e outra a busca ainda entregaria a versão velha.
            await store.remover_fontes([src])
        await asyncio.to_thread(db.log_etl, "APOSENTADO", os.path.basename(src), motivo)
        return True

    # -- Ingestão de livros — Fase 1 (2026-07-25) -------------------------------
    async def ingestao_livros(self) -> int:
        """Consome os jobs de capítulo de dados/ingestao/pendentes — SEMPRE em idle
        (o chamador é o scheduler, que só dispara sem sessão viva; aqui cada lote
        ainda cede a GPU via _esperar_idle + preemptible). Capado por ciclo.

        Crash-safe: o job só sai de pendentes/ após o capítulo INTEIRO ser salvo;
        reprocessar um job interrompido é idempotente (o dedup por átomo descarta
        o que já entrou). Devolve quantos capítulos concluiu."""
        pend = Path(settings.dir_ingestao) / "pendentes"
        if not pend.is_dir():
            return 0
        jobs = sorted(pend.glob("*.json"))[: settings.ingestao_caps_por_ciclo]
        concluidos = 0
        for job_path in jobs:
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                telemetry.error("INGESTAO", f"Job ilegível: {job_path.name}", exc)
                continue
            if await self._processar_capitulo(job):
                destino = pend.parent / "processados" / job_path.name
                destino.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await asyncio.to_thread(lambda a=job_path, b=destino: a.replace(b))
                except FileNotFoundError:
                    # O job sumiu da fila entre o glob e o arquivamento. Acontece
                    # quando DOIS drenadores rodam ao mesmo tempo (2026-07-27: um
                    # atomizador órfão sobreviveu ao script que o lançava e
                    # disputou a fila com o novo). O capítulo já foi atomizado —
                    # e o dedup por átomo torna reprocessar idempotente de
                    # qualquer forma —, então derrubar a drenagem INTEIRA por
                    # causa disto é o pior desfecho possível: os outros 100 jobs
                    # ficam parados por um arquivo que já está no lugar certo.
                    telemetry.warn("INGESTAO", f"Job já arquivado por outro processo: "
                                               f"{job_path.name}")
                concluidos += 1
        if concluidos:
            await self.ctx.vectorstore.sync()
            telemetry.track("INGESTAO", f"{concluidos} capítulo(s) atomizados e indexados.")
        return concluidos

    async def _processar_capitulo(self, job: dict) -> bool:
        """Um capítulo → átomos com proveniência + UMA nota-síntese (hierárquico:
        a atomização fragmenta o argumento; a síntese preserva a tese do capítulo).
        False = não terminou (preempção/erro) e o job PERMANECE pendente."""
        titulo_livro = job.get("livro", "?")
        cap = job.get("titulo_cap") or f"cap. {job.get('capitulo', '?')}"
        # A string vem do módulo do livro (ponto único): ela é a âncora que a nota
        # de figura attach-only usa para achar o texto da sua própria página.
        origem = livro_mod.origem_do_job(job)
        lotes = livro_mod.fatiar_lotes(job.get("texto", ""), settings.ingestao_lote_chars)
        # TRIAGEM (2026-07-25): capa, ficha catalográfica, índice remissivo e créditos
        # de fotos NÃO viram átomo. Medido no Amabis: 6% do livro é aparato editorial
        # — incluindo um capítulo inteiro de índice remissivo (53k chars). Filtrar
        # ANTES do LLM economiza a GPU e, sobretudo, evita ruído permanente no vault.
        if settings.triagem_habilitada:
            lotes, descartados = triagem.filtrar_lotes(lotes)
            if descartados:
                telemetry.track(
                    "INGESTAO",
                    f"'{cap}': {len(descartados)} trecho(s) fora da atomização — {descartados[0]}")
        if not lotes:
            telemetry.track("INGESTAO", f"'{cap}': só aparato editorial, nada a atomizar.")
            return True   # capítulo vazio/inútil: não bloqueia a fila
        corpos: List[str] = []
        # SALVAMENTO SOBREPOSTO (2026-07-25): o gráfico da GPU do dono mostrava
        # decode → VALE → decode dentro do mesmo capítulo. O vale era este salvamento:
        # para cada um dos ~57 átomos ele faz embedding, busca no Chroma e escreve em
        # disco — trabalho de CPU/disco que rodava DEPOIS do decode, com a GPU parada.
        # Agora o save do lote N corre enquanto o lote N+1 decodifica: o `await` do
        # collect libera o event loop, e a task pendente avança ali dentro.
        salvamento = None
        try:
            for lote in lotes:
                await self._esperar_idle()
                try:
                    conteudo = await self.ctx.llama.collect(
                        prompts.prompt_atomizar_livro(titulo_livro, cap, lote),
                        max_tokens=self._max_fundo(settings.max_tokens_atomizacao),
                        system_prompt=prompts.SYS_SINTESE,
                        preemptible=True,
                    )
                except InferenciaPreemptada:
                    telemetry.track("INGESTAO", f"'{cap}' cedeu a GPU — retoma no próximo idle.")
                    return False
                except Exception as exc:
                    telemetry.error("INGESTAO", f"Falha ao atomizar lote de '{cap}'", exc)
                    return False
                corpos.append(conteudo)
                if salvamento is not None:
                    await salvamento      # o do lote anterior já correu sob o decode
                salvamento = asyncio.ensure_future(
                    # acervo=True: livro é BIBLIOTECA, comum aos quatro — não a
                    # memória de quem por acaso estava conversando quando o idle rodou.
                    self._salvar_atomos(conteudo, "Livro", "INGESTAO_LIVRO", origem=origem,
                                        subpasta=livro_mod.slug(titulo_livro),
                                        acervo=True))
        finally:
            # Nenhum átomo fica pendurado, nem quando o capítulo aborta acima.
            if salvamento is not None:
                await salvamento
        await self._sintese_capitulo(titulo_livro, cap, origem, corpos,
                                     job.get("figuras") or [])
        return True

    async def _sintese_capitulo(self, titulo_livro: str, cap: str, origem: str,
                                corpos: List[str], figuras: List[dict] = ()) -> None:
        """O 'reduce' do capítulo. Best-effort: falha aqui não invalida os átomos
        já salvos (o capítulo conta como concluído — a síntese é o bônus da tese)."""
        base = "\n\n".join(corpos)[: settings.ingestao_lote_chars * 2]
        if not base.strip():
            return
        await self._esperar_idle()
        try:
            sintese = await self.ctx.llama.collect(
                prompts.prompt_sintese_capitulo(titulo_livro, cap, base),
                max_tokens=self._max_fundo(settings.max_tokens_sintese_capitulo),
                system_prompt=prompts.SYS_SINTESE,
                preemptible=True,
            )
        except Exception as exc:
            telemetry.error("INGESTAO", f"Falha na síntese de '{cap}'", exc)
            return
        if not sintese.strip():
            return
        # Nota única via _salvar_atomos: formato/tags garantidos, dedup incluso.
        # As FIGURAS do capítulo entram aqui (Fase 5a) e não em cada átomo: a
        # síntese É o capítulo, então o link fica preciso em vez de repetido; e a
        # legenda vai como texto, que é o que faz o RAG achar "o diagrama de X".
        corpo = f"## Síntese — {titulo_livro}: {cap}\n{sintese.strip()}\n#sintese_capitulo"
        corpo += figuras_mod.bloco_markdown(list(figuras), settings.subpasta_figuras)
        await self._salvar_atomos(corpo, "LivroSintese", "INGESTAO_LIVRO", origem=origem,
                                  subpasta=livro_mod.slug(titulo_livro), acervo=True)

    # -- Pasta vigiada de livros + colheita acadêmica (Fase 4, 2026-07-25) ------
    def _enfileirar_jobs(self, jobs: List[dict], base_nome: str) -> int:
        """Grava jobs na fila DURÁVEL da ingestão (dados/ingestao/pendentes). Nome
        com prefixo do documento + índice: dois documentos nunca se sobrescrevem."""
        pend = Path(settings.dir_ingestao) / "pendentes"
        pend.mkdir(parents=True, exist_ok=True)
        for j in jobs:
            alvo = pend / f"{base_nome}__{j['capitulo']:03d}.json"
            alvo.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        return len(jobs)

    async def ingerir_pasta_livros(self) -> int:
        """Pasta VIGIADA: PDFs largados em dados/livros/entrada/ são extraídos e
        enfileirados no idle — sem rodar script. Só a EXTRAÇÃO acontece aqui (rápida,
        sem GPU); a atomização é a passada de `ingestao_livros`.

        O PDF sempre SAI da entrada: digital vai p/ processados/, ESCANEADO vai p/
        aguardando_ocr/ (o worker OCR da Fase 3 pega de lá). Nada é apagado, e nada
        fica em loop sendo re-lido a cada tick."""
        if not settings.livros_entrada_habilitada:
            return 0
        entrada = Path(settings.dir_livros) / "entrada"
        if not entrada.is_dir():
            return 0
        pdfs = sorted(p for p in entrada.glob("*.pdf") if p.is_file())[: settings.livros_por_ciclo]
        feitos = 0
        for pdf in pdfs:
            try:
                paginas, toc = await asyncio.to_thread(livro_mod.extrair_pdf, pdf)
            except Exception as exc:
                telemetry.error("INGESTAO", f"Falha ao ler {pdf.name}", exc)
                await self._mover_livro(pdf, "falhou")
                continue
            titulo = pdf.stem
            if livro_mod.parece_escaneado([len(t) for t in paginas]):
                telemetry.track("INGESTAO", f"'{titulo}' é ESCANEADO — vai p/ aguardando_ocr (Fase 3).")
                await self._mover_livro(pdf, "aguardando_ocr")
                continue
            jobs = livro_mod.montar_jobs(titulo, paginas, toc)
            if not jobs:
                await self._mover_livro(pdf, "falhou")
                continue
            await self._anexar_figuras(pdf, titulo, jobs)
            n = await asyncio.to_thread(self._enfileirar_jobs, jobs, livro_mod.slug(titulo))
            await self._mover_livro(pdf, "processados")
            telemetry.track("INGESTAO", f"'{titulo}': {n} capítulo(s) enfileirados da pasta vigiada.")
            feitos += 1
        return feitos

    async def _anexar_figuras(self, pdf: Path, titulo: str, jobs: List[dict]) -> None:
        """Extrai as figuras do PDF (WebP no vault) e distribui cada uma para o job
        do capítulo cuja faixa de páginas a contém. Best-effort: falhar aqui não pode
        custar a ingestão do TEXTO, que é o que importa."""
        if not settings.figuras_habilitadas:
            return
        try:
            achadas = await asyncio.to_thread(
                figuras_mod.extrair_de_pdf, pdf, settings.dir_figuras,
                livro_mod.slug(titulo), settings.figuras_min_lado,
                settings.figuras_qualidade, settings.figuras_max_lado,
                settings.figuras_max_por_livro,
            )
        except Exception as exc:
            telemetry.error("FIGURAS", f"Falha ao extrair figuras de '{titulo}'", exc)
            return
        if not achadas:
            return
        for j in jobs:
            j["figuras"] = figuras_mod.figuras_do_intervalo(
                achadas, j["pagina_inicio"], j["pagina_fim"])
        telemetry.track("FIGURAS", f"'{titulo}': {len(achadas)} figura(s) em WebP no vault.")

    async def _mover_livro(self, pdf: Path, sub: str) -> None:
        destino = Path(settings.dir_livros) / sub
        destino.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(lambda: pdf.replace(destino / pdf.name))
        except OSError as exc:
            telemetry.error("INGESTAO", f"Falha ao mover {pdf.name} p/ {sub}", exc)

    # -- OCR de livro escaneado — Fase 3 (2026-07-25) ---------------------------
    async def ocr_livros(self) -> int:
        """Transcreve UM livro escaneado da fila `aguardando_ocr/`, N páginas por
        passada, retomando de onde parou (estado em `_ocr_estado/<slug>.json`).

        Só a TRANSCRIÇÃO acontece aqui; ao terminar o livro, os jobs entram na fila
        da Fase 1 e a atomização é a passada de ingestão seguinte. Devolve as páginas
        transcritas nesta passada (0 = nada a fazer, ou OCR não configurado).

        Pré-condição de VRAM: o chamador (scheduler) já descarregou o LLM."""
        if not settings.ocr_habilitado:
            return 0
        fila = Path(settings.dir_livros) / "aguardando_ocr"
        pdfs = sorted(p for p in fila.glob("*.pdf") if p.is_file()) if fila.is_dir() else []
        if not pdfs:
            return 0
        ok, motivo = ocr_mod.disponibilidade(
            settings.ocr_bin, settings.caminho_modelo_ocr, settings.caminho_mmproj_ocr)
        if not ok:
            # 1x por passada, não por página/livro: informa sem virar spam de log.
            telemetry.warn("OCR", f"{len(pdfs)} livro(s) esperando, mas o OCR não está pronto: {motivo}")
            return 0
        return await self._ocr_um_livro(pdfs[0])

    async def ocr_livro(self, pdf: Path, on_pagina=None) -> int:
        """Transcreve UM livro específico (acionamento MANUAL, scripts/ocr_agora.py).

        Mesmo caminho do worker idle — só a escolha do alvo muda: aqui o dono aponta
        o arquivo, lá a fila decide. `on_pagina(numero, texto)` (opcional) recebe cada
        página assim que sai, para acompanhar a transcrição ao vivo: custo ZERO de GPU
        (o texto já está em memória; sem callback ele seria só descartado). Devolve
        páginas feitas nesta chamada; 0 se o OCR não estiver configurado."""
        ok, motivo = ocr_mod.disponibilidade(
            settings.ocr_bin, settings.caminho_modelo_ocr, settings.caminho_mmproj_ocr)
        if not ok:
            telemetry.warn("OCR", f"OCR não está pronto: {motivo}")
            return 0
        return await self._ocr_um_livro(pdf, on_pagina=on_pagina)

    def _ocr_estado_path(self, pdf: Path) -> Path:
        return Path(settings.dir_livros) / "_ocr_estado" / f"{livro_mod.slug(pdf.stem)}.json"

    async def _ocr_um_livro(self, pdf: Path, on_pagina=None) -> int:
        # GUARDA ANTI-DESPERDÍCIO (visto ao vivo em 2026-07-25): PDF colocado na fila
        # à mão pode JÁ ter camada de texto — dois dos três livros do dono estavam
        # assim, e o OCR gastaria ~4h de GPU para produzir texto PIOR que o embutido.
        # Aqui ele é desviado para a entrada digital, que é rápida e mais fiel.
        # Aberto a partir dos BYTES, não do caminho: no Windows o PyMuPDF segura o
        # handle do arquivo quando falha em PDF inválido, e aí o `move` para falhou/
        # bate em "arquivo em uso" — o PDF ficaria preso na fila para sempre, retentado
        # a cada ciclo. Em stream mode nenhum handle toca o path (medido 2026-07-25).
        try:
            dados = await asyncio.to_thread(pdf.read_bytes)
            paginas_txt, _ = await asyncio.to_thread(livro_mod.extrair_pdf, None, dados)
        except Exception:
            paginas_txt = []
        if paginas_txt and not livro_mod.parece_escaneado([len(t) for t in paginas_txt]):
            telemetry.track(
                "OCR", f"'{pdf.stem}' JÁ tem texto selecionável — sem OCR; "
                       "mandando para a ingestão digital (mais rápida e mais fiel).")
            await self._mover_livro(pdf, "entrada")
            return 0
        estado_path = self._ocr_estado_path(pdf)
        estado = {"paginas": [], "proxima": 0}
        if estado_path.is_file():
            try:
                estado = json.loads(estado_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                telemetry.warn("OCR", f"Estado ilegível de {pdf.name}; recomeçando o livro.")
        try:
            total = await asyncio.to_thread(ocr_mod.total_paginas, pdf)
        except Exception as exc:
            telemetry.error("OCR", f"PDF ilegível: {pdf.name}", exc)
            await self._mover_livro(pdf, "falhou")
            return 0
        inicio = int(estado.get("proxima", 0))
        tmp = Path(settings.ocr_tmp_dir or (Path(settings.dir_livros) / "_ocr_tmp"))
        try:
            imagens = await asyncio.to_thread(
                ocr_mod.render_paginas, pdf, tmp, settings.ocr_dpi,
                inicio, settings.ocr_paginas_por_ciclo)
        except Exception as exc:
            telemetry.error("OCR", f"Falha ao rasterizar {pdf.name}", exc)
            return 0
        feitas = 0
        # UM servidor para o lote inteiro: o modelo (3 GB) carrega uma vez, não a
        # cada página. O `with` garante que o processo morre — e a VRAM volta —
        # mesmo se a conversa interromper no meio ou algo explodir.
        try:
            servidor = await asyncio.to_thread(
                lambda: ocr_mod.abrir_servidor(
                    settings.ocr_bin, settings.caminho_modelo_ocr,
                    settings.caminho_mmproj_ocr, settings.ocr_porta,
                    settings.ocr_timeout_pagina, settings.ocr_n_gpu_layers,
                    settings.ocr_n_ctx, settings.ocr_paralelo).__enter__())
        except Exception as exc:
            telemetry.error("OCR", "Não consegui subir o llama-server do OCR", exc)
            return 0
        # FILA CONTÍNUA (não blocos): um semáforo mantém `ocr_paralelo` páginas SEMPRE
        # em voo — assim que uma termina, a próxima entra. Em blocos, a barreira no fim
        # de cada bloco deixava slots ociosos esperando a página mais lenta (medido:
        # 1,99s/pág em blocos de 4 contra 1,61s/pág com a fila cheia; 2,02x contra o
        # sequencial de 3,26s). `gather` PRESERVA A ORDEM, obrigatório aqui — página
        # fora de ordem viraria um livro embaralhado no vault.
        passo = max(1, settings.ocr_paralelo)
        sem = asyncio.Semaphore(passo)
        PAUSADA = object()   # distingue "conversa começou" de "página falhou"

        async def _uma(img: Path):
            async with sem:
                # Cede a vez a CADA página (não a cada bloco): a GPU volta pra conversa
                # em ~2s se o dono aparecer. Mesma porta que o resto do ETL respeita.
                if not self.ctx.interactive_idle.is_set():
                    return PAUSADA
                return await asyncio.to_thread(servidor.transcrever, img)

        try:
            resultados = await asyncio.gather(*(_uma(img) for img in imagens))
            for img, texto in zip(imagens, resultados):
                if texto is PAUSADA:
                    telemetry.track("OCR", "Conversa começou — OCR pausado, retoma no próximo idle.")
                    break        # e as seguintes também param: a ordem é contígua
                if texto is None:
                    telemetry.warn("OCR", f"Página {inicio + feitas + 1} falhou; segue para a próxima.")
                    texto = ""
                estado["paginas"].append(texto)
                estado["proxima"] = inicio + feitas + 1
                feitas += 1
                if on_pagina is not None:
                    # Acompanhamento ao vivo (manual): custo zero — o texto já existe.
                    try:
                        on_pagina(estado["proxima"], texto)
                    except Exception as exc:
                        telemetry.error("OCR", "Falha no callback de progresso", exc)
                await asyncio.to_thread(img.unlink, True)
                if feitas % passo == 0:
                    # Estado a cada `passo` páginas: um crash custa no máximo isso,
                    # sem reescrever o JSON a cada página.
                    await asyncio.to_thread(self._salvar_estado_ocr, estado_path, estado)
            await asyncio.to_thread(self._salvar_estado_ocr, estado_path, estado)
        finally:
            await asyncio.to_thread(servidor.__exit__, None, None, None)
        telemetry.track("OCR", f"'{pdf.stem}': {estado['proxima']}/{total} páginas transcritas.")
        if estado["proxima"] >= total:
            await self._finalizar_ocr(pdf, estado, estado_path)
        return feitas

    def _salvar_estado_ocr(self, path: Path, estado: dict) -> None:
        """Grava o progresso a CADA página (OCR é caro: um crash não pode custar o
        livro inteiro). Escreve em .tmp e renomeia — nunca deixa JSON pela metade."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    async def _finalizar_ocr(self, pdf: Path, estado: dict, estado_path: Path) -> None:
        """Livro transcrito: vira jobs de capítulo (sem TOC — PDF de imagem não tem),
        o PDF sai da fila e o estado é limpo."""
        paginas = [p for p in estado.get("paginas", [])]
        uteis = [p for p in paginas if ocr_mod.pagina_util(p, settings.ocr_min_chars_pagina)]
        if not uteis:
            telemetry.warn("OCR", f"'{pdf.stem}': nenhuma página com texto aproveitável.")
            await self._mover_livro(pdf, "falhou")
            return
        jobs = livro_mod.montar_jobs(pdf.stem, paginas, [])
        n = await asyncio.to_thread(self._enfileirar_jobs, jobs, f"ocr-{livro_mod.slug(pdf.stem)}")
        await self._mover_livro(pdf, "processados")
        await asyncio.to_thread(estado_path.unlink, True)
        telemetry.track("OCR", f"'{pdf.stem}' transcrito: {n} capítulo(s) na fila de atomização.")

    async def colheita_academica(self) -> int:
        """Fase 4: busca PDFs acadêmicos sobre os temas quentes/lacunas do dono e os
        enfileira como jobs (herdando proveniência + síntese da ingestão). SEM LLM
        aqui — é rede e extração; a atomização é a passada de `ingestao_livros`.

        Isolada do caminho vivo: nada nesta função toca a atomização web em tempo
        real. Alvos vêm das tabelas que já existem (temas_quentes, lacunas) e são
        CARIMBADOS como pesquisados para não repetir no próximo ciclo."""
        if not settings.academico_habilitado:
            return 0
        alvos = await self._alvos_academicos()
        if not alvos:
            return 0
        total = 0
        for termos, marcar in alvos:
            try:
                candidatos = await self.ctx.web.buscar_pdfs(termos, settings.academico_resultados_busca)
            except Exception as exc:
                telemetry.error("ACADEMICO", f"Falha na busca de PDFs para '{termos}'", exc)
                continue
            aceitos = 0
            for cand in candidatos:
                if aceitos >= settings.academico_pdfs_por_alvo:
                    break
                if await asyncio.to_thread(db.pdf_academico_visto, cand["url"]):
                    continue
                total += await self._colher_pdf(cand, termos)
                aceitos += 1 if total else 0
            await asyncio.to_thread(marcar, textutils.normaliza(termos))
        if total:
            telemetry.track("ACADEMICO", f"{total} paper(s) enfileirados p/ atomização no idle.")
        return total

    async def _alvos_academicos(self) -> List[Tuple[str, object]]:
        """Temas QUENTES primeiro (o que o dono mais reusa), depois LACUNAS (o que
        ficou sem resposta) — os dois sinais que o projeto já coleta."""
        quentes = await asyncio.to_thread(
            db.get_temas_quentes, settings.academico_alvos_por_ciclo,
            settings.idle_temas_min_reuso, settings.academico_intervalo_horas // 24 or 1,
        )
        alvos: List[Tuple[str, object]] = [(t["termos"], db.marcar_tema_pesquisado) for t in quentes]
        if len(alvos) < settings.academico_alvos_por_ciclo:
            lacunas = await asyncio.to_thread(db.get_lacunas, settings.academico_alvos_por_ciclo)
            alvos += [(l["termos"], db.marcar_lacuna_pesquisada) for l in lacunas]
        return alvos[: settings.academico_alvos_por_ciclo]

    async def _colher_pdf(self, cand: dict, termos: str) -> int:
        """Baixa, extrai e enfileira UM paper. 0 = descartado (motivo logado)."""
        url = cand["url"]
        dados = await self.ctx.web.baixar_pdf(url, settings.academico_max_mb)
        await asyncio.to_thread(db.marcar_pdf_academico, url)  # visto: não retenta
        if not dados:
            return 0
        try:
            paginas, _ = await asyncio.to_thread(livro_mod.extrair_pdf, None, dados)
        except Exception as exc:
            telemetry.warn("ACADEMICO", f"PDF ilegível ({url[:60]}): {exc}")
            return 0
        titulo = academico.titulo_do_paper(cand.get("titulo", ""), url)
        job = academico.job_de_paper(titulo, url, paginas, settings.academico_min_chars)
        if job is None:
            telemetry.track("ACADEMICO", f"Texto raso (paywall/scan), descartado: {url[:60]}")
            return 0
        job["texto"] = "\n\n".join(antiinjecao.filtrar_chunks(
            [b for b in job["texto"].split("\n\n") if b.strip()])[0])
        if not job["texto"].strip():
            return 0
        await asyncio.to_thread(self._enfileirar_jobs, [job],
                                f"paper-{livro_mod.slug(termos)}-{livro_mod.slug(titulo, 24)}")
        return 1

    # -- Consolidação de átomos — Fase 2 (2026-07-25) ---------------------------
    async def consolidar_atomos(self) -> int:
        """Funde grupos de átomos quase-idênticos num canônico ("3000 relatórios de
        um assunto"). Só o subdir AUTO-COLHIDO (Conhecimento_Novo) — nota escrita à
        mão nunca é tocada. Ordem à prova de falha: a fusão via LLM acontece ANTES
        de qualquer mexida em arquivo; originais são ARQUIVADOS (nunca deletados) e
        removidos do índice; só então o canônico é salvo. Devolve grupos fundidos.

        ⚠ UMA PASSADA POR RAIZ DE ESCRITA, nunca uma só sobre tudo que enxergo. O
        `corpus_com_embeddings` já faz o fan-in (acervo + vault pessoal do dono), e um
        grupo que misturasse os dois seria FUNDIDO num canônico único — o conteúdo
        pessoal de alguém acabaria dentro de uma nota da biblioteca comum, que os
        quatro leem. Não é um risco teórico: é a operação que este método faz de
        propósito. O acervo entra na lista porque a ingestão de obra escreve nele, e
        deixá-lo de fora silenciaria a consolidação dos átomos de livro."""
        vs = self.ctx.vectorstore
        if not hasattr(vs, "corpus_com_embeddings"):
            return 0   # fail-open: fakes antigos/índice frio
        st = self.ctx.settings
        if not multiusuario_ligado():
            return await self._consolidar_raiz(raiz_dos_atomos(st), acervo=False)
        total = 0
        total += await self._consolidar_raiz(raiz_dos_atomos(st, acervo=True), acervo=True)
        for dono in await asyncio.to_thread(donos_do_ciclo, st):
            with identidade.usar_dono(dono):
                try:
                    total += await self._consolidar_raiz(raiz_dos_atomos(st), acervo=False)
                except Exception as exc:
                    telemetry.error(
                        "CONSOLIDACAO", f"Consolidação do vault de '{dono}' falhou", exc)
        return total

    async def _consolidar_raiz(self, raiz_alvo: Path, acervo: bool) -> int:
        """A consolidação de UMA raiz do vault. É o corpo histórico do
        `consolidar_atomos`; o que mudou foi receber a raiz por parâmetro."""
        corpus = await self.ctx.vectorstore.corpus_com_embeddings()
        if not corpus:
            return 0
        raiz = os.path.normpath(str(raiz_alvo))
        por_fonte: dict = {}
        for src, texto, emb in corpus:
            # 1 chunk representa o átomo (átomo é curto; chunk extra ~ quase-cópia)
            src_n = os.path.normpath(src)
            if src_n.startswith(raiz) and src_n not in por_fonte:
                por_fonte[src_n] = emb
        fontes = list(por_fonte)
        if len(fontes) < settings.consolidacao_min_grupo:
            return 0
        grupos = await asyncio.to_thread(
            consolidacao.agrupar_redundantes,
            [por_fonte[s] for s in fontes],
            settings.consolidacao_dist_max, settings.consolidacao_min_grupo,
        )
        fundidos = 0
        for grupo in grupos[: settings.consolidacao_grupos_por_ciclo]:
            if await self._consolidar_grupo([fontes[i] for i in grupo], acervo):
                fundidos += 1
        if fundidos:
            await self.ctx.vectorstore.sync()
            telemetry.track("CONSOLIDACAO", f"{fundidos} grupo(s) fundido(s) em canônicos.")
        return fundidos

    async def _consolidar_grupo(self, caminhos: List[str], acervo: bool = False) -> bool:
        """Um grupo → um átomo canônico. False = grupo fica para o próximo ciclo."""
        textos: List[str] = []
        for c in caminhos:
            try:
                textos.append(strip_frontmatter(
                    await asyncio.to_thread(lambda p=c: Path(p).read_text(encoding="utf-8"))))
            except OSError:
                return False   # arquivo mexido por fora: não arrisca, tenta depois
        await self._esperar_idle()
        try:
            fundido = await self.ctx.llama.collect(
                prompts.prompt_fundir_atomos("\n\n---\n\n".join(textos)[:settings.ingestao_lote_chars]),
                max_tokens=self._max_fundo(settings.max_tokens_sintese),
                system_prompt=prompts.SYS_SINTESE,
                preemptible=True,
            )
        except InferenciaPreemptada:
            telemetry.track("CONSOLIDACAO", "Fusão cedeu a GPU — retoma no próximo idle.")
            return False
        except Exception as exc:
            telemetry.error("CONSOLIDACAO", "Falha na fusão do grupo", exc)
            return False
        if not fundido.strip():
            return False
        # ARQUIVA (nunca deleta) ANTES de salvar o canônico — os originais ainda
        # indexados fariam o dedup do save matar o próprio canônico.
        destino = Path(settings.dir_arquivo_consolidacao) / datetime.now().strftime("%Y%m%d_%H%M%S")
        destino.mkdir(parents=True, exist_ok=True)

        def _mover() -> None:
            for c in caminhos:
                Path(c).replace(destino / Path(c).name)

        await asyncio.to_thread(_mover)
        if hasattr(self.ctx.vectorstore, "remover_fontes"):
            await self.ctx.vectorstore.remover_fontes(caminhos)
        nomes = ", ".join(Path(c).name for c in caminhos[:5]) + ("…" if len(caminhos) > 5 else "")
        origem = (f"Consolidação de {len(caminhos)} átomos ({nomes}) — "
                  f"originais em _arquivo_consolidacao/{destino.name}/")
        # O canônico volta para a MESMA raiz de onde saíram os originais (ver o ⚠ do
        # `consolidar_atomos`): fundir dentro de um escopo e gravar em outro seria o
        # mesmo vazamento, só com um passo a mais.
        salvos = await self._salvar_atomos(fundido, "Consolidado", "CONSOLIDACAO",
                                           origem=origem, acervo=acervo)
        if not salvos:
            # Pior caso do design: canônico não entrou, mas NADA se perdeu — os
            # originais estão íntegros no arquivo morto; restaurar = mover de volta.
            telemetry.warn("CONSOLIDACAO", f"Canônico não salvo; originais preservados em {destino}")
            return False
        await asyncio.to_thread(db.log_etl, "CONSOLIDACAO",
                                f"{len(caminhos)} -> 1 ({destino.name})", "CONCLUIDO")
        return True

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
            # Anti-injeção na PERSISTÊNCIA (painel 2026-07-24, seg-01): o filtro do
            # caminho VIVO (rag._deep_fetch) não cobre esta fila — a colheita dos
            # perdedores do race enfileira texto CRU de página (respostas.on_colheita),
            # e um payload que chegasse aqui viraria átomo PERMANENTE do vault,
            # aterrando respostas futuras. Choke point único da rota web→átomo: dropa
            # só o bloco envenenado; página inteiramente suja morre sem virar nota.
            blocos = [b for b in dados.split("\n\n") if b.strip()]
            limpos, removidos = antiinjecao.filtrar_chunks(blocos)
            if removidos:
                telemetry.track(
                    "ETL_POST_CHAT",
                    f"Anti-injeção: {removidos} trecho(s) descartado(s) de '{tema}'.",
                )
            if not limpos:
                pendentes.pop(0)
                continue
            dados = "\n\n".join(limpos)
            # EM LOTES que cabem no n_ctx (2026-07-31): a colheita enfileira o CORPO de
            # uma página inteira, até `web_fetch_max_chars` (20.000 chars) — o prompt
            # único podia estourar o contexto do mesmo jeito que o dump da conversa.
            sinteses: List[str] = []
            preemptado = False
            for lote in textutils.lotes_de_texto(dados, settings.etl_lote_chars):
                await self._esperar_idle()    # a série é longa: cede a vez a cada lote
                try:
                    sinteses.append(await self.ctx.llama.collect(
                        prompts.prompt_sintese(tema, lote),
                        max_tokens=self._max_fundo(settings.max_tokens_sintese),  # #29
                        system_prompt=prompts.SYS_SINTESE,
                        preemptible=True,   # background: a pergunta do usuário passa na frente
                    ))
                except InferenciaPreemptada:
                    preemptado = True
                    break
                except Exception as exc:
                    # Para a série, mas guarda o que já saiu: meia página retida é melhor
                    # que nenhuma, e o dedup por átomo limpa a sobreposição de um retry.
                    telemetry.error("ETL_POST_CHAT", f"Falha ao sintetizar lote de '{tema}'", exc)
                    break
            if preemptado:
                telemetry.track("ETL_POST_CHAT", f"'{tema}' cedeu a GPU — será retomado no idle.")
                continue                      # item continua em pendentes[0]
            pendentes.pop(0)                  # consumido: erro real não retenta em loop
            conteudo = "\n\n".join(s for s in sinteses if s.strip())
            if not conteudo:
                continue
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
        for salva com sucesso — senão a conversa fica pra próxima passada (nada se perde).

        Uma passada POR DONO (ver `_por_dono`): cada um tem o seu dump, e a conversa de
        um jamais pode virar átomo no vault de outro — esse vazamento seria PERMANENTE,
        porque o dump morre na atomização e a nota fica."""
        await self._por_dono(self._summarize_dump_do_dono)

    async def _summarize_dump_do_dono(self) -> None:
        """A atomização do dump de UM dono — o corpo histórico do `summarize_dump`."""
        path = caminho_chat_dump(self.ctx.settings)
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

        # EM LOTES que cabem no n_ctx (idle de 2026-07-31): o dump acumula entre as
        # passadas — quando uma atomização é preemptada ou não salva nada, a conversa
        # fica para a próxima. Ele chegou a 2 dias (118 turnos, ~57k chars) e a chamada
        # única morreu com "Requested tokens (21277) exceed context window of 8192".
        # Fatiar preserva a conversa inteira; truncar a jogaria fora.
        lotes = textutils.lotes_de_texto(conteudo, settings.etl_lote_chars)
        telemetry.track("IDLE", f"Atomizando histórico da conversa (Zettelkasten): "
                                f"{len(conteudo)} chars em {len(lotes)} lote(s).")
        partes: List[str] = []
        falhou = False
        for lote in lotes:
            # Cede a vez ANTES de cada lote, não só uma vez no começo: numa conversa
            # longa são várias chamadas, e o usuário pode voltar no meio da série.
            await self._esperar_idle()
            try:
                parte = await self.ctx.llama.collect(
                    prompts.prompt_sintese_conversa(lote),
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
            except Exception as exc:
                telemetry.error("IDLE", "Falha ao atomizar um lote da conversa", exc)
                falhou = True
                continue
            parte = parte.strip()
            if not parte:
                # Resposta VAZIA não é "nada a reter" — o prompt manda escrever o literal
                # 'NADA' nesse caso. Vazio significa que o decode MORREU (hoje o worker do
                # llm.py loga e encerra a stream), e foi exatamente essa confusão que fez
                # o estouro de contexto ser lido como small talk e APAGAR o dump.
                telemetry.warn("IDLE", "Lote da conversa voltou vazio do LLM — tratado como falha.")
                falhou = True
                continue
            if parte.upper().strip(".!\n ") != "NADA":
                partes.append(parte)

        atomos = "\n\n".join(partes)

        async def _limpar_dump() -> None:
            try:
                await asyncio.to_thread(lambda: open(path, "w").close())
            except OSError as exc:
                telemetry.error("IDLE", "Erro ao limpar dump", exc)

        if falhou:
            # NUNCA limpa com falha em jogo. O que deu certo é salvo mesmo assim (nada
            # se perde nos dois sentidos): o dedup por átomo de `_salvar_atomos` descarta
            # o repetido quando a próxima passada reprocessar o dump inteiro.
            if atomos:
                salvos = await self._salvar_atomos(atomos, "Conversa", "IDLE_CONVERSA")
                if salvos:
                    await self.ctx.vectorstore.sync()
                    telemetry.warn("IDLE", f"Conversa atomizada em PARTE ({salvos} átomo(s)) — "
                                           "dump preservado p/ retry.")
                    return
            telemetry.warn("IDLE", "Atomização da conversa falhou — dump preservado p/ retry.")
            return

        # O prompt manda responder só 'NADA' quando não há conhecimento a reter
        # (conversa de small talk). Nesse caso não cria nota — mas limpa o dump.
        if not atomos:
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

    async def _perfil_vigente(self) -> str:
        """O perfil de estilo DESTE dono, para o LLM refinar em cima.

        Um usuário só: o cache em `ctx.perfil_conversa`, como sempre. Vários: o BANCO
        (`db.ler_perfil` já filtra por dono), porque `ctx.perfil_conversa` é UM valor
        para a máquina inteira — refinar o perfil de B em cima do de A produziria uma
        voz misturada, e gravá-lo no cache daria a voz de B ao próximo turno de A.

        TODO(main.py/state.py): `ctx.perfil_conversa` precisa virar por-dono (dict ou
        leitura sob demanda). Enquanto for um valor só, o hot-path que o LÊ
        (`respostas`) continua entregando o perfil de quem o cacheou no boot — este
        método corrige a ESCRITA, não a leitura do outro lado."""
        if not multiusuario_ligado():
            return self.ctx.perfil_conversa or ""
        return await asyncio.to_thread(db.ler_perfil) or ""

    async def _atualizar_perfil(self, conteudo: str) -> None:
        """#36: destila da conversa uma diretriz de COMO responder ao usuário e a
        persiste (+ atualiza o cache em ctx, lido no hot-path). Preemptível; 'NADA'
        do LLM mantém o perfil atual. Best-effort — nunca derruba a atomização."""
        if not settings.diapasao_habilitado:
            return
        atual = await self._perfil_vigente()
        await self._esperar_idle()
        try:
            resp = await self.ctx.llama.collect(
                # CAUDA, não o começo (teto de 2026-07-31): este prompt recebia o MESMO
                # dump inteiro que estourou o n_ctx no `summarize_dump`. Aqui truncar é a
                # escolha certa, não a preguiçosa — o perfil descreve como falar com o
                # usuário AGORA, e é o fim da conversa que carrega isso.
                prompts.prompt_perfil_conversa(conteudo[-settings.etl_perfil_max_chars:],
                                               atual),
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
        if novo and novo != atual:
            # O cache em `ctx` só é escrito quando ele DE FATO representa este dono —
            # ver `_perfil_vigente`. O banco é gravado sempre (lá o perfil tem dono).
            if not multiusuario_ligado():
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
        encerra a pesquisa — o idle acabou, e as lacunas não-tocadas ficam para a próxima.

        Uma passada POR DONO (ver `_por_dono`): a tabela `lacunas` tem coluna `dono` e
        `db.get_lacunas` filtra pelo contexto, então rodar isto sem dono (o caso do
        scheduler, que dispara pelo RELÓGIO) não traria lacuna nenhuma — a base pararia
        de crescer em silêncio, que é a pior falha possível numa rotina de fundo."""
        await self._por_dono(self._pesquisa_proativa_do_dono)

    async def _pesquisa_proativa_do_dono(self) -> None:
        """A pesquisa proativa de UM dono — o corpo histórico da `pesquisa_proativa`."""
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

    async def revalidar_acervo_web(self) -> None:
        """No idle, reconfere se cada imagem colhida da web é o que a nota AFIRMA ser.

        Por que existe: o gate que julga o candidato ANTES de baixar
        (`imagem_web.casa_com_o_pedido`) só passou a rodar em 2026-07-31. O que entrou
        antes dele nunca foi julgado — e das 40 notas de `Figuras/_web/`, **4 (10%)**
        mostram outra coisa: 'polvo' é um calendário do EBT da Flórida, 'estômato' é um
        formulário de currículo em espanhol, 'solo argiloso' é uma miniatura do Rainbow
        Six Siege (o buscador casou o "solo" de *solo queue*). Elas estão INDEXADAS e
        podem ser anexadas como figura numa resposta.

        Barata de propósito, e é isso que a torna adequada ao idle: NÃO usa LLM, NÃO usa
        VLM, NÃO usa rede — é comparação léxica sobre o título que a nota já carrega. O
        Qwen2.5-VL-7B pega as mesmas 4, mas custa 7,9 GB de VRAM (numa placa de 10) e
        0,87 s por imagem; aqui são microssegundos e a GPU segue livre para a conversa.

        Não apaga nem reescreve nada: marca `acervo_confere: NAO` no frontmatter, que é
        metadado (não entra no corpo indexado) e deixa a decisão com o dono. Marcar é
        reversível; apagar imagem por heurística não é.
        """
        if not settings.idle_revalidar_acervo:
            return
        pasta = imagem_web.pasta_do_acervo()
        if not pasta.is_dir():
            return
        try:
            notas = sorted(pasta.glob("*.md"))
            marcadas, conferidas = await asyncio.to_thread(self._revalidar_notas, notas)
        except OSError as exc:
            telemetry.error("ETL_ACERVO", "Revalidação do acervo web falhou", exc)
            return
        if conferidas:
            telemetry.track("ETL_ACERVO",
                            f"Acervo web revalidado: {conferidas} nota(s), "
                            f"{marcadas} marcada(s) como suspeita(s).")

    def _revalidar_notas(self, notas: List[Path]) -> Tuple[int, int]:
        """O laço síncrono da revalidação (roda em `to_thread`). Devolve (marcadas, lidas)."""
        marcadas = conferidas = 0
        for md in notas:
            try:
                texto = md.read_text(encoding="utf-8")
            except OSError:
                continue      # nota sumindo no meio do idle não derruba o ciclo
            m = _TERMO_DO_ACERVO.search(texto)
            titulo = _TITULO_H2.search(texto)
            if not (m and titulo):
                continue
            conferidas += 1
            ok = imagem_web.confere_o_acervo(
                m.group(1), titulo.group(1), _evidencia_independente(texto))
            marca = "sim" if ok else "NAO"
            if not ok:
                marcadas += 1
            atual = _CAMPO_CONFERE.search(texto)
            if atual and atual.group(1) == marca:
                continue      # idempotente: sem reescrita, sem mexer no mtime do reindex
            novo = (_CAMPO_CONFERE.sub(f"acervo_confere: {marca}", texto, count=1)
                    if atual else
                    texto.replace("tipo: figura", f"tipo: figura\nacervo_confere: {marca}", 1))
            if novo != texto:
                md.write_text(novo, encoding="utf-8")
        return marcadas, conferidas

    async def pesquisa_temas_quentes(self) -> None:
        """#4: no idle, RE-PESQUISA na web os temas que o usuário mais REUSA do vault (o
        estágio Banco respondeu de fato) para trazer NOVIDADE. É o ESPELHO da lacuna:
        lacuna = o que FALTOU (banco não cobria); tema quente = o favorito que o banco JÁ
        cobre — por isso, ao contrário da proativa, NÃO há o skip Nível-1 por cobertura do
        banco (o banco sempre cobre; seria pular tudo). O valor vem do dedup por átomo em
        `_salvar_atomos`: só o fato INÉDITO vira nota, o resto é descartado.

        Roda DEPOIS da pesquisa_proativa (buraco genuíno tem prioridade sobre refrescar o
        que já se sabe). Preemptível: se o usuário volta, InferenciaPreemptada encerra e os
        temas não-tocados ficam para a próxima passada de idle. Capado por ciclo.

        Uma passada POR DONO, pelo mesmo motivo da lacuna: `temas_quentes` tem coluna
        `dono`, e o favorito de um não é o do outro."""
        await self._por_dono(self._pesquisa_temas_do_dono)

    async def _pesquisa_temas_do_dono(self) -> None:
        """A re-pesquisa de temas quentes de UM dono — o corpo histórico do método."""
        if not settings.idle_pesquisa_temas:
            return
        temas = await asyncio.to_thread(
            db.get_temas_quentes,
            settings.idle_temas_max * 4,
            settings.idle_temas_min_reuso,
            settings.idle_temas_cooldown_dias,
        )
        if not temas:
            return
        feitas = 0
        for tema in temas:
            if feitas >= settings.idle_temas_max:
                break
            termos = tema["termos"]
            chave = textutils.normaliza(termos)
            # Mesmo backstop da lacuna: trivial ('ok') e sem-núcleo não viram pesquisa.
            if not lacuna_pesquisavel(termos):
                await asyncio.to_thread(db.marcar_tema_pesquisado, chave)
                continue
            await self._esperar_idle()
            dados = await self.ctx.web.search(termos, consulta=termos)
            if not dados or dados == NENHUM:
                await asyncio.to_thread(db.marcar_tema_pesquisado, chave)
                continue
            try:
                conteudo = await self.ctx.llama.collect(
                    prompts.prompt_sintese(termos, dados),
                    max_tokens=self._max_fundo(settings.max_tokens_sintese),  # #29
                    system_prompt=prompts.SYS_SINTESE,
                    preemptible=True,
                )
            except InferenciaPreemptada:
                telemetry.track("ETL_TEMAS", "Usuário voltou — re-pesquisa de temas adiada.")
                return
            except Exception as exc:
                telemetry.error("ETL_TEMAS", f"Falha ao sintetizar tema quente '{termos}'", exc)
                await asyncio.to_thread(db.marcar_tema_pesquisado, chave)
                continue
            # O dedup por átomo (dentro de _salvar_atomos) descarta o que já se sabe —
            # salvos>0 só quando a web trouxe fato INÉDITO. Marca em todo caso (cooldown).
            salvos = await self._salvar_atomos(conteudo, "TemaQuente", "ETL_TEMAS")
            await asyncio.to_thread(db.marcar_tema_pesquisado, chave)
            if salvos:
                feitas += 1
                telemetry.track("ETL_TEMAS", f"Tema quente '{termos}': {salvos} átomo(s) novos.")
        if feitas:
            await self.ctx.vectorstore.sync()
            telemetry.track("ETL_TEMAS", f"Re-pesquisa de temas quentes: {feitas} tema(s) atualizado(s).")

    async def run_idle(self, itens: List[Tuple[str, str]]) -> None:
        """Orquestra o idle: 1) atomiza as pesquisas da fila, 2) atomiza a conversa,
        3) PESQUISA PROATIVA das lacunas, 4) RE-PESQUISA os TEMAS QUENTES (#4: os favoritos
        que o usuário mais reusa), 5) DESCARREGA o modelo, liberando a VRAM (o pilar pedido:
        a GPU volta pra outros trabalhos quando o chat para).

        A ordem importa e foi pedida assim: o ETL PRECISA do modelo, então o unload é o
        ÚLTIMO passo. Lacunas (buraco) vêm antes dos temas quentes (refrescar o conhecido):
        prioridade ao que falta. E só descarrega se ninguém voltou a interagir no meio-tempo —
        `interactive_idle` está SETADO quando não há inferência interativa em voo; se o
        usuário mandou algo, o pipeline o limpou e o unload é pulado (o próprio pipeline
        religou/manteve o modelo). Se descarregar e a mensagem chegar logo depois,
        `ensure_loaded` (no stream) religa: seguro nas duas direções."""
        # A marca é AQUI e não em quem chama porque são três chamadores (fim de
        # conversa, /api/idle e teste) e o watcher de economia precisa que nenhum
        # deles esqueça: dormir no meio de uma atomização joga fora o trabalho feito.
        self.ctx.idle_em_andamento = True
        try:
            await self._run_idle(itens)
        finally:
            self.ctx.idle_em_andamento = False

    async def _run_idle(self, itens: List[Tuple[str, str]]) -> None:
        await self.process_queue(itens)
        await self.summarize_dump()
        # ANTES das pesquisas de propósito: é a única rotina do idle que não toca o LLM
        # (léxico puro, milissegundos), então roda enquanto a GPU ainda está quente com o
        # trabalho anterior, em vez de esperar a fila de inferência.
        await self.revalidar_acervo_web()
        await self.pesquisa_proativa()
        await self.pesquisa_temas_quentes()
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
        se a base multiplicar, trocar por count() + amostragem.

        ⚠ COM O MULTIUSUÁRIO LIGADO ESTE NÚMERO CONTA SÓ O ACERVO. `_store` é a coleção
        base, e o snapshot roda no idle sem dono no contexto — de propósito: ele é o
        painel do DONO DA MÁQUINA (mesma família das tabelas que `telemetry` deixou sem
        segmentar), e somar os vaults pessoais aqui exporia o tamanho da memória de cada
        um num gráfico que os quatro veem. O que está errado é o número ficar MENOR sem
        avisar — daí o aviso no log, e não um total silenciosamente incompleto.

        TODO(rag.py): um `VectorStore.dump_escopo(include)` público resolveria o outro
        lado (snapshot por dono, cada um vendo o seu). Não há hoje porta pública que
        pagine documents+metadatas do escopo — `corpus_com_embeddings` traz os vetores
        junto e é caro demais para observabilidade."""
        try:
            store = getattr(self.ctx.vectorstore, "_store", None)
            if store is None:
                return
            if multiusuario_ligado():
                telemetry.track(
                    "BASE", "Snapshot diário: contando só o acervo comum "
                            "(os vaults pessoais ficam fora do painel da máquina).")
            if await asyncio.to_thread(db.snapshot_base_hoje):
                return
            dump = await asyncio.to_thread(
                rag.dump_paginado, store, ["documents", "metadatas"]
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
