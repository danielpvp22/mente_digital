"""OCR de livro escaneado — Fase 3 (2026-07-25).

Sem GPU/binário/PDF real: as decisões são puras e o subprocesso + a rasterização
são fakes. O que se trava: o comando montado (sem shell, caminho com espaço), a
limpeza do stdout do CLI, o NO-OP fail-soft quando o OCR não está configurado, a
RETOMADA por página (estado em disco), o fim do livro virando jobs, a pausa quando
a conversa começa, e o gate do scheduler — incluindo o UNLOAD do LLM antes de rodar
(exigência do dono: nada do projeto na VRAM).
"""
import json
from datetime import datetime
from pathlib import Path

from mente_digital import ocr
from mente_digital.config import settings
from mente_digital.etl import EtlProcessor
from mente_digital.scheduler import SchedulerService
from mente_digital.state import AppContext


# ---------- puro ---------------------------------------------------------------
def test_montar_comando_do_servidor_sem_shell():
    cmd = ocr.montar_comando(r"C:\meu path\llama-server.exe", "m.gguf", "mm.gguf",
                             8099, n_gpu_layers=-1, n_ctx=4096)
    assert cmd[0] == r"C:\meu path\llama-server.exe"        # lista de args, não string
    for flag, valor in (("-m", "m.gguf"), ("--mmproj", "mm.gguf"),
                        ("--port", "8099"), ("-c", "4096")):
        assert cmd[cmd.index(flag) + 1] == valor
    # Servidor EFÊMERO de uso interno: nunca pode escutar na LAN.
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    # Slots concorrentes (1,63x medido); nunca menos de 1.
    assert cmd[cmd.index("--parallel") + 1] == "4"
    assert ocr.montar_comando("b", "m", "mm", 1, paralelo=0)[-1] == "1"


async def test_paralelismo_preserva_a_ORDEM_das_paginas(monkeypatch, tmp_path):
    """As páginas vão em blocos concorrentes; se a ordem escorregar, o livro fica
    embaralhado no vault. O fake devolve um texto por página e o teste exige que o
    estado saia na sequência certa mesmo com a conclusão fora de ordem."""
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=6)
    monkeypatch.setattr(settings, "ocr_paralelo", 3)
    monkeypatch.setattr(settings, "ocr_paginas_por_ciclo", 6)

    import time as _t

    class ForaDeOrdem:
        """Termina as páginas ao CONTRÁRIO dentro do bloco (pior caso da corrida)."""
        def __enter__(self): return self
        def __exit__(self, *e): pass

        def transcrever(self, imagem, *a, **k):
            n = int(imagem.stem[1:])
            _t.sleep(0.02 * (3 - n % 3))   # a última do bloco responde primeiro
            return f"pagina-{n}"

    monkeypatch.setattr("mente_digital.etl.ocr_mod.abrir_servidor",
                        lambda *a, **k: ForaDeOrdem())
    vistas = []
    assert await etl.ocr_livro(pdf, on_pagina=lambda n, t: vistas.append((n, t))) == 6
    assert [n for n, _ in vistas] == [1, 2, 3, 4, 5, 6]
    assert [t for _, t in vistas] == [f"pagina-{i}" for i in range(6)]


def test_payload_leva_imagem_e_prompt_sem_grounding():
    p = ocr.montar_payload("QUJD", max_tokens=64)
    partes = p["messages"][0]["content"]
    assert partes[0]["image_url"]["url"].startswith("data:image/png;base64,QUJD")
    assert partes[1]["text"] == ocr.PROMPT_PADRAO
    assert "<|grounding|>" not in ocr.PROMPT_PADRAO   # senão a saída vem com coordenadas
    assert p["temperature"] == 0 and p["max_tokens"] == 64


def test_extrair_markdown_remove_anotacoes_de_grounding():
    # Medido ao vivo: com <|grounding|> a saída vem como `text[[112, 145, 483, 292]]`.
    bruto = "sub_title[[113, 99, 379, 131]]\n## Classificação\ntext[[112, 145, 483, 292]]\nA ideia de ancestralidade."
    limpo = ocr.extrair_markdown(bruto)
    assert "[[" not in limpo
    assert "## Classificação" in limpo and "A ideia de ancestralidade." in limpo


def test_extrair_markdown_tira_ruido_e_preserva_texto():
    bruto = ("main: load time = 123ms\n"
             "clip_model_loader: loaded meta data\n"
             "<|grounding|>Convert the document to markdown.\n"
             "## Capítulo 1\n"
             "O main do livro fala de disciplina.\n"
             "\n"
             "eval time = 900ms\n")
    limpo = ocr.extrair_markdown(bruto)
    assert limpo.startswith("## Capítulo 1")
    # "main" no MEIO da frase não é log — a linha real do livro sobrevive.
    assert "O main do livro fala de disciplina." in limpo
    assert "eval time" not in limpo and "<|grounding|>" not in limpo


def test_disponibilidade_explica_o_que_falta(tmp_path):
    ok, motivo = ocr.disponibilidade("", "m", "mm")
    assert not ok and "MENTE_OCR_BIN" in motivo
    binario = tmp_path / "cli.exe"
    binario.write_text("x")
    ok, motivo = ocr.disponibilidade(str(binario), str(tmp_path / "nao.gguf"), "mm")
    assert not ok and "GGUF" in motivo
    modelo = tmp_path / "m.gguf"
    modelo.write_text("x")
    ok, motivo = ocr.disponibilidade(str(binario), str(modelo), str(tmp_path / "no.gguf"))
    assert not ok and "mmproj" in motivo
    mm = tmp_path / "mm.gguf"
    mm.write_text("x")
    assert ocr.disponibilidade(str(binario), str(modelo), str(mm)) == (True, "ok")


def test_pagina_util():
    assert not ocr.pagina_util("  ", 40) and ocr.pagina_util("x" * 40, 40)


# ---------- fluxo com fakes ----------------------------------------------------
def _ambiente(monkeypatch, tmp_path, n_paginas=3, configurado=True, saida="texto " * 30):
    monkeypatch.setattr(settings, "dir_livros", str(tmp_path / "livros"))
    monkeypatch.setattr(settings, "dir_ingestao", str(tmp_path / "ingestao"))
    fila = tmp_path / "livros" / "aguardando_ocr"
    fila.mkdir(parents=True)
    pdf = fila / "Livro Escaneado.pdf"
    pdf.write_bytes(b"%PDF-fake")
    if configurado:
        for campo, nome in (("ocr_bin", "cli.exe"), ("caminho_modelo_ocr", "m.gguf"),
                            ("caminho_mmproj_ocr", "mm.gguf")):
            arq = tmp_path / nome
            arq.write_text("x")
            monkeypatch.setattr(settings, campo, str(arq))
    else:
        monkeypatch.setattr(settings, "ocr_bin", "")

    def _render(pdf_, destino, dpi, inicio, quantas):
        destino.mkdir(parents=True, exist_ok=True)
        fim = min(inicio + quantas, n_paginas)
        saidas = []
        for i in range(inicio, fim):
            alvo = destino / f"p{i:04d}.png"
            alvo.write_bytes(b"png")
            saidas.append(alvo)
        return saidas

    chamadas = []

    class FakeServidor:
        """Contrato do ServidorOcr: context manager + transcrever(imagem)."""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fechado = True

        def transcrever(self, imagem, *a, **k):
            chamadas.append(imagem)
            return saida

    monkeypatch.setattr("mente_digital.etl.ocr_mod.total_paginas", lambda p: n_paginas)
    monkeypatch.setattr("mente_digital.etl.ocr_mod.render_paginas", _render)
    monkeypatch.setattr("mente_digital.etl.ocr_mod.abrir_servidor",
                        lambda *a, **k: FakeServidor())
    ctx = AppContext(settings=settings)
    ctx.interactive_idle.set()
    return EtlProcessor(ctx), ctx, pdf, chamadas


async def test_no_op_quando_ocr_nao_configurado(monkeypatch, tmp_path):
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, configurado=False)
    assert await etl.ocr_livros() == 0
    assert chamadas == []          # não tentou nada
    assert pdf.exists()            # e o livro CONTINUA na fila, esperando


async def test_livro_transcrito_vira_jobs_e_sai_da_fila(monkeypatch, tmp_path):
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=3)
    assert await etl.ocr_livros() == 3
    assert len(chamadas) == 3
    assert not pdf.exists()
    assert (Path(settings.dir_livros) / "processados" / pdf.name).exists()
    jobs = list((tmp_path / "ingestao" / "pendentes").glob("ocr-*.json"))
    assert len(jobs) == 1
    assert "texto" in json.loads(jobs[0].read_text(encoding="utf-8"))["texto"]
    assert not etl._ocr_estado_path(pdf).exists()      # estado limpo no fim


async def test_retomada_por_pagina(monkeypatch, tmp_path):
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=5)
    monkeypatch.setattr(settings, "ocr_paginas_por_ciclo", 2)
    assert await etl.ocr_livros() == 2
    estado = json.loads(etl._ocr_estado_path(pdf).read_text(encoding="utf-8"))
    assert estado["proxima"] == 2 and pdf.exists()      # meio do livro: segue na fila
    assert await etl.ocr_livros() == 2                  # retoma de onde parou
    assert json.loads(etl._ocr_estado_path(pdf).read_text(encoding="utf-8"))["proxima"] == 4
    assert await etl.ocr_livros() == 1                  # última página fecha o livro
    assert not pdf.exists() and len(chamadas) == 5      # nenhuma página repetida


async def test_conversa_pausa_o_ocr(monkeypatch, tmp_path):
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=4)
    ctx.interactive_idle.clear()                        # dono falando
    assert await etl.ocr_livros() == 0
    assert chamadas == [] and pdf.exists()


async def test_pagina_sem_texto_nao_derruba_o_livro(monkeypatch, tmp_path):
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=2)
    class SoFalha:
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def transcrever(self, imagem, *a, **k): return None

    monkeypatch.setattr("mente_digital.etl.ocr_mod.abrir_servidor", lambda *a, **k: SoFalha())
    assert await etl.ocr_livros() == 2                  # seguiu nas duas
    # Nada aproveitável -> vai p/ falhou, não fica em loop na fila.
    assert (Path(settings.dir_livros) / "falhou" / pdf.name).exists()


class FakeLlamaVram:
    def __init__(self):
        self.ready = True
        self.unloads = 0

    async def unload(self):
        self.unloads += 1
        self.ready = False


class FakeServicoGpu:
    """STT/TTS/embeddings: descarregam e recarregam, e registram a ORDEM."""

    def __init__(self, log, nome):
        self.ready = True
        self._log = log
        self._nome = nome

    def unload(self):
        self.ready = False
        self._log.append(f"unload:{self._nome}")

    def load(self):
        self.ready = True
        self._log.append(f"load:{self._nome}")


async def test_ocr_roda_com_a_vram_limpa_e_restaura_os_servicos(monkeypatch, tmp_path):
    """Exigência do dono: NADA do projeto na VRAM enquanto o OCR (outro processo/
    venv) roda — e a voz não pode voltar muda depois."""
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=1)
    log = []
    ctx.llama = FakeLlamaVram()
    ctx.stt = FakeServicoGpu(log, "stt")
    ctx.tts = FakeServicoGpu(log, "tts")

    class VS:
        pass

    ctx.vectorstore = VS()
    ctx.vectorstore.embeddings = FakeServicoGpu(log, "emb")
    ctx.etl = etl
    original = etl.ocr_livros

    async def _ocr_espiando():
        # No MOMENTO do OCR, tudo do projeto tem de estar fora da GPU.
        assert ctx.llama.ready is False
        assert not any(s.ready for s in (ctx.stt, ctx.tts, ctx.vectorstore.embeddings))
        log.append("ocr")
        return await original()

    monkeypatch.setattr(etl, "ocr_livros", _ocr_espiando)
    sched = SchedulerService(ctx)
    assert sched._ocr_devido(datetime(2026, 7, 25, 4, 0)) is True
    await sched._executar_ocr()

    assert ctx.llama.unloads == 1
    assert log.index("ocr") > max(log.index(f"unload:{n}") for n in ("stt", "tts", "emb"))
    # Restaurados DEPOIS (o STT não auto-carrega: sem isto a voz ficaria muda).
    assert all(s.ready for s in (ctx.stt, ctx.tts, ctx.vectorstore.embeddings))
    assert ctx.llama.ready is False        # LLM fica lazy (religa no próximo turno)
    assert len(chamadas) == 1              # e transcreveu de fato


async def test_acionamento_manual_transcreve_o_livro_apontado(monkeypatch, tmp_path):
    """scripts/ocr_agora.py: o MESMO caminho do worker idle, só que o dono escolhe a
    hora e o alvo. Aqui trava-se o wrapper público que o script chama."""
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=2)
    assert await etl.ocr_livro(pdf) == 2
    assert not pdf.exists()                     # concluído: saiu da fila
    assert len(list((tmp_path / "ingestao" / "pendentes").glob("ocr-*.json"))) == 1


async def test_progresso_ao_vivo_entrega_cada_pagina(monkeypatch, tmp_path):
    """O acompanhamento em tempo real do dono: callback por página, custo zero de
    GPU (o texto já existe — sem callback ele seria só guardado)."""
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=3, saida="pagina ok")
    vistas = []
    assert await etl.ocr_livro(pdf, on_pagina=lambda n, t: vistas.append((n, t))) == 3
    assert [n for n, _ in vistas] == [1, 2, 3]        # em ordem, uma a uma
    assert all("pagina ok" in t for _, t in vistas)


async def test_callback_quebrado_nao_derruba_a_transcricao(monkeypatch, tmp_path):
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=2)

    def _explode(n, t):
        raise RuntimeError("terminal fechou")

    assert await etl.ocr_livro(pdf, on_pagina=_explode) == 2   # transcreveu mesmo assim


async def test_pdf_com_texto_e_desviado_do_ocr(monkeypatch, tmp_path):
    """Guarda anti-desperdício (caso real 2026-07-25): dois dos três livros do dono
    na fila de OCR JÁ tinham texto — seriam ~4h de GPU para um resultado PIOR."""
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=3)
    monkeypatch.setattr("mente_digital.etl.livro_mod.extrair_pdf",
                        lambda caminho, fonte=None: ([("texto real " * 200)] * 3, []))
    assert await etl.ocr_livro(pdf) == 0
    assert chamadas == []                                        # nenhuma página no OCR
    assert (Path(settings.dir_livros) / "entrada" / pdf.name).exists()  # via digital


async def test_acionamento_manual_recusa_sem_ocr_configurado(monkeypatch, tmp_path):
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=2, configurado=False)
    assert await etl.ocr_livro(pdf) == 0        # motivo vai pro log, nada é tentado
    assert chamadas == [] and pdf.exists()


async def test_scheduler_gates_do_ocr(monkeypatch, tmp_path):
    etl, ctx, pdf, chamadas = _ambiente(monkeypatch, tmp_path, n_paginas=1)
    sched = SchedulerService(ctx)
    agora = datetime(2026, 7, 25, 4, 0)
    ctx.sessoes.add(object())
    assert sched._ocr_devido(agora) is False        # conversa aberta
    ctx.sessoes.clear()
    ctx.interactive_idle.clear()
    assert sched._ocr_devido(agora) is False        # inferência em voo
    ctx.interactive_idle.set()
    pdf.unlink()
    assert sched._ocr_devido(agora) is False        # fila vazia
