"""Imagem da WEB, servida LOCALMENTE (2026-07-31).

Pedido do dono: quando ele pede para ver algo e o acervo do vault não tem a foto,
o assistente deve procurar na internet em vez de só dizer que não tem.

A parte difícil não é achar a imagem — é entregá-la sem furar o pilar do projeto.
O front RECUSA de propósito markdown de imagem com URL externa (templates/index.html:
"uma nota envenenada pela web faria o navegador buscar um servidor de fora"). Essa
regra está certa e não é contornada aqui: o SERVIDOR baixa, sanitiza e guarda o
arquivo, e o chat recebe o mesmo wikilink de sempre, servido por `/api/imagem/`.
O navegador continua falando só com o localhost.

Três defesas, todas no caminho do download:
1. **SSRF** — só http/https, e o host tem de resolver para IP público. Sem isto,
   uma URL devolvida por um buscador poderia apontar para `169.254.169.254` ou para
   a rede doméstica do dono e o servidor buscaria por ele.
2. **Tamanho** — leitura em pedaços com corte duro; um "image/jpeg" de 2 GB não vira
   memória cheia.
3. **Reencode** — o arquivo é aberto e RE-SALVO em WebP pelo Pillow. É o que
   transforma bytes de origem desconhecida em pixel: sobrevive a imagem de verdade,
   morre polyglot/SVG com script/EXIF com carga. Por isso SVG não é aceito, mesmo
   sendo imagem: ele é documento executável.

O cache é por hash da URL: a mesma foto pedida duas vezes não é baixada de novo.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import re
import socket
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import urlparse

from mente_digital import textutils
from mente_digital.config import settings
from mente_digital.telemetry import telemetry

_EXT = ".webp"

# Importado do rag para não haver dois User-Agents divergindo com o tempo.
from mente_digital.rag import _HEADERS_FETCH  # noqa: E402


def nome_do_cache(url: str) -> str:
    """Nome estável do arquivo para uma URL. Puro/testável."""
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:24] + _EXT


def host_publico(url: str) -> bool:
    """A URL aponta para um host público (não é SSRF)? Faz DNS — não é puro.

    Recusa esquema fora de http/https, host vazio, e qualquer resolução que caia em
    faixa privada/loopback/link-local. É a diferença entre "baixar uma foto" e
    "fazer o servidor varrer a rede de casa por conta de um link de buscador".
    """
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False
    return True


def _pasta() -> Path:
    return Path(settings.caminho_obsidian) / settings.imagem_web_subpasta


def _sanitizar(bruto: bytes) -> Optional[bytes]:
    """Bytes de origem desconhecida -> WebP re-encodado, ou None. IO/CPU-bound.

    Abrir e re-salvar é a sanitização: o que sai é pixel produzido pelo Pillow, não
    o arquivo que veio da rede. `verify()` antes, porque ele detecta truncado/
    malformado sem decodificar a imagem inteira.
    """
    try:
        from PIL import Image
    except ImportError:
        telemetry.warn("IMG_WEB", "Pillow ausente — imagem da web desligada.")
        return None
    try:
        Image.open(io.BytesIO(bruto)).verify()
        img = Image.open(io.BytesIO(bruto))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=80)
        return buf.getvalue()
    except Exception as exc:                      # imagem quebrada/hostil: descarta
        telemetry.warn("IMG_WEB", f"Imagem recusada na sanitização: {exc}")
        return None


async def baixar(url: str) -> Optional[str]:
    """Baixa, sanitiza e guarda. Devolve o caminho RELATIVO ao vault, ou None.

    O caminho relativo é o que vira `![[...]]` no chat e é resolvido pelo
    `/api/imagem/`, que já tem allowlist de extensão e trava de path traversal.
    """
    if not url or not settings.imagem_web_enabled:
        return None
    destino = _pasta() / nome_do_cache(url)
    rel = f"{settings.imagem_web_subpasta}/{destino.name}"
    if destino.is_file():
        return rel                                # cache: mesma foto, sem rede
    if not await asyncio.to_thread(host_publico, url):
        telemetry.warn("IMG_WEB", f"URL recusada (host não público): {url[:80]}")
        return None

    import httpx

    teto = settings.imagem_web_max_mb * 1024 * 1024
    try:
        async with httpx.AsyncClient(
            timeout=settings.imagem_web_timeout_segundos, follow_redirects=True,
            # O MESMO User-Agent do deep-fetch: muitos sites 403-am requisição sem
            # UA de navegador, e hotlink de imagem é onde isso mais acontece.
            headers=dict(_HEADERS_FETCH),
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                tipo = (resp.headers.get("content-type") or "").lower()
                # SVG é imagem e é documento executável — fica de fora de propósito.
                if not tipo.startswith("image/") or "svg" in tipo:
                    telemetry.warn("IMG_WEB", f"Tipo recusado: {tipo!r}")
                    return None
                pedacos, total = [], 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > teto:
                        telemetry.warn(
                            "IMG_WEB", f"Imagem maior que {settings.imagem_web_max_mb}MB — corta.")
                        return None
                    pedacos.append(chunk)
    except Exception as exc:
        telemetry.warn("IMG_WEB", f"Falha ao baixar imagem ({url[:60]}): {exc}")
        return None

    limpo = await asyncio.to_thread(_sanitizar, b"".join(pedacos))
    if not limpo:
        return None
    try:
        await asyncio.to_thread(_gravar, destino, limpo)
    except OSError as exc:
        telemetry.error("IMG_WEB", "Não consegui gravar a imagem baixada", exc)
        return None
    telemetry.track("IMG_WEB", f"imagem da web guardada: {rel} ({len(limpo)//1024}KB)")
    return rel


def _gravar(destino: Path, dados: bytes) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(dados)


def embed(rel: str) -> str:
    """Caminho relativo -> o wikilink que o front já sabe renderizar. Puro."""
    return f"![[{rel}]]" if rel else ""


def casa_com_o_pedido(candidato: dict, termos: str) -> bool:
    """O candidato fala do que foi pedido? Puro/testável.

    É o mesmo ATERRAMENTO LÉXICO que governa o texto no `VectorStore.search`, agora
    aplicado à imagem: o buscador sempre devolve CINCO resultados, então "não achei"
    nunca chega como lista vazia — chega como cinco coisas quaisquer. Sem esta
    régua o primeiro que baixa vence, e foi o que o teste de 50 casos pegou:
      "tem foto de um estômato?" -> um formulário de currículo colombiano em branco
      "me mostra uma imagem de um polvo" -> um cartaz "EBT Florida Reload Schedule"
    Nos dois o título da página não tinha NADA a ver com o termo — dava para saber
    antes de baixar.

    Casa contra título e URL (a URL costuma trazer o slug do assunto). Sem keyword
    utilizável, aceita: melhor uma imagem fraca do que travar tudo por causa de uma
    pergunta cujo termo o normalizador não soube fatiar.
    """
    chaves = textutils.palavras_chave(termos or "")
    if not chaves:
        return True
    alvo = " ".join(str((candidato or {}).get(c) or "")
                    for c in ("title", "url", "source", "image"))
    return textutils.contem_alguma(alvo, chaves)


async def primeira_util(candidatos: Sequence[dict], termos: str = "") -> Optional[tuple]:
    """Tenta os candidatos em ordem e devolve (caminho_relativo, candidato).

    Em ordem e não em paralelo de propósito: o buscador já ranqueou, e baixar cinco
    para usar uma é gastar rede do dono à toa. O primeiro que passa no gate E
    sobrevive às três defesas é o que aparece.

    Se NENHUM casar o pedido, devolve None — e o chamador diz que não achou. Mentir
    com uma imagem qualquer é pior do que admitir a falta: a resposta ao lado dela
    seria sobre outra coisa.
    """
    descartados = 0
    for c in candidatos or ():
        if termos and not casa_com_o_pedido(c, termos):
            descartados += 1
            continue
        rel = await baixar(str((c or {}).get("image") or (c or {}).get("url") or ""))
        if rel:
            return rel, c
    if descartados:
        telemetry.track(
            "IMG_WEB",
            f"{descartados} candidato(s) fora do assunto '{termos}' — descartado(s).")
    return None


def creditar(candidato: dict) -> str:
    """A linha de origem da imagem. Puro.

    O dono precisa saber que ESTA foto veio da internet e de onde — o resto da
    resposta é do vault dele, e misturar as duas coisas sem dizer seria pior do
    que não mostrar imagem nenhuma.
    """
    if not candidato:
        return ""
    fonte = str(candidato.get("source") or candidato.get("title") or "").strip()
    pagina = str(candidato.get("url") or "").strip()
    if fonte and pagina:
        return f"(imagem da web — {fonte}: {pagina})"
    return f"(imagem da web — {fonte or pagina})" if (fonte or pagina) else "(imagem da web)"


def nota_do_acervo(rel: str, candidato: dict, termos: str, agora: str) -> str:
    """A nota .md que faz a imagem da web virar ACERVO. Puro/testável.

    Ordem do dono (2026-07-31): "guardar um acervo local também, pra não ficar
    dependente da web". Sem esta nota o .webp é só cache — ninguém o encontra,
    porque no vault quem é indexado é o MARKDOWN, não a imagem. Com ela, a foto
    baixada hoje é achada amanhã pela busca local, de graça e sem rede, pelo mesmo
    caminho das figuras dos livros.

    O formato é o das notas de figura da importação (frontmatter de proveniência +
    título + corpo + embed + Malha + tags), para não inventar um segundo formato
    que o resto do sistema teria de aprender a ler.

    A proveniência diz WEB e guarda a URL: quando esta nota aparecer numa resposta,
    dá para saber que não é o livro do dono falando — e apagar todo o acervo web de
    uma vez é remover uma pasta.
    """
    titulo = (str((candidato or {}).get("title") or termos or "imagem")).strip()
    titulo = re.sub(r"\s+", " ", titulo)[:120]
    fonte = str((candidato or {}).get("source") or "").strip()
    pagina = str((candidato or {}).get("url") or "").strip()
    # O texto vem de terceiros: some com o que quebraria o frontmatter ou embutiria
    # marcação. É a mesma cautela do resto da colheita web deste projeto.
    seguro = re.sub(r"[\[\]{}<>|\r\n]", " ", titulo).strip() or "imagem da web"
    return (
        "---\n"
        f"origem: Web — busca de imagem por '{termos}'\n"
        f"origem_url: {pagina}\n"
        f"origem_site: {fonte}\n"
        f"colhido_em: {agora}\n"
        "tipo: figura\n"
        "---\n"
        f"## {seguro}\n"
        f"{seguro}\n"
        f"![[{rel}]]\n"
        f"Imagem obtida na web em uma busca por '{termos}'"
        + (f", em {fonte}." if fonte else ".") + "\n"
        f"**Malha Neural:** [[{termos.strip().title() or 'Imagem'}]] [[Imagem da Web]]\n"
        "#zettelkasten_atomico #conhecimento_novo\n"
    )


def guardar_no_acervo(rel: str, candidato: dict, termos: str, agora: str) -> Optional[str]:
    """Grava a nota .md ao lado da imagem. Devolve o caminho, ou None. IO.

    Idempotente pelo nome (o mesmo hash da imagem): pedir a mesma foto de novo não
    cria nota duplicada. Fail-soft — o acervo é bônus; se a gravação falhar, a
    imagem já foi entregue ao dono e o turno não pode quebrar por causa disso.
    """
    if not rel or not settings.imagem_web_acervo:
        return None
    destino = Path(settings.caminho_obsidian) / rel
    nota = destino.with_suffix(".md")
    if nota.is_file():
        return str(nota)
    try:
        nota.parent.mkdir(parents=True, exist_ok=True)
        nota.write_text(nota_do_acervo(rel, candidato, termos, agora), encoding="utf-8")
    except OSError as exc:
        telemetry.error("IMG_WEB", "Não consegui gravar a nota do acervo", exc)
        return None
    telemetry.track("IMG_WEB", f"acervo: nota criada para {rel}")
    return str(nota)


def limpar_antigas(maximo: int) -> int:
    """Mantém no máximo `maximo` imagens (as mais recentes). IO.

    Some com a NOTA junto: deixar a nota sem a imagem produziria a "figura quebrada
    na tela" que o `_preparar_figuras` já aprendeu a evitar conferindo o .webp em
    disco. Com o acervo ligado o default é 0 (não limpa) — apagar o acervo por
    idade seria voltar a depender da web, que é o que o dono pediu para evitar.
    """
    pasta = _pasta()
    if maximo <= 0 or not pasta.is_dir():
        return 0
    arquivos: List[Path] = sorted(
        (p for p in pasta.glob(f"*{_EXT}") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True)
    removidas = 0
    for p in arquivos[maximo:]:
        try:
            p.unlink()
            p.with_suffix(".md").unlink(missing_ok=True)
            removidas += 1
        except OSError as exc:
            telemetry.warn("IMG_WEB", f"Não consegui remover {p.name}: {exc}")
    return removidas
