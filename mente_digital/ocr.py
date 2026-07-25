"""OCR de livros escaneados — Fase 3 (2026-07-25).

O caso: um livro que é só IMAGEM (sem texto selecionável). A Fase 1 o detecta e o
manda para `dados/livros/aguardando_ocr/`; aqui ele vira texto e volta para a MESMA
fila de jobs — proveniência e síntese hierárquica saem de graça, o pipeline a
jusante não muda em nada.

POR QUE SUBPROCESSO, e não import: o modelo (baidu/Unlimited-OCR) exige Python
3.12 + torch 2.10 + CUDA 12.9, e a env do projeto é 3.10 com transformers travado
<5 pelo coqui/XTTS — importar é impossível. A saída é o binário `llama-mtmd-cli`
do llama.cpp com o GGUF quantizado (Q4_K_M ~1,95 GB + mmproj ~774 MB): processo
separado, VRAM devolvida ao fim de cada página, zero contaminação de dependência.

ATENÇÃO (verificado no card do modelo em 2026-07-25): o GGUF exige um llama.cpp
compilado com a PR #17400 — não funciona com o release padrão. Por isso
`disponibilidade()` checa tudo ANTES de tentar e o worker é NO-OP silencioso
enquanto `MENTE_OCR_BIN` não apontar para um binário que existe: quem não
configurou não sofre nada, e o livro fica esperando na fila sem se perder.

VRAM: roda só quando NADA do projeto está na GPU (o scheduler descarrega o LLM
antes) — exigência do dono, e o que torna 3 GB de OCR viáveis na 3080.

Estilo do módulo: decisões puras (comando, limpeza da saída, disponibilidade) +
um punhado de funções de IO claramente marcadas no fim.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

# Prompt do card do modelo. `<|grounding|>` é o token que liga o modo de parsing
# de documento — sem ele o modelo descreve a imagem em vez de transcrevê-la.
PROMPT_PADRAO = "<|grounding|>Convert the document to markdown."

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
        return False, ("MENTE_OCR_BIN não configurado (aponte para o llama-mtmd-cli "
                       "de um llama.cpp com a PR #17400)")
    if not Path(bin_path).is_file():
        return False, f"binário não encontrado: {bin_path}"
    if not Path(modelo).is_file():
        return False, f"GGUF do OCR não encontrado: {modelo}"
    if not Path(mmproj).is_file():
        return False, f"mmproj não encontrado: {mmproj} (obrigatório p/ o modelo de visão)"
    return True, "ok"


def montar_comando(bin_path: str, modelo: str, mmproj: str, imagem: str,
                   prompt: str = PROMPT_PADRAO, n_gpu_layers: int = -1,
                   n_ctx: int = 8192) -> List[str]:
    """Comando do llama-mtmd-cli para UMA página. Puro (lista de args, sem shell —
    caminho com espaço, comum no Windows, não vira injeção nem quebra)."""
    return [
        bin_path,
        "-m", modelo,
        "--mmproj", mmproj,
        "--image", imagem,
        "-p", prompt,
        "--temp", "0",
        "-ngl", str(n_gpu_layers),
        "-c", str(n_ctx),
    ]


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
    # O CLI às vezes ecoa o prompt antes da resposta.
    return re.sub(r"^\s*<\|grounding\|>[^\n]*\n?", "", texto).strip()


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


def rodar_ocr(comando: List[str], timeout: int) -> Optional[str]:
    """Executa o CLI numa página. None em qualquer falha (o chamador loga e segue):
    OCR é trabalho de fundo, uma página ruim não pode derrubar o livro inteiro."""
    # B404/B603: subprocesso é o MECANISMO desta fase (env incompatível, ver docstring);
    # o comando é uma LISTA montada por `montar_comando`, sem shell — nada de entrada
    # do usuário vira argumento (o único dado externo é o caminho do PNG que nós geramos).
    import subprocess  # nosec B404

    try:
        proc = subprocess.run(  # nosec B603
            comando, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            env={**os.environ, "GGML_LOG_LEVEL": "2"},
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return extrair_markdown(proc.stdout)
