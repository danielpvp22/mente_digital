"""OCR de livros escaneados — Fase 3 (2026-07-25).

O caso: um livro que é só IMAGEM (sem texto selecionável). A Fase 1 o detecta e o
manda para `dados/livros/aguardando_ocr/`; aqui ele vira texto e volta para a MESMA
fila de jobs — proveniência e síntese hierárquica saem de graça, o pipeline a
jusante não muda em nada.

POR QUE SUBPROCESSO, e não import: o modelo exige Python 3.12 + torch 2.10 + CUDA
12.9, e a env do projeto é 3.10 com transformers travado <5 pelo coqui/XTTS —
importar é impossível. Rodamos o llama.cpp como processo separado: zero
contaminação de dependência e VRAM devolvida ao fim.

POR QUE `llama-server` E NÃO `llama-mtmd-cli` (medido nesta máquina, 2026-07-25):
o `llama-mtmd-cli` faz fail-fast (0xC0000409, ZERO saída) assim que recebe
`--mmproj` — inclusive com um caminho de mmproj INEXISTENTE, na CPU, em dois
builds (b9977 e b10107), no terminal do dono e sem antivírus, sem registro no
Visor de Eventos. Já o `llama-server` do MESMO pacote carrega o mesmo par
modelo+mmproj em 2s e transcreve uma página em ~3,5s. Além de funcionar, o
servidor é melhor: o modelo carrega UMA vez para o livro inteiro, em vez de a
cada página (o custo dominante do CLI seria recarregar 3 GB 600 vezes).

MODELO (verificado em 2026-07-25): usar o `ggml-org/DeepSeek-OCR-GGUF` OFICIAL,
publicado junto com o suporte no llama.cpp (PR "mtmd: Add DeepSeekOCR Support",
mergeada em 2026-03-25 — qualquer release posterior serve, sem compilar nada). O
`baidu/Unlimited-OCR-GGUF` NÃO serve: ele até carrega no servidor, mas devolve
conteúdo VAZIO (1 token, finish_reason=stop) porque depende da PR 24975, que
segue fora do master — o card dele avisa, apesar de citar o número errado. Ainda assim
`disponibilidade()` checa tudo ANTES de tentar e o worker é NO-OP silencioso
enquanto `MENTE_OCR_BIN` não apontar para um binário que existe: quem não
configurou não sofre nada, e o livro fica esperando na fila sem se perder.

VRAM: roda só quando NADA do projeto está na GPU (o scheduler descarrega o LLM
antes) — exigência do dono, e o que torna 3 GB de OCR viáveis na 3080.

Estilo do módulo: decisões puras (comando, limpeza da saída, disponibilidade) +
um punhado de funções de IO claramente marcadas no fim.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

# Prompt de transcrição. SEM `<|grounding|>`: aquele token liga o modo com
# COORDENADAS — a saída vem cheia de `text[[112, 145, 483, 292]]` (medido), que
# viraria lixo dentro dos átomos. Para OCR de livro queremos só a prosa.
PROMPT_PADRAO = "Convert the document to markdown."

# Os dois prompts do RECORTE DE FIGURAS (Fase 5c), onde as coordenadas são
# justamente o produto. Ficam aqui, e não no script, porque são contrato com o
# modelo — quem trocar o GGUF precisa reconferir os dois no mesmo lugar.
#
# O de markdown transcreve a página inteira e por isso traz a `image_caption`
# pareada com cada `image`; o de localização devolve SÓ o layout (300 chars
# contra 6.000), então é 3-4x mais rápido e nunca trunca. Rodam os dois: medido
# em 2026-07-25, cada um achou uma figura que o outro perdeu.
PROMPT_GROUNDING_MD = "<|grounding|>Convert the document to markdown."
PROMPT_LOCALIZAR_FIGURA = "Locate <|ref|>figure<|/ref|> in the image."

# Rede de segurança: se algum caminho ainda produzir as anotações de grounding,
# elas somem aqui em vez de poluir o Zettelkasten.
_GROUNDING_RE = re.compile(r"^\s*\w+\[\[[\d,\s]+\]\]\s*", re.MULTILINE)

# Ruído que o llama-mtmd-cli mistura no stdout junto com o texto gerado. Casado no
# INÍCIO da linha (prefixo) para nunca comer uma linha real do livro que por acaso
# contenha uma dessas palavras no meio.
_PREFIXOS_RUIDO = (
    "main:", "llama_", "llm_load", "clip_", "load_tensors", "ggml_", "build:",
    "encoding image", "decoding image", "image encoded", "system_info",
    "sampler ", "generate:", "print_info", "init:", "common_", "warning:",
    "srv ", "eval time", "total time", "load time", "prompt eval",
)


def disponibilidade(bin_path: str, modelo: str, mmproj: str) -> Tuple[bool, str]:
    """(ok, motivo). Checa o trio ANTES de qualquer trabalho — o motivo vai pro log
    uma vez, para o dono saber exatamente o que falta configurar."""
    if not bin_path:
        return False, ("MENTE_OCR_BIN não configurado (aponte para o llama-server.exe "
                       "de um llama.cpp >= 2026-03-25, quando o suporte entrou)")
    if not Path(bin_path).is_file():
        return False, f"binário não encontrado: {bin_path}"
    if not Path(modelo).is_file():
        return False, f"GGUF do OCR não encontrado: {modelo}"
    if not Path(mmproj).is_file():
        return False, f"mmproj não encontrado: {mmproj} (obrigatório p/ o modelo de visão)"
    return True, "ok"


def montar_comando(bin_path: str, modelo: str, mmproj: str, porta: int,
                   n_gpu_layers: int = -1, n_ctx: int = 16384,
                   paralelo: int = 4) -> List[str]:
    """Args do `llama-server` multimodal. Puro (lista, sem shell — caminho com
    espaço, comum no Windows, não vira injeção nem quebra). Sobe em 127.0.0.1:
    é um servidor efêmero de uso interno, não pode escutar na LAN.

    `paralelo` = slots simultâneos: 4 páginas concorrentes deram 1,63x de throughput
    (medido 2026-07-25) sem duplicar os pesos na VRAM."""
    return [
        bin_path,
        "-m", modelo,
        "--mmproj", mmproj,
        "--host", "127.0.0.1",
        "--port", str(porta),
        "-ngl", str(n_gpu_layers),
        "-c", str(n_ctx),
        "--parallel", str(max(1, paralelo)),
    ]


def montar_payload(imagem_b64: str, prompt: str = PROMPT_PADRAO,
                   max_tokens: int = 2048) -> dict:
    """Corpo da chamada /v1/chat/completions com a página como imagem. Puro."""
    return {
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + imagem_b64}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }


def extrair_markdown(stdout: str) -> str:
    """Tira o ruído do CLI e devolve só o texto da página. Conservador: descarta a
    linha apenas quando ela COMEÇA com um prefixo conhecido de log."""
    linhas = []
    for ln in (stdout or "").splitlines():
        alvo = ln.strip()
        if not alvo:
            linhas.append("")
            continue
        if any(alvo.lower().startswith(p) for p in _PREFIXOS_RUIDO):
            continue
        linhas.append(ln.rstrip())
    texto = "\n".join(linhas).strip()
    texto = re.sub(r"^\s*<\|grounding\|>[^\n]*\n?", "", texto)   # eco do prompt
    return _GROUNDING_RE.sub("", texto).strip()


def pagina_util(texto: str, min_chars: int) -> bool:
    """Página com texto de menos é capa/ilustração/falha — não conta como conteúdo,
    mas TAMBÉM não é erro (livro tem página em branco)."""
    return len(texto.strip()) >= min_chars


# --- IO / subprocesso (o resto do módulo é puro) -----------------------------
def render_paginas(pdf: Path, destino: Path, dpi: int,
                   inicio: int, quantas: int) -> List[Path]:
    """Rasteriza [inicio, inicio+quantas) em PNG. Import TARDIO do PyMuPDF, como no
    livro.py — o servidor sobe sem ele."""
    import fitz

    destino.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf))
    try:
        fim = min(inicio + quantas, doc.page_count)
        saidas = []
        for i in range(inicio, fim):
            alvo = destino / f"p{i:04d}.png"
            doc.load_page(i).get_pixmap(dpi=dpi).save(str(alvo))
            saidas.append(alvo)
        return saidas
    finally:
        doc.close()


def total_paginas(pdf: Path) -> int:
    import fitz

    doc = fitz.open(str(pdf))
    try:
        return doc.page_count
    finally:
        doc.close()


class ServidorOcr:
    """Sobe o `llama-server` UMA vez e transcreve N páginas nele.

    Context manager: o processo morre no `__exit__` aconteça o que acontecer
    (barge-in, erro, fim do lote), devolvendo a VRAM. Cada página é um POST — o
    modelo já está quente, então o custo por página é só encode da imagem +
    decode (~3,5s medido), contra recarregar 3 GB por página num CLI por chamada.
    """

    def __init__(self, comando: List[str], porta: int, timeout_pagina: int,
                 espera_boot: int = 180) -> None:
        self._comando = comando
        self._porta = porta
        self._timeout = timeout_pagina
        self._espera = espera_boot
        self._proc = None

    @property
    def _base(self) -> str:
        return f"http://127.0.0.1:{self._porta}"

    def __enter__(self) -> "ServidorOcr":
        # B404/B603: subprocesso é o MECANISMO desta fase (env incompatível, ver a
        # docstring do módulo); o comando é uma LISTA montada por `montar_comando`,
        # sem shell — nada vindo do usuário vira argumento.
        import subprocess  # nosec B404
        import time
        import urllib.request

        # DEVNULL: o log do servidor é verboso e, sem ninguém drenando o pipe, o
        # buffer encheria e travaria o processo no meio do livro.
        self._proc = subprocess.Popen(  # nosec B603
            self._comando, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(Path(self._comando[0]).parent),
        )
        limite = time.monotonic() + self._espera
        while time.monotonic() < limite:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"llama-server morreu no boot (exit={self._proc.returncode})")
            try:
                with urllib.request.urlopen(self._base + "/health", timeout=2):  # nosec B310
                    return self
            except Exception:
                time.sleep(1.0)
        self.__exit__(None, None, None)
        raise RuntimeError("llama-server não respondeu ao /health a tempo")

    def __exit__(self, *exc) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=20)
        except Exception:
            self._proc.kill()
        self._proc = None

    def responder(self, imagem: bytes, prompt: str,
                  max_tokens: int = 4096) -> Tuple[str, str]:
        """Chamada CRUA: devolve (conteúdo, motivo_de_parada), sem limpar nada.

        Existe para o recorte de figuras (Fase 5c), que precisa do oposto do que
        `transcrever` faz: lá as anotações `image[[x0,y0,x1,y1]]` são LIXO a
        remover, aqui são o produto.

        E devolve o `finish_reason` porque ele é a única forma de saber que a
        página foi CORTADA no meio: numa página densa o modelo gasta os tokens
        transcrevendo o texto e para antes de chegar às figuras do rodapé. Sem
        esse sinal, a página perdida seria indistinguível de uma página sem
        figura — o pior modo de falha para um pipeline cujo requisito é não
        deixar nenhuma imagem passar.

        Recebe BYTES e não um caminho de propósito: a página já está em memória e
        gravar um PNG temporário por chamada foi fonte de bug (duas threads
        sobrescrevendo o mesmo arquivo e o OCR lendo a figura errada)."""
        import base64
        import json
        import urllib.request

        b64 = base64.b64encode(imagem).decode()
        corpo = json.dumps(montar_payload(b64, prompt, max_tokens)).encode()
        req = urllib.request.Request(
            self._base + "/v1/chat/completions", data=corpo,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self._timeout) as r:  # nosec B310
            dados = json.loads(r.read())
        escolha = (dados.get("choices") or [{}])[0]
        return ((escolha.get("message") or {}).get("content") or "",
                escolha.get("finish_reason") or "")

    def transcrever(self, imagem: Path, prompt: str = PROMPT_PADRAO,
                    max_tokens: int = 2048) -> Optional[str]:
        """Uma página → markdown. None em qualquer falha: OCR é trabalho de fundo,
        uma página ruim não pode derrubar o livro inteiro."""
        import base64
        import json
        import urllib.request

        try:
            b64 = base64.b64encode(imagem.read_bytes()).decode()
            corpo = json.dumps(montar_payload(b64, prompt, max_tokens)).encode()
            req = urllib.request.Request(
                self._base + "/v1/chat/completions", data=corpo,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self._timeout) as r:  # nosec B310
                dados = json.loads(r.read())
            return extrair_markdown(dados["choices"][0]["message"]["content"])
        except Exception:
            return None


def abrir_servidor(bin_path: str, modelo: str, mmproj: str, porta: int,
                   timeout_pagina: int, n_gpu_layers: int = -1,
                   n_ctx: int = 16384, paralelo: int = 4) -> ServidorOcr:
    """Fábrica do servidor efêmero (uso: `with abrir_servidor(...) as s:`)."""
    return ServidorOcr(
        montar_comando(bin_path, modelo, mmproj, porta, n_gpu_layers, n_ctx, paralelo),
        porta, timeout_pagina,
    )
