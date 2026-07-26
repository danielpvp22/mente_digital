"""Recorta as figuras de um livro ESCANEADO e as torna achaveis — Fase 5c.

Por que existe (medido em 2026-07-25): `figuras.extrair_de_pdf` pega a imagem
EMBUTIDA, o que so vale em PDF digital (Raven: 1008 figuras). Nos dois livros
escaneados a imagem embutida e a PAGINA inteira fotografada — Amabis 1/pagina de
1011x1546, Cervantes 1/pagina de 783x1236 — e a guarda daquele modulo apaga tudo,
com razao: retrato de pagina nao ilustra nada. Aqui a figura e ACHADA dentro da
pagina e RECORTADA.

COMO a figura e achada: pelo `<|grounding|>` do proprio DeepSeek-OCR da Fase 3,
que devolve o layout rotulado da pagina (`image[[x0,y0,x1,y1]]` em 0-999) com a
legenda ja PAREADA. Isso substituiu, em 2026-07-25, uma heuristica de grade de
tinta/cor/textura que errava nos dois sentidos depois de tres calibracoes — ver a
lapide na docstring de `mente_digital/figuras_recorte.py`.

DUAS PASSADAS por pagina, porque o requisito e nao deixar UMA imagem passar:
  1. markdown com grounding — traz as legendas, mas gasta tokens transcrevendo o
     texto e pode ser CORTADO antes das figuras do rodape (o script detecta o
     corte pelo `finish_reason` e repete com o dobro de tokens);
  2. localizacao de figura — devolve so caixas, e portanto nunca trunca.
A uniao das duas (por IoU) e o que fecha o recall.

E, principalmente: cada recorte vira uma NOTA com legenda e ancora de contexto
(pagina + capitulo + temas). Sem isso a figura fica invisivel para o RAG.

Uso — o `--amostra`/`--saida` existem para OLHAR antes de comprometer 1.140
paginas, e nenhuma delas escreve no vault:
    python scripts/recortar_figuras.py --livro cervantes --amostra 24 --saida /tmp/amostra
    python scripts/recortar_figuras.py --livro cervantes --paginas 250,157,33-40
    python scripts/recortar_figuras.py --livro cervantes            # passada real
Depois: `python eval/auditar_figuras.py <relatorio.json>` desenha as caixas sobre
as paginas para conferencia visual, e SO ENTAO `python scripts/reindexar.py`.

Usa a GPU: nao rode com a atomizacao em andamento (o script avisa e para).
"""
import argparse
import io
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)                      # o pydantic le o .env relativo ao CWD

from mente_digital import atomos, figuras, figuras_recorte as fr, ocr  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.telemetry import telemetry  # noqa: E402

DIR_LIVROS = Path("dados/livros/processados")

# O slug TEM de ser o mesmo que os jobs ja usaram, senao o wikilink de amanha nao
# casa com a pasta de hoje. O do Amabis carrega o prefixo "ocr-" que o worker da
# Fase 3 poe no titulo; nao e cosmetico, e a identidade do livro no vault.
LIVROS = {
    "cervantes": {
        "pdf": "Cervantes.Marijuana Horticulture Medical Growers Bible_pages reordered_ocr.pdf",
        "slug": "cervantesmarijuana-horticulture-medical",
        "titulo": "Cervantes — Marijuana Horticulture (Medical Grower's Bible)",
    },
    "amabis": {
        "pdf": "Biologia dos Organismos Volume 2 (AMABIS E MARTHO).pdf",
        "slug": "ocr-biologia-dos-organismos-volume-2-amabis",
        "titulo": "Biologia dos Organismos Volume 2 (Amabis e Martho)",
    },
}

# Respiro em volta da caixa, em fracao do tamanho da propria figura: a caixa do
# modelo encosta no traco, e os dois livros rasterizam em larguras bem diferentes
# (1241 e 1773 px), entao margem fixa em pixels seria generosa num e rente no outro.
# 5% e nao 2%: com 2% a auditoria de 2026-07-26 achou uma duzia de recortes com a
# borda passando POR DENTRO do rotulo da figura ("Estria casparina em|corte
# longitudinal", "Fra|gmentos de folha"). Sobrar um fiapo de pagina nao custa nada;
# faltar meio rotulo estraga a figura.
MARGEM_FRAC = 0.05

_local = threading.local()


def _doc(pdf_bytes: bytes):
    """Um `fitz.Document` POR THREAD. Documento do PyMuPDF nao e thread-safe, e
    compartilhar um so entre os slots do OCR corrompe a rasterizacao."""
    import fitz

    if getattr(_local, "doc", None) is None:
        _local.doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return _local.doc


def _png_da_pagina(doc, pno: int, dpi: int) -> Tuple[bytes, int, int]:
    """Pagina rasterizada -> (PNG em memoria, largura, altura). Sem arquivo
    temporario: o caminho compartilhado ja causou threads lendo a imagem errada."""
    pix = doc.load_page(pno).get_pixmap(dpi=dpi)
    return pix.tobytes("png"), pix.width, pix.height


def _recortar(png: bytes, ret: Tuple[int, int, int, int]) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").crop(ret).save(buf, format="PNG")
    return buf.getvalue()


def _com_retomada(servidor, png: bytes, prompt: str, max_tokens: int,
                  tetos: int = 2) -> Tuple[str, bool]:
    """Chama o modelo e REPETE com o dobro de tokens enquanto a saida vier cortada.

    O corte e o unico modo de falha silencioso desta fase: uma pagina densa que
    estoura o limite para de emitir ANTES das figuras do rodape, e o resultado e
    indistinguivel de uma pagina sem figura. Devolve (bruto, truncou_ate_o_fim)."""
    for tentativa in range(tetos + 1):
        bruto, motivo = servidor.responder(png, prompt, max_tokens << tentativa)
        if motivo != "length":
            return bruto, False
    return bruto, True


def _texto_por_pagina(slug: str) -> List[Tuple[int, int, str, str]]:
    """(pagina_inicio, pagina_fim, titulo_cap, texto) dos jobs deste livro.

    E a FONTE DE VERDADE contra a qual a legenda e conferida: o texto que o worker
    da Fase 3 extraiu do proprio livro, numa passada INDEPENDENTE desta. Sem isso
    nao ha como separar legenda real de invencao do modelo."""
    faixas = []
    for pasta in ("processados", "pendentes"):
        for f in (Path(settings.dir_ingestao) / pasta).glob(f"{slug}__*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            faixas.append((int(d.get("pagina_inicio", 0)), int(d.get("pagina_fim", 0)),
                           d.get("titulo_cap") or "", d.get("texto") or ""))
    return faixas


def _da_pagina(faixas, pagina: int, indice: int) -> str:
    for ini, fim, cap, texto in faixas:
        if ini <= pagina <= fim:
            return (cap, texto)[indice]
    return ""


def _fila_ocupada() -> bool:
    return any((Path(settings.dir_ingestao) / "pendentes").glob("*.json"))


def _paginas_alvo(total: int, amostra: int, escolhidas: str) -> List[int]:
    """Indices 0-based das paginas a processar."""
    if escolhidas:
        saida: List[int] = []
        for parte in escolhidas.split(","):
            parte = parte.strip()
            if "-" in parte:
                a, b = parte.split("-", 1)
                saida.extend(range(int(a) - 1, int(b)))
            elif parte:
                saida.append(int(parte) - 1)
        return [p for p in saida if 0 <= p < total]
    if amostra:
        # Espalhado pelo miolo: capa e sumario nao representam o livro.
        return sorted({int(total * (0.10 + 0.80 * i / max(1, amostra - 1)))
                       for i in range(amostra)})
    return list(range(total))


def _detectar(servidor, pdf_bytes: bytes, pno: int, dpi: int,
              max_tokens: int) -> dict:
    """UMA pagina: rasteriza, roda as duas passadas, consolida. So leitura."""
    png, larg, alt = _png_da_pagina(_doc(pdf_bytes), pno, dpi)
    bruto_md, cortou = _com_retomada(servidor, png, ocr.PROMPT_GROUNDING_MD, max_tokens)
    bruto_loc, _ = _com_retomada(servidor, png, ocr.PROMPT_LOCALIZAR_FIGURA, 1024)
    aceitas, rejeitadas = fr.consolidar(bruto_md, bruto_loc)

    # RESGATE por coluna: a pagina que so produziu caixa-gigante e uma pagina
    # sobre a qual ainda nao se sabe nada — o modelo desistiu de segmentar em vez
    # de errar de pouco (medido na p325 do Cervantes, 3 figuras perdidas, e na
    # p510 do Amabis, a Figura 17.4 inteira). Reperguntar coluna a coluna devolve
    # a proporcao normal de uma "pagina" e o modelo volta a segmentar. So roda nas
    # paginas doentes, entao nao pesa na passada do livro.
    resgates = []
    if fr.precisa_resgate(aceitas, rejeitadas, cortou):
        for faixa in fr.FAIXAS_RESGATE:
            recorte = _recortar(png, faixa.para_pixels(larg, alt))
            bruto, _ = _com_retomada(servidor, recorte, ocr.PROMPT_GROUNDING_MD,
                                     max_tokens)
            resgates.append((bruto, faixa))
        aceitas, rejeitadas = fr.consolidar(bruto_md, bruto_loc, resgates)

    return {
        "pagina": pno + 1, "largura": larg, "altura": alt, "png": png,
        "truncou": cortou, "resgatada": bool(resgates),
        "texto_ocr": fr.texto_do_grounding(bruto_md),
        "figuras": [{"caixa": list(f.caixa),
                     "ret": list(f.caixa.para_pixels(larg, alt, MARGEM_FRAC)),
                     "legenda_crua": f.legenda} for f in aceitas],
        "rejeitadas": [{"caixa": list(f.caixa), "motivo": m} for f, m in rejeitadas],
    }


def processar(livro: str, dpi: int, amostra: int, escolhidas: str, dry_run: bool,
              limite: int, saida: Optional[str], relatorio: Optional[str],
              max_tokens: int, paralelo: int, n_ctx: int) -> int:
    cfg = LIVROS[livro]
    pdf = DIR_LIVROS / cfg["pdf"]
    if not pdf.is_file():
        print(f"PDF nao encontrado: {pdf}")
        return 2
    ok, motivo = ocr.disponibilidade(settings.ocr_bin, settings.caminho_modelo_ocr,
                                     settings.caminho_mmproj_ocr)
    if not ok:
        print(f"OCR indisponivel: {motivo}")
        return 2
    if _fila_ocupada():
        print("Ha capitulos na fila de atomizacao — o OCR disputaria a GPU. Abortado.")
        return 3

    destino = Path(saida) / cfg["slug"] if saida else settings.dir_figuras / cfg["slug"]
    pdf_bytes = pdf.read_bytes()
    total = ocr.total_paginas(pdf)
    paginas = _paginas_alvo(total, amostra, escolhidas)
    print(f"{livro}: {len(paginas)} de {total} pagina(s) | dpi={dpi} | "
          f"{paralelo} em paralelo | destino={destino}", flush=True)

    resultados: Dict[int, dict] = {}
    feitas, trava = [0], threading.Lock()

    def _uma(pno: int) -> None:
        try:
            r = _detectar(servidor, pdf_bytes, pno, dpi, max_tokens)
        except Exception as exc:
            # A pagina que falha NAO pode sumir em silencio: vai para o relatorio
            # com o erro, para a auditoria saber que ali ha um buraco a refazer.
            telemetry.error("FIGURAS", f"pagina {pno + 1} falhou", exc)
            r = {"pagina": pno + 1, "erro": repr(exc), "figuras": [],
                 "rejeitadas": [], "truncou": False}
        with trava:
            resultados[pno] = r
            feitas[0] += 1
            if feitas[0] % 25 == 0 or feitas[0] == len(paginas):
                print(f"  {feitas[0]}/{len(paginas)} paginas", flush=True)

    with ocr.abrir_servidor(settings.ocr_bin, settings.caminho_modelo_ocr,
                            settings.caminho_mmproj_ocr, settings.ocr_porta,
                            settings.ocr_timeout_pagina, settings.ocr_n_gpu_layers,
                            n_ctx, paralelo) as servidor:
        with ThreadPoolExecutor(max_workers=paralelo) as pool:
            list(pool.map(_uma, paginas))

    return _consolidar_e_gravar(livro, cfg, resultados, paginas, destino, dry_run,
                                limite, relatorio, total)


def _consolidar_e_gravar(livro: str, cfg: dict, resultados: Dict[int, dict],
                         paginas: List[int], destino: Path, dry_run: bool,
                         limite: int, relatorio: Optional[str], total: int) -> int:
    faixas = _texto_por_pagina(cfg["slug"])
    por_pagina = {p + 1: len(resultados.get(p, {}).get("figuras", [])) for p in paginas}
    truncadas = [r["pagina"] for r in resultados.values() if r.get("truncou")]
    erradas = [r["pagina"] for r in resultados.values() if r.get("erro")]
    rejeitadas = sum(len(r.get("rejeitadas", [])) for r in resultados.values())

    print(f"\n{livro}: {fr.resumo_contagem(por_pagina)}")
    print(f"  {rejeitadas} caixa(s) rejeitada(s) pelos filtros")
    if truncadas:
        telemetry.warn("FIGURAS", f"{len(truncadas)} pagina(s) truncaram ATE O FIM "
                                  f"(recall em risco): {truncadas[:20]}")
    if erradas:
        telemetry.warn("FIGURAS", f"{len(erradas)} pagina(s) falharam: {erradas[:20]}")

    escritas = gravadas = 0
    agora = datetime.now()
    if not dry_run:
        destino.mkdir(parents=True, exist_ok=True)
    for pno in paginas:
        r = resultados.get(pno) or {}
        pagina = pno + 1
        fonte_cap = _da_pagina(faixas, pagina, 1)
        capitulo = fr.capitulo_util(_da_pagina(faixas, pagina, 0))
        temas = fr.temas_do_texto(fonte_cap or r.get("texto_ocr", ""))
        for i, f in enumerate(r.get("figuras", []), start=1):
            if gravadas >= limite:
                telemetry.warn("FIGURAS", f"teto de {limite} figuras atingido")
                break
            # ANCORA: legenda cuja palavras nao estao no texto do capitulo e
            # invencao do modelo (medido: "2023-2024 Academic Year Calendar" numa
            # pagina de biologia em portugues). Reprovada vira "".
            f["legenda"] = fr.legenda_ancorada(f["legenda_crua"], fonte_cap)
            nome = figuras.nome_arquivo(cfg["slug"], pagina, i)
            f["arquivo"] = nome
            gravadas += 1
            if dry_run:
                continue
            webp = figuras.para_webp(_recortar(r["png"], tuple(f["ret"])),
                                     settings.figuras_qualidade,
                                     settings.figuras_max_lado)
            if webp is None:
                continue
            (destino / nome).write_bytes(webp)
            corpo = fr.corpo_da_nota(f"{cfg['slug']}/{nome}", f["legenda"], pagina,
                                     settings.subpasta_figuras, livro=cfg["titulo"],
                                     capitulo=capitulo, temas=temas)
            nota = atomos.normalizar_atomo(
                corpo, f"Livro '{cfg['titulo']}' (p. {pagina}) — figura", agora)
            if nota.strip():
                (destino / (Path(nome).stem + ".md")).write_text(nota, encoding="utf-8")
                escritas += 1

    com_legenda = sum(1 for r in resultados.values() for f in r.get("figuras", [])
                      if f.get("legenda"))
    print(f"  {gravadas} figura(s) | {com_legenda} com legenda ancorada "
          f"({100 * com_legenda // max(1, gravadas)}%)")
    if relatorio:
        _escrever_relatorio(relatorio, livro, cfg, resultados, paginas, total, destino)
    if dry_run:
        print("(--dry-run: nada foi gravado)")
    else:
        print(f"  {escritas} nota(s) em {destino}")
        print("AUDITE antes de indexar: python eval/auditar_figuras.py "
              f"{relatorio or '<relatorio.json>'}")
    return 0


def _escrever_relatorio(caminho: str, livro: str, cfg: dict,
                        resultados: Dict[int, dict], paginas: List[int],
                        total: int, destino: Path) -> None:
    """Relatorio JSON com TODA caixa — aceita e rejeitada, com o motivo.

    E o que torna a passada auditavel: a auditoria visual desenha exatamente
    estas caixas (sem redetectar), e a lista de rejeitadas e a prova de que o
    filtro nao comeu figura de verdade."""
    dados = {
        "livro": livro, "slug": cfg["slug"], "titulo": cfg["titulo"],
        "pdf": cfg["pdf"], "total_paginas": total, "destino": str(destino),
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "paginas": [{k: v for k, v in (resultados.get(p) or {"pagina": p + 1}).items()
                     if k != "png"} for p in paginas],
    }
    alvo = Path(caminho)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    alvo.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  relatorio -> {alvo}")


def refazer_notas(relatorio: str, saida: Optional[str]) -> int:
    """Regrava so os .md a partir de um relatorio ja existente. Nao usa GPU.

    Existe porque detectar e ANOTAR sao custos de ordens diferentes: a deteccao
    custa ~20 min de GPU por livro, a nota e uma funcao pura sobre o que ja esta
    no relatorio. Quando o corpo da nota muda — foi o caso em 2026-07-26, quando o
    dono viu que as 1.020 notas do Cervantes tinham saido SEM um unico wikilink de
    Malha Neural — refazer as notas nao pode implicar reprocessar o livro."""
    dados = json.loads(Path(relatorio).read_text(encoding="utf-8"))
    cfg = LIVROS[dados["livro"]]
    destino = Path(saida) / cfg["slug"] if saida else settings.dir_figuras / cfg["slug"]
    if not destino.is_dir():
        print(f"pasta das figuras nao encontrada: {destino}")
        return 2
    faixas = _texto_por_pagina(cfg["slug"])
    agora = datetime.now()
    escritas = orfas = 0
    for reg in dados["paginas"]:
        pagina = reg["pagina"]
        fonte_cap = _da_pagina(faixas, pagina, 1)
        capitulo = fr.capitulo_util(_da_pagina(faixas, pagina, 0))
        temas = fr.temas_do_texto(fonte_cap or reg.get("texto_ocr", ""))
        for f in reg.get("figuras", []):
            nome = f.get("arquivo")
            if not nome or not (destino / nome).is_file():
                orfas += 1          # a imagem nao foi gravada: nao inventa nota
                continue
            corpo = fr.corpo_da_nota(f"{cfg['slug']}/{nome}", f.get("legenda") or "",
                                     pagina, settings.subpasta_figuras,
                                     livro=cfg["titulo"], capitulo=capitulo,
                                     temas=temas)
            nota = atomos.normalizar_atomo(
                corpo, f"Livro '{cfg['titulo']}' (p. {pagina}) — figura", agora)
            if nota.strip():
                (destino / (Path(nome).stem + ".md")).write_text(nota, encoding="utf-8")
                escritas += 1
    print(f"{escritas} nota(s) regravada(s) em {destino}"
          + (f" | {orfas} figura(s) sem imagem no disco (puladas)" if orfas else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--livro", choices=sorted(LIVROS))
    ap.add_argument("--refazer-notas", default="",
                    help="regrava so os .md a partir deste relatorio (sem GPU)")
    ap.add_argument("--dpi", type=int, default=150,
                    help="resolucao da rasterizacao (150 = ~1240x1750)")
    ap.add_argument("--amostra", type=int, default=0,
                    help="processa so N paginas espalhadas pelo miolo (validacao)")
    ap.add_argument("--paginas", default="",
                    help="paginas especificas, 1-based: '250,157,33-40'")
    ap.add_argument("--saida", default="",
                    help="pasta alternativa (validacao); vazio = o vault")
    ap.add_argument("--relatorio", default="",
                    help="JSON com todas as caixas, para a auditoria visual")
    ap.add_argument("--dry-run", action="store_true", help="detecta sem gravar")
    ap.add_argument("--limite", type=int, default=settings.figuras_max_por_livro)
    ap.add_argument("--max-tokens", type=int, default=4096,
                    help="teto da passada de markdown (dobra sozinho se truncar)")
    ap.add_argument("--paralelo", type=int, default=settings.ocr_paralelo)
    ap.add_argument("--n-ctx", type=int, default=0,
                    help="0 = 8192 por slot (o llama-server DIVIDE o -c entre os "
                         "slots; com o default de 16384 e 4 slots sobram 4096 "
                         "por pagina, e a passada de markdown trunca)")
    args = ap.parse_args()
    if args.refazer_notas:
        return refazer_notas(args.refazer_notas, args.saida or None)
    if not args.livro:
        ap.error("--livro e obrigatorio (ou use --refazer-notas)")
    n_ctx = args.n_ctx or max(settings.ocr_n_ctx, 8192 * max(1, args.paralelo))
    return processar(args.livro, args.dpi, args.amostra, args.paginas, args.dry_run,
                     args.limite, args.saida, args.relatorio, args.max_tokens,
                     max(1, args.paralelo), n_ctx)


if __name__ == "__main__":
    raise SystemExit(main())
