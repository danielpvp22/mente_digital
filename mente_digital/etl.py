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
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from mente_digital import contradicao
from mente_digital import diapasao
from mente_digital import prompts
from mente_digital import textutils
from mente_digital import academico
from mente_digital import antiinjecao
from mente_digital import consolidacao
from mente_digital import livro as livro_mod
from mente_digital import ocr as ocr_mod
from mente_digital.atomos import _slug_titulo, dividir_atomos, normalizar_atomo
from mente_digital.config import settings
from mente_digital.llm import InferenciaPreemptada
from mente_digital.otimizador import lacuna_pesquisavel
from mente_digital.rag import NENHUM, strip_frontmatter
from mente_digital.state import AppContext
from mente_digital.telemetry import db, telemetry


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

    async def _salvar_atomos(self, texto: str, prefixo: str, tipo_log: str,
                             origem: Optional[str] = None) -> int:
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
            # `origem` (opcional) é a proveniência RICA do frontmatter (ex.: livro/
            # capítulo/página) — separada do `prefixo` porque este também vira nome
            # de arquivo, e "p. 12-30" tem caracteres inválidos no Windows.
            bloco = normalizar_atomo(bloco, origem or prefixo, agora)
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
                await asyncio.to_thread(lambda a=job_path, b=destino: a.replace(b))
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
        origem = (f"Livro '{titulo_livro}' — {cap} "
                  f"(p. {job.get('pagina_inicio', '?')}-{job.get('pagina_fim', '?')})")
        lotes = livro_mod.fatiar_lotes(job.get("texto", ""), settings.ingestao_lote_chars)
        if not lotes:
            return True   # capítulo vazio: nada a fazer, não bloqueia a fila
        corpos: List[str] = []
        for lote in lotes:
            await self._esperar_idle()
            try:
                conteudo = await self.ctx.llama.collect(
                    prompts.prompt_atomizar_livro(titulo_livro, cap, lote),
                    max_tokens=self._max_fundo(settings.max_tokens_sintese),
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
            await self._salvar_atomos(conteudo, "Livro", "INGESTAO_LIVRO", origem=origem)
        await self._sintese_capitulo(titulo_livro, cap, origem, corpos)
        return True

    async def _sintese_capitulo(self, titulo_livro: str, cap: str, origem: str,
                                corpos: List[str]) -> None:
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
        corpo = f"## Síntese — {titulo_livro}: {cap}\n{sintese.strip()}\n#sintese_capitulo"
        await self._salvar_atomos(corpo, "LivroSintese", "INGESTAO_LIVRO", origem=origem)

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
            n = await asyncio.to_thread(self._enfileirar_jobs, jobs, livro_mod.slug(titulo))
            await self._mover_livro(pdf, "processados")
            telemetry.track("INGESTAO", f"'{titulo}': {n} capítulo(s) enfileirados da pasta vigiada.")
            feitos += 1
        return feitos

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
        tmp = Path(settings.dir_livros) / "_ocr_tmp"
        try:
            imagens = await asyncio.to_thread(
                ocr_mod.render_paginas, pdf, tmp, settings.ocr_dpi,
                inicio, settings.ocr_paginas_por_ciclo)
        except Exception as exc:
            telemetry.error("OCR", f"Falha ao rasterizar {pdf.name}", exc)
            return 0
        feitas = 0
        for img in imagens:
            # Cede a vez ANTES de cada página: o subprocesso é longo (segundos) e a
            # GPU tem de voltar pra conversa se o dono aparecer. O `interactive_idle`
            # é a mesma porta que o resto do ETL respeita.
            if not self.ctx.interactive_idle.is_set():
                telemetry.track("OCR", "Conversa começou — OCR pausado, retoma no próximo idle.")
                break
            comando = ocr_mod.montar_comando(
                settings.ocr_bin, settings.caminho_modelo_ocr, settings.caminho_mmproj_ocr,
                str(img), n_gpu_layers=settings.ocr_n_gpu_layers, n_ctx=settings.n_ctx)
            texto = await asyncio.to_thread(ocr_mod.rodar_ocr, comando, settings.ocr_timeout_pagina)
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
            await asyncio.to_thread(self._salvar_estado_ocr, estado_path, estado)
            await asyncio.to_thread(img.unlink, True)
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
        removidos do índice; só então o canônico é salvo. Devolve grupos fundidos."""
        vs = self.ctx.vectorstore
        if not hasattr(vs, "corpus_com_embeddings"):
            return 0   # fail-open: fakes antigos/índice frio
        corpus = await vs.corpus_com_embeddings()
        if not corpus:
            return 0
        raiz = os.path.normpath(str(settings.dir_conhecimento_novo))
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
            if await self._consolidar_grupo([fontes[i] for i in grupo]):
                fundidos += 1
        if fundidos:
            await self.ctx.vectorstore.sync()
            telemetry.track("CONSOLIDACAO", f"{fundidos} grupo(s) fundido(s) em canônicos.")
        return fundidos

    async def _consolidar_grupo(self, caminhos: List[str]) -> bool:
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
        salvos = await self._salvar_atomos(fundido, "Consolidado", "CONSOLIDACAO", origem=origem)
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

    async def pesquisa_temas_quentes(self) -> None:
        """#4: no idle, RE-PESQUISA na web os temas que o usuário mais REUSA do vault (o
        estágio Banco respondeu de fato) para trazer NOVIDADE. É o ESPELHO da lacuna:
        lacuna = o que FALTOU (banco não cobria); tema quente = o favorito que o banco JÁ
        cobre — por isso, ao contrário da proativa, NÃO há o skip Nível-1 por cobertura do
        banco (o banco sempre cobre; seria pular tudo). O valor vem do dedup por átomo em
        `_salvar_atomos`: só o fato INÉDITO vira nota, o resto é descartado.

        Roda DEPOIS da pesquisa_proativa (buraco genuíno tem prioridade sobre refrescar o
        que já se sabe). Preemptível: se o usuário volta, InferenciaPreemptada encerra e os
        temas não-tocados ficam para a próxima passada de idle. Capado por ciclo."""
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
        await self.process_queue(itens)
        await self.summarize_dump()
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
