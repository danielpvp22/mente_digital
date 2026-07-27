"""Auditoria VISUAL do recorte de figuras: desenha as caixas sobre a pagina.

Por que existe (armadilha paga em 2026-07-25): contagem MENTE. Uma passada com
"807 figuras, 72% com legenda" parecia otima e era feita de retratos de pagina.
So olhando se sabe. Esta e a etapa obrigatoria entre `recortar_figuras.py` e
`reindexar.py`.

Le o RELATORIO da passada (nao redetecta): o que aparece aqui e exatamente o que
foi gravado. Verde = figura recortada; vermelho = caixa rejeitada, com o motivo
escrito ao lado — porque uma rejeicao errada e uma imagem perdida, e o requisito
desta fase e nao perder nenhuma.

Uso:
    python eval/auditar_figuras.py <relatorio.json>              # tudo, 4 por folha
    python eval/auditar_figuras.py <relatorio.json> --por-folha 6 --inicio 100
    python eval/auditar_figuras.py <relatorio.json> --so-secas   # paginas sem figura
"""
import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

VERDE = (0, 170, 0)
VERMELHO = (220, 0, 0)
LARANJA = (230, 130, 0)


def _rotulo(desenho, xy, texto, cor, fonte):
    """Texto com tarja branca atras — sobre uma figura escura, letra sem fundo
    some, e uma auditoria que nao se le nao audita nada."""
    x, y = xy
    try:
        caixa = desenho.textbbox((x, y), texto, font=fonte)
        desenho.rectangle([caixa[0] - 3, caixa[1] - 2, caixa[2] + 3, caixa[3] + 2],
                          fill=(255, 255, 255))
    except Exception:
        pass
    desenho.text((x, y), texto, fill=cor, font=fonte)


def folha(paginas_doc, registros, destino, escala, fonte):
    import io

    from PIL import Image, ImageDraw

    miniaturas = []
    for reg, pagina in zip(registros, paginas_doc):
        pix = pagina.get_pixmap(dpi=150)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        # A caixa foi calculada sobre a pagina rasterizada no MESMO dpi; se o
        # relatorio guardou outra dimensao, reescala em vez de desenhar torto.
        fx = pix.width / max(1, reg.get("largura") or pix.width)
        fy = pix.height / max(1, reg.get("altura") or pix.height)
        d = ImageDraw.Draw(img)
        for i, f in enumerate(reg.get("figuras", []), 1):
            x0, y0, x1, y1 = (int(v * (fx if j % 2 == 0 else fy))
                              for j, v in enumerate(f["ret"]))
            d.rectangle([x0, y0, x1, y1], outline=VERDE, width=8)
            leg = (f.get("legenda") or "")[:40]
            _rotulo(d, (x0 + 10, y0 + 8), f"f{i}" + (f" {leg}" if leg else ""),
                    VERDE, fonte)
        for rj in reg.get("rejeitadas", []):
            cx = rj["caixa"]
            x0, y0, x1, y1 = (int(cx[0] / 999 * pix.width), int(cx[1] / 999 * pix.height),
                              int(cx[2] / 999 * pix.width), int(cx[3] / 999 * pix.height))
            d.rectangle([x0, y0, x1, y1], outline=VERMELHO, width=5)
            _rotulo(d, (x0 + 8, max(0, y0 - 26)), f"X {rj['motivo']}", VERMELHO, fonte)
        miniaturas.append((reg, img.resize((img.width // escala, img.height // escala))))

    larg = max(m.width for _, m in miniaturas)
    alt = max(m.height for _, m in miniaturas)
    cols = min(4, len(miniaturas))
    linhas = (len(miniaturas) + cols - 1) // cols
    saida = Image.new("RGB", (cols * (larg + 12), linhas * (alt + 30)), (250, 250, 250))
    esc = ImageDraw.Draw(saida)
    for i, (reg, m) in enumerate(miniaturas):
        x, y = (i % cols) * (larg + 12), (i // cols) * (alt + 30)
        aviso = " [TRUNCOU]" if reg.get("truncou") else ""
        aviso += " [ERRO]" if reg.get("erro") else ""
        cor = LARANJA if aviso else (0, 0, 0)
        esc.text((x + 6, y + 8), f"p{reg['pagina']} — {len(reg.get('figuras', []))} "
                 f"figura(s){aviso}", fill=cor, font=fonte)
        saida.paste(m, (x, y + 26))
    saida.save(destino)
    return destino


def main() -> int:
    import fitz
    from PIL import ImageFont

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("relatorio")
    ap.add_argument("--por-folha", type=int, default=4)
    ap.add_argument("--inicio", type=int, default=0, help="pula as N primeiras paginas")
    ap.add_argument("--quantas", type=int, default=0, help="0 = todas")
    ap.add_argument("--escala", type=int, default=2, help="reducao da miniatura")
    ap.add_argument("--so-secas", action="store_true",
                    help="so as paginas SEM figura (caca ao falso negativo)")
    ap.add_argument("--so-rejeitadas", action="store_true",
                    help="so as paginas com caixa rejeitada (caca ao filtro errado)")
    ap.add_argument("--saida", default="")
    args = ap.parse_args()

    dados = json.loads(Path(args.relatorio).read_text(encoding="utf-8"))
    regs = dados["paginas"]
    if args.so_secas:
        regs = [r for r in regs if not r.get("figuras")]
    if args.so_rejeitadas:
        regs = [r for r in regs if r.get("rejeitadas")]
    regs = regs[args.inicio:]
    if args.quantas:
        regs = regs[:args.quantas]
    if not regs:
        print("nada a auditar com esse filtro")
        return 0

    pasta = Path(args.saida) if args.saida else Path(args.relatorio).with_suffix("")
    pasta.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(Path("dados/livros/processados") / dados["pdf"]))
    try:
        fonte = ImageFont.load_default()
        try:
            fonte = ImageFont.truetype("arial.ttf", 34)
        except Exception:
            pass
        feitas = []
        for i in range(0, len(regs), args.por_folha):
            lote = regs[i:i + args.por_folha]
            paginas = [doc.load_page(r["pagina"] - 1) for r in lote]
            destino = pasta / f"p{lote[0]['pagina']:04d}-{lote[-1]['pagina']:04d}.png"
            feitas.append(folha(paginas, lote, destino, args.escala, fonte))
            print(f"  {destino} ({sum(len(r.get('figuras', [])) for r in lote)} figuras)")
    finally:
        doc.close()
    total = sum(len(r.get("figuras", [])) for r in regs)
    secas = sum(1 for r in regs if not r.get("figuras"))
    print(f"\n{len(feitas)} folha(s) em {pasta} | {len(regs)} paginas, {total} figuras, "
          f"{secas} sem figura")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
