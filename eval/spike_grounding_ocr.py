"""Spike: o <|grounding|> do DeepSeek-OCR marca FIGURA na pagina?

Manda a p550 do Amabis (a que tem duas figuras anatomicas que o detector
heuristico perdeu) e imprime a saida CRUA, para ver se vem caixa rotulada.
"""
import sys
from pathlib import Path

RAIZ = Path(r"D:\projetos\mente_digital")
sys.path.insert(0, str(RAIZ))
import os  # noqa: E402

os.chdir(RAIZ)
from mente_digital import ocr  # noqa: E402
from mente_digital.config import settings  # noqa: E402

TMP = Path(__file__).parent / "spike"
TMP.mkdir(exist_ok=True)
PAGINAS = {"amabis": 550, "cervantes": 250}

from scripts.recortar_figuras import DIR_LIVROS, LIVROS  # noqa: E402

alvos = []
for livro, pag in PAGINAS.items():
    import fitz

    doc = fitz.open(stream=(DIR_LIVROS / LIVROS[livro]["pdf"]).read_bytes(), filetype="pdf")
    destino = TMP / f"{livro}_p{pag}.png"
    doc.load_page(pag - 1).get_pixmap(dpi=150).save(str(destino))
    doc.close()
    alvos.append((livro, pag, destino))

ok, motivo = ocr.disponibilidade(settings.ocr_bin, settings.caminho_modelo_ocr,
                                 settings.caminho_mmproj_ocr)
print("OCR disponivel:", ok, motivo)
if not ok:
    raise SystemExit(1)

PROMPT = "<|grounding|>Convert the document to markdown."
with ocr.abrir_servidor(settings.ocr_bin, settings.caminho_modelo_ocr,
                        settings.caminho_mmproj_ocr, settings.ocr_porta,
                        settings.ocr_timeout_pagina, settings.ocr_n_gpu_layers,
                        settings.ocr_n_ctx, 1) as servidor:
    for livro, pag, img in alvos:
        # transcrever() LIMPA o grounding (e o que queremos ver); chamamos o
        # endpoint por baixo para ver o CRU.
        import base64
        import json
        import urllib.request

        b64 = base64.b64encode(img.read_bytes()).decode()
        corpo = json.dumps(ocr.montar_payload(b64, PROMPT, 4096)).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{settings.ocr_porta}/v1/chat/completions",
            data=corpo, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=settings.ocr_timeout_pagina) as r:
            dados = json.loads(r.read())
        bruto = dados["choices"][0]["message"]["content"]
        print(f"\n{'='*70}\n{livro} p{pag} — {len(bruto)} chars\n{'='*70}")
        print(bruto[:2500])
