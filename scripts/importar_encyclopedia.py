"""Importa um livro publicado na WEB (capítulo por página) para a fila de ingestão.

Uso:
    python scripts/importar_encyclopedia.py --listar
    python scripts/importar_encyclopedia.py --url <URL-do-capitulo>      # 1 capítulo
    python scripts/importar_encyclopedia.py --tudo                      # todos
    python scripts/importar_encyclopedia.py --url <URL> --seco          # não grava nada

Como o `ingerir_livro.py` (PDF), este script SÓ extrai e enfileira: o texto vira
jobs JSON em dados/ingestao/pendentes/ e o SCHEDULER atomiza no idle, sem nunca
competir com uma conversa aberta. As figuras viram .webp no vault, na MESMA
estrutura do resto (`Figuras/<slug-do-livro>/<slug>_pNNNN_fN.webp`), então o
gate de figura, a proveniência e o `bloco_de_figuras` funcionam sem alteração.

Retomada: capítulo já processado é pulado (estado em `_web_estado.json`), e
imagem já baixada não é rebaixada. Pode interromper e rodar de novo.

Educado por padrão: respeita robots.txt, um pedido por vez e pausa entre eles.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.robotparser
from datetime import datetime
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mente_digital import atomos, encyclopedia, prompts  # noqa: E402
from mente_digital import figuras as figs_mod, livro as livro_mod  # noqa: E402
from mente_digital.config import settings  # noqa: E402

INDICE = "https://marijuanagrowing.com/the-cannabis-encyclopedia/"
TITULO = "The Cannabis Encyclopedia (Jorge Cervantes) — edição web"
# ASCII PURO: header HTTP é codificado em ascii pelo httpx e um acento aqui
# derruba o cliente inteiro no construtor (UnicodeEncodeError).
UA = "MenteDigital/1.0 (assistente pessoal local; arquivo para uso proprio)"
PAUSA_S = 1.5
MAX_CHARS_BLOCO = 6000


def _http():
    import httpx
    return httpx.Client(
        timeout=60, follow_redirects=True,
        headers={"User-Agent": UA, "Accept-Language": "en;q=0.9"},
    )


_ROBOTS: dict = {}


def permitido(cli, url: str) -> bool:
    """Checa o robots.txt do host, buscando-o com o MESMO cliente do resto.

    NÃO usar `RobotFileParser.read()`: ele busca pelo opener padrão do urllib,
    que este site responde com 403 — e o parser trata 401/403 como
    'disallow_all', em silêncio. O resultado era recusar TODO capítulo alegando
    robots.txt, com um robots.txt que só bloqueia /wp-admin/ (medido)."""
    partes = urllib.parse.urlsplit(url)
    host = f"{partes.scheme}://{partes.netloc}"
    rp = _ROBOTS.get(host)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = cli.get(f"{host}/robots.txt")
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except Exception:
            rp.parse([])                 # sem robots legível: nada proibido
        _ROBOTS[host] = rp
    return rp.can_fetch(UA, url)


def listar_capitulos(cli) -> List[tuple]:
    """[(ordem, titulo, url)] a partir do índice — a ordem da página é a do livro."""
    from bs4 import BeautifulSoup
    html = cli.get(INDICE).text
    soup = BeautifulSoup(html, "html.parser")
    vistos, saida = set(), []
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(INDICE, a["href"].split("#")[0].rstrip("/") + "/")
        texto = a.get_text(" ", strip=True)
        if href == INDICE or not href.startswith("https://marijuanagrowing.com/"):
            continue
        if "chapter" not in (texto + href).lower():
            continue
        if href in vistos:
            continue
        vistos.add(href)
        saida.append((len(saida) + 1, texto or href, href))
    return saida


def escrever_nota(fig, slug: str, destino: Path, cap_num: int,
                  titulo_cap: str) -> bool:
    """Grava o `.md` da figura ao lado do `.webp`. Devolve se escreveu.

    Sem esta nota a figura é invisível: a busca de figura roda sobre NOTAS
    (`where={"tipo":"figura"}`) e o anexo à resposta é montado a partir dos
    caminhos de nota. Um `.webp` solto no vault nunca chega à tela.

    O `origem` é montado pelo MESMO ponto único que o ETL usará ao atomizar o
    texto desta página (`livro.origem_do_job`) — é a igualdade exata dessas duas
    strings que faz o anexo por co-locação achar a figura da página certa."""
    origem = livro_mod.origem_do_job({
        "livro": TITULO, "capitulo": cap_num, "titulo_cap": titulo_cap,
        "pagina_inicio": fig.pagina, "pagina_fim": fig.pagina,
    })
    corpo = encyclopedia.corpo_nota_figura(
        fig, f"{slug}/{fig.arquivo}", settings.subpasta_figuras,
        TITULO, titulo_cap,
        tag_anexo=prompts.TAG_FIGURA_ANEXO if fig.attach_only else "",
    )
    nota = atomos.normalizar_atomo(corpo, origem, datetime.now())
    if not nota.strip():
        return False
    (destino / (Path(fig.arquivo).stem + ".md")).write_text(nota, encoding="utf-8")
    return True


def baixar_figuras(cli, blocos, slug: str, destino: Path, cap_num: int,
                   titulo_cap: str, seco: bool = False) -> tuple:
    """Baixa, converte para WebP, grava a nota e devolve (imagens, notas).

    Falha de uma imagem NÃO derruba o capítulo: a figura é descartada e o texto
    segue — o vault com um átomo a menos de imagem é melhor que capítulo nenhum."""
    gravadas = notas = 0
    por_pagina: dict = {}
    for bloco in blocos:
        for fig in bloco.figuras:
            por_pagina.setdefault(fig.pagina, []).append(fig)
    if not seco and por_pagina:
        destino.mkdir(parents=True, exist_ok=True)
    for pagina, lista in sorted(por_pagina.items()):
        for i, fig in enumerate(lista, 1):
            nome = figs_mod.nome_arquivo(slug, pagina, i)
            fig.arquivo = nome                       # o job referencia por nome
            alvo = destino / nome
            if seco:
                continue
            if not alvo.exists():
                try:
                    resp = cli.get(fig.url)
                    resp.raise_for_status()
                    webp = figs_mod.para_webp(resp.content, settings.figuras_qualidade,
                                              settings.figuras_max_lado)
                    if not webp:
                        print(f"      ! imagem ilegível, pulando: {fig.url[:70]}")
                        fig.arquivo = ""
                        continue
                    alvo.write_bytes(webp)
                except Exception as exc:
                    print(f"      ! falhou {fig.url[:70]}: {exc}")
                    fig.arquivo = ""
                    continue
                finally:
                    time.sleep(PAUSA_S)
            gravadas += 1
            # A nota é REESCRITA mesmo quando a imagem já estava em disco: ela é
            # pura (função do que já se sabe), e o corpo dela ainda muda entre
            # passadas — rodar de novo tem que corrigir a nota velha, não pulá-la.
            try:
                notas += escrever_nota(fig, slug, destino, cap_num, titulo_cap)
            except OSError as exc:
                print(f"      ! nota não gravada para {nome}: {exc}")
    return gravadas, notas


def montar_jobs(titulo_livro: str, cap_num: int, titulo_cap: str,
                blocos) -> List[dict]:
    jobs = []
    for bloco in blocos:
        if not bloco.texto.strip():
            continue
        jobs.append({
            "livro": titulo_livro,
            "capitulo": cap_num,
            "titulo_cap": titulo_cap,
            "pagina_inicio": bloco.pagina,
            "pagina_fim": bloco.pagina,
            "texto": bloco.texto,
            "figuras": [
                {"arquivo": getattr(f, "arquivo", ""), "pagina": f.pagina,
                 "legenda": f.legenda,
                 # a escada e a malha viajam com a figura: quem grava a nota
                 # precisa saber de que degrau veio a legenda (para calibrar
                 # depois) e com que conceitos ligá-la ao texto.
                 "legenda_origem": f.legenda_origem,
                 "titulo_secao": f.titulo_secao,
                 "attach_only": f.attach_only,
                 "conceitos": f.conceitos}
                for f in bloco.figuras if getattr(f, "arquivo", "")
            ],
        })
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listar", action="store_true", help="só lista os capítulos")
    ap.add_argument("--url", help="importa UM capítulo (para conferir antes do lote)")
    ap.add_argument("--tudo", action="store_true", help="importa todos os capítulos")
    ap.add_argument("--seco", action="store_true",
                    help="não grava job nem imagem; só relata o que faria")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS_BLOCO)
    args = ap.parse_args()

    slug = livro_mod.slug(TITULO)
    dir_figs = Path(settings.caminho_obsidian) / settings.subpasta_figuras / slug
    dir_jobs = Path(settings.dir_ingestao) / "pendentes"
    estado_p = Path(settings.dir_ingestao) / f"_web_{slug}.json"
    estado = json.loads(estado_p.read_text("utf-8")) if estado_p.exists() else {}

    with _http() as cli:
        capitulos = listar_capitulos(cli)
        if args.listar:
            for n, t, u in capitulos:
                marca = "ok" if u in estado else "  "
                print(f"  [{marca}] {n:2d}. {t[:62]:62s} {u}")
            print(f"\n{len(capitulos)} capítulo(s). slug do livro: {slug}")
            return 0

        alvos = ([(n, t, u) for n, t, u in capitulos if u.rstrip('/') == args.url.rstrip('/')]
                 if args.url else capitulos if args.tudo else [])
        if args.url and not alvos:      # URL fora do índice: importa mesmo assim
            alvos = [(1, args.url, args.url)]
        if not alvos:
            ap.error("escolha --listar, --url <URL> ou --tudo")

        pagina = int(estado.get("_proxima_pagina", 1))
        total_jobs = total_figs = 0
        for n, titulo_cap, url in alvos:
            if url in estado and not args.seco:
                print(f"  [{n:2d}] já importado, pulando: {titulo_cap[:50]}")
                continue
            if not permitido(cli, url):
                print(f"  [{n:2d}] robots.txt não permite: {url}")
                continue
            print(f"  [{n:2d}] {titulo_cap[:60]}")
            try:
                html = cli.get(url).text
            except Exception as exc:
                print(f"      ! não baixou: {exc}")
                continue
            titulo, brutos = encyclopedia.extrair_capitulo(html, titulo_cap)
            if not brutos:
                print("      ! sem corpo reconhecível — pulado")
                continue
            blocos = encyclopedia.fatiar(brutos, args.max_chars, pagina)
            print(f"      {encyclopedia.estatisticas(blocos)}  "
                  f"páginas {blocos[0].pagina}-{blocos[-1].pagina}")
            gravadas, notas = baixar_figuras(cli, blocos, slug, dir_figs, n,
                                             titulo or titulo_cap, args.seco)
            jobs = montar_jobs(TITULO, n, titulo or titulo_cap, blocos)
            print(f"      {len(jobs)} job(s), {gravadas} figura(s) em disco, "
                  f"{notas} nota(s) de figura")
            if not args.seco:
                dir_jobs.mkdir(parents=True, exist_ok=True)
                for j, job in enumerate(jobs, 1):
                    alvo = dir_jobs / f"{slug}__{n:03d}_{j:03d}.json"
                    alvo.write_text(json.dumps(job, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
                estado[url] = {"capitulo": n, "jobs": len(jobs),
                               "figuras": gravadas,
                               "paginas": [blocos[0].pagina, blocos[-1].pagina]}
            pagina = blocos[-1].pagina + 1
            total_jobs += len(jobs)
            total_figs += gravadas
            time.sleep(PAUSA_S)

        if not args.seco:
            estado["_proxima_pagina"] = pagina
            estado_p.parent.mkdir(parents=True, exist_ok=True)
            estado_p.write_text(json.dumps(estado, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    print(f"\nTotal: {total_jobs} job(s) enfileirado(s), {total_figs} figura(s).")
    if not args.seco and total_jobs:
        print("O scheduler atomiza no idle. Para rodar já: "
              "python scripts/atomizar_agora.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
