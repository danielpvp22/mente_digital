"""IMAGEM DA WEB: o servidor baixa, sanitiza e serve — o navegador nunca sai de casa.

O pedido do dono (2026-07-31) foi simples: "tem foto de uma capivara?" não pode
morrer num "não tenho". O difícil não é achar a imagem, é entregá-la sem furar o
pilar do projeto — o front RECUSA de propósito markdown de imagem com URL externa,
porque uma nota envenenada pela web faria o navegador buscar um servidor de fora.

Por isso a feature inteira é uma sequência de defesas, e é isso que se testa aqui:

1. **SSRF** — a URL vem de um BUSCADOR, ou seja, de fora. Sem o gate de host
   público, `http://169.254.169.254/latest/meta-data/` (o endereço de metadados de
   nuvem) ou `http://192.168.0.1/` fariam o servidor varrer a rede de casa do dono
   e trazer o resultado pra tela. É o teste mais importante deste arquivo.
2. **Tamanho** — o corte é no MEIO do stream: um "image/jpeg" de 2 GB tem que
   parar de ser lido, não ser lido inteiro e depois descartado.
3. **Tipo** — SVG é imagem e é documento executável; fica de fora por isso, não
   por descuido.

E a ordem de precedência: o acervo do vault SEMPRE vence. A foto do livro do dono
responde melhor, já está em disco e não gasta rede — a web só entra no vazio, ou
quando ele MANDA buscar fora ("procura na internet uma foto de X").

Nada aqui toca a rede, o disco do vault real ou a GPU: DNS, httpx, LLM, TTS, store
e web são fakes.
"""
from __future__ import annotations

import io
import os
import socket
from collections import deque
from pathlib import Path

import httpx
import pytest

from mente_digital import imagem_web, otimizador
from mente_digital.agent import Agent
from mente_digital.config import settings
from mente_digital.rag import LocalResult
from mente_digital.state import AppContext, SessionMemory

from conftest import FakeLlama, FakeTts, make_send, textos_de_tokens

URL = "https://exemplo.org/foto.jpg"
IP_PUBLICO = "93.184.216.34"


@pytest.fixture(autouse=True)
def _vault_tmp(monkeypatch, tmp_path):
    """O cache da imagem da web mora DENTRO do vault (é o `/api/imagem/` que serve,
    e essa rota tranca tudo fora da raiz). Redirecionar o vault para o tmp_path em
    TODO teste deste arquivo não é preciosismo: `limpar_antigas` APAGA arquivos, e
    sem isto ela varreria `Figuras/_web` do vault real do dono."""
    monkeypatch.setattr(settings, "caminho_obsidian", str(tmp_path))
    return tmp_path


# ==========================================================================
# Fakes de rede — nenhum socket é aberto em teste nenhum
# ==========================================================================
def _dns(monkeypatch, ip: str) -> list:
    """Resolve QUALQUER host para `ip`, sem tocar na rede. Devolve os hosts pedidos.

    A lista de hosts é o que permite provar o NEGATIVO: uma URL recusada antes do
    DNS (esquema errado, host vazio) não pode nem chegar ao resolvedor."""
    perguntados: list = []

    def _fake(host, port, *args, **kwargs):
        perguntados.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(imagem_web.socket, "getaddrinfo", _fake)
    return perguntados


class _Resposta:
    """Resposta de streaming do httpx: content-type + pedaços do corpo."""

    def __init__(self, content_type: str, pedacos, erro: str = "") -> None:
        self.headers = {"content-type": content_type}
        self._pedacos = list(pedacos)
        self._erro = erro
        self.lidos = 0            # quantos pedaços o `baixar` chegou a consumir

    def raise_for_status(self) -> None:
        if self._erro:
            raise RuntimeError(self._erro)

    async def aiter_bytes(self):
        for pedaco in self._pedacos:
            self.lidos += 1
            yield pedaco


class _Ctx:
    def __init__(self, resposta: _Resposta) -> None:
        self._resposta = resposta

    async def __aenter__(self) -> _Resposta:
        return self._resposta

    async def __aexit__(self, *_exc) -> bool:
        return False


def _httpx(monkeypatch, resposta: _Resposta) -> list:
    """Troca `httpx.AsyncClient` pelo cliente falso. Devolve as URLs pedidas.

    O `baixar` faz `import httpx` DENTRO da função (o caminho quente não paga o
    import), e o import devolve o mesmo objeto de módulo do sys.modules — então
    trocar o atributo aqui alcança a produção."""
    pedidas: list = []

    class _Cliente:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> bool:
            return False

        def stream(self, metodo, url):
            pedidas.append((metodo, url))
            return _Ctx(resposta)

    monkeypatch.setattr(httpx, "AsyncClient", _Cliente)
    return pedidas


def _rede_proibida(monkeypatch) -> None:
    """Qualquer toque em rede (DNS ou HTTP) vira falha do teste. É assim que o
    caminho "não foi à rede" deixa de ser afirmação e vira prova."""
    def _sem_dns(*_a, **_k):
        raise AssertionError("resolveu DNS quando não devia")

    class _SemHttp:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("abriu conexão HTTP quando não devia")

    monkeypatch.setattr(imagem_web.socket, "getaddrinfo", _sem_dns)
    monkeypatch.setattr(httpx, "AsyncClient", _SemHttp)


def _png(cor=(10, 200, 30)) -> bytes:
    """Um PNG de verdade (8x8) — o `_sanitizar` abre e re-salva com o Pillow, então
    bytes inventados não serviriam para provar o reencode."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), cor).save(buf, format="PNG")
    return buf.getvalue()


# ==========================================================================
# 1. Nome do cache
# ==========================================================================
def test_nome_do_cache_e_estavel_e_termina_em_webp():
    """Estável porque É o cache: se o nome variasse entre chamadas, a mesma foto
    seria baixada de novo toda vez. `.webp` porque o que fica em disco nunca é o
    arquivo que veio da rede — é o reencode do Pillow."""
    assert imagem_web.nome_do_cache(URL) == imagem_web.nome_do_cache(URL)
    assert imagem_web.nome_do_cache(URL).endswith(".webp")


def test_urls_diferentes_dao_nomes_diferentes():
    """Colisão aqui mostraria a foto ERRADA (a do link anterior) com um crédito que
    aponta para outro site — o pior defeito possível numa feature de proveniência."""
    assert imagem_web.nome_do_cache(URL) != imagem_web.nome_do_cache(URL + "?v=2")
    assert imagem_web.nome_do_cache("") != imagem_web.nome_do_cache("a")


# ==========================================================================
# 2. SSRF — a defesa central: a URL veio de um buscador, não do dono
# ==========================================================================
def test_host_publico_aceita_https_de_ip_publico(monkeypatch):
    perguntados = _dns(monkeypatch, IP_PUBLICO)

    assert imagem_web.host_publico(URL) is True
    assert imagem_web.host_publico("http://exemplo.org/a.png") is True
    assert perguntados == ["exemplo.org", "exemplo.org"]


def test_ssrf_recusa_loopback(monkeypatch):
    """127.0.0.1 é o próprio servidor: um link do buscador não pode fazer o app
    buscar as rotas internas dele mesmo (o `/api/` roda ali)."""
    _dns(monkeypatch, "127.0.0.1")

    assert imagem_web.host_publico("http://localhost/a.png") is False


@pytest.mark.parametrize("ip", ["192.168.0.1", "10.0.0.5", "172.16.3.9"])
def test_ssrf_recusa_rede_privada(monkeypatch, ip):
    """A rede de CASA do dono: roteador, NAS, câmeras. Sem este gate, "tem foto
    disso?" viraria um scanner da LAN dele com o resultado impresso na tela."""
    _dns(monkeypatch, ip)

    assert imagem_web.host_publico("http://qualquer.coisa/a.png") is False


def test_ssrf_recusa_link_local_de_metadados_de_nuvem(monkeypatch):
    """169.254.169.254 é o endereço de metadados das nuvens (AWS/GCP/Azure) — o
    alvo clássico de SSRF, porque responde CREDENCIAIS a um GET simples. Hoje o
    app roda na máquina do dono; o dia em que rodar num VPS, este teste é a única
    coisa entre um link de buscador e a chave da instância."""
    _dns(monkeypatch, "169.254.169.254")

    assert imagem_web.host_publico("http://169.254.169.254/latest/meta-data/") is False


def test_ssrf_recusa_host_que_resolve_para_publico_E_privado(monkeypatch):
    """Um nome pode devolver VÁRIOS registros. Se bastasse um endereço público
    para aprovar, o atacante publicaria os dois e o servidor poderia conectar no
    privado — por isso a régua é "todos têm que ser públicos", não "algum"."""
    def _dois(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (IP_PUBLICO, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 0)),
        ]

    monkeypatch.setattr(imagem_web.socket, "getaddrinfo", _dois)

    assert imagem_web.host_publico("http://dois-registros.org/a.png") is False


@pytest.mark.parametrize("url", [
    "file:///C:/Users/User/.ssh/id_rsa",   # ler o disco do dono pelo "download"
    "ftp://exemplo.org/a.png",
    "data:image/png;base64,AAAA",          # nem chega a ser uma requisição
])
def test_ssrf_recusa_esquema_fora_de_http(monkeypatch, url):
    """E recusa ANTES do DNS: o esquema é decidido no parse, sem custo nem lookup."""
    perguntados = _dns(monkeypatch, IP_PUBLICO)

    assert imagem_web.host_publico(url) is False
    assert perguntados == []


@pytest.mark.parametrize("url", ["", "http://", "https://", "nao é uma url"])
def test_ssrf_recusa_host_vazio(monkeypatch, url):
    perguntados = _dns(monkeypatch, IP_PUBLICO)

    assert imagem_web.host_publico(url) is False
    assert perguntados == []


def test_ssrf_recusa_quando_o_dns_nao_resolve(monkeypatch):
    """Host que não existe: falha FECHADA. Sem o except, o erro subiria e a
    exceção do gate de segurança viraria um traceback no meio do turno."""
    def _falha(*_a, **_k):
        raise socket.gaierror("nome não resolve")

    monkeypatch.setattr(imagem_web.socket, "getaddrinfo", _falha)

    assert imagem_web.host_publico(URL) is False


# ==========================================================================
# 3. `baixar` — tipo e tamanho
# ==========================================================================
async def test_baixar_recusa_content_type_que_nao_e_imagem(monkeypatch, tmp_path):
    """O link do buscador pode apontar para a PÁGINA e não para o arquivo. HTML
    entrando no reencode é lixo garantido; recusar pelo cabeçalho é mais barato."""
    _dns(monkeypatch, IP_PUBLICO)
    resposta = _Resposta("text/html; charset=utf-8", [b"<html>"])
    _httpx(monkeypatch, resposta)

    assert await imagem_web.baixar(URL) is None
    assert resposta.lidos == 0                    # nem chegou a ler o corpo
    assert not (tmp_path / settings.imagem_web_subpasta).exists()


@pytest.mark.parametrize("tipo", ["image/svg+xml", "IMAGE/SVG+XML", "image/svg+xml; q=1"])
async def test_baixar_recusa_svg_mesmo_sendo_imagem(monkeypatch, tipo):
    """SVG passa no teste "começa com image/" e mesmo assim fica de fora: ele é
    documento EXECUTÁVEL (script, <foreignObject>, referência externa). O front
    renderiza o que o servidor guardar — um SVG hostil aqui é XSS na cara do dono."""
    _dns(monkeypatch, IP_PUBLICO)
    resposta = _Resposta(tipo, [b"<svg onload='...'/>"])
    _httpx(monkeypatch, resposta)

    assert await imagem_web.baixar(URL) is None
    assert resposta.lidos == 0


async def test_baixar_corta_no_MEIO_do_stream_ao_passar_do_teto(monkeypatch, tmp_path):
    """O ponto do teto não é recusar o arquivo grande — é não CARREGÁ-LO. Um
    "image/jpeg" de 2 GB lido inteiro para depois ser descartado é memória cheia
    do mesmo jeito. Com teto de 1MB e pedaços de 512KB, a leitura tem que morrer
    no 3º (1,5MB) e nunca pedir o 4º."""
    monkeypatch.setattr(settings, "imagem_web_max_mb", 1)
    _dns(monkeypatch, IP_PUBLICO)
    resposta = _Resposta("image/jpeg", [b"x" * (512 * 1024)] * 4)
    _httpx(monkeypatch, resposta)

    assert await imagem_web.baixar(URL) is None
    assert resposta.lidos == 3                    # parou de ler, não leu tudo
    assert not (tmp_path / settings.imagem_web_subpasta).exists()


async def test_baixar_nao_abre_conexao_para_host_nao_publico(monkeypatch):
    """A ORDEM importa: o gate de SSRF roda ANTES de qualquer socket HTTP. Se ele
    rodasse depois, a conexão com a rede interna já teria acontecido."""
    _dns(monkeypatch, "192.168.0.1")

    class _SemHttp:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("abriu conexão para host privado")

    monkeypatch.setattr(httpx, "AsyncClient", _SemHttp)

    assert await imagem_web.baixar("http://roteador.local/a.png") is None


async def test_baixar_reencoda_para_webp_e_guarda_no_vault(monkeypatch, tmp_path):
    """O caminho feliz, e ele prova a 3ª defesa: o que fica em disco é WebP
    produzido pelo Pillow (assinatura RIFF/WEBP), não o PNG que veio da rede. É o
    reencode que mata polyglot e carga em EXIF — sobrevive só o pixel."""
    _dns(monkeypatch, IP_PUBLICO)
    _httpx(monkeypatch, _Resposta("image/png", [_png()]))

    rel = await imagem_web.baixar(URL)

    assert rel == f"{settings.imagem_web_subpasta}/{imagem_web.nome_do_cache(URL)}"
    gravado = (tmp_path / rel).read_bytes()
    assert gravado[:4] == b"RIFF" and gravado[8:12] == b"WEBP"


async def test_baixar_descarta_o_que_nao_e_imagem_de_verdade(monkeypatch, tmp_path):
    """Content-type mente. Um `image/png` cujo corpo não abre no Pillow é o
    polyglot clássico — e morre na sanitização, sem nada ser gravado."""
    _dns(monkeypatch, IP_PUBLICO)
    _httpx(monkeypatch, _Resposta("image/png", [b"MZ\x90\x00 isto e um executavel"]))

    assert await imagem_web.baixar(URL) is None
    assert not (tmp_path / settings.imagem_web_subpasta).exists()


async def test_baixar_e_fail_soft_quando_a_rede_falha(monkeypatch):
    """404/timeout/hotlink barrado é ROTINA (é por isso que se pedem 5 candidatos).
    A falha vira None para o próximo candidato ser tentado — nunca uma exceção que
    derrubaria o turno inteiro depois de a resposta já ter sido falada."""
    _dns(monkeypatch, IP_PUBLICO)
    _httpx(monkeypatch, _Resposta("image/png", [], erro="403 Forbidden"))

    assert await imagem_web.baixar(URL) is None


# ==========================================================================
# 4. Cache — a mesma foto não é baixada duas vezes
# ==========================================================================
async def test_baixar_devolve_o_cache_sem_ir_a_rede(monkeypatch, tmp_path):
    """"tem foto disso?" repetido não pode pagar rede de novo. E o atalho é ANTES
    do DNS: aqui qualquer toque em socket é falha do teste."""
    pasta = tmp_path / settings.imagem_web_subpasta
    pasta.mkdir(parents=True)
    (pasta / imagem_web.nome_do_cache(URL)).write_bytes(b"webp de ontem")
    _rede_proibida(monkeypatch)

    assert await imagem_web.baixar(URL) == \
        f"{settings.imagem_web_subpasta}/{imagem_web.nome_do_cache(URL)}"


async def test_botao_desligado_nao_toca_em_nada(monkeypatch):
    monkeypatch.setattr(settings, "imagem_web_enabled", False)
    _rede_proibida(monkeypatch)

    assert await imagem_web.baixar(URL) is None
    assert await imagem_web.baixar("") is None


# ==========================================================================
# 5. Puros: `embed` e `creditar`
# ==========================================================================
def test_embed_e_wikilink_e_nunca_url_externa():
    """O contrato com o front, e a razão de a feature existir deste jeito: o chat
    recebe o MESMO wikilink das figuras do livro, resolvido pelo `/api/imagem/`.
    Um `![](https://...)` aqui faria o navegador do dono buscar um domínio de fora
    — que é justamente o que o front recusa de propósito."""
    saida = imagem_web.embed("Figuras/_web/abc.webp")

    assert saida == "![[Figuras/_web/abc.webp]]"
    assert "http" not in saida


def test_embed_vazio_nao_vira_wikilink_quebrado():
    assert imagem_web.embed("") == ""


def test_creditar_diz_fonte_e_pagina():
    """O resto da resposta é do vault do DONO; esta imagem não é. Sem dizer de
    onde veio, as duas coisas se confundem na mesma bolha do chat."""
    assert imagem_web.creditar({"source": "Wikipedia", "url": "https://w/capivara"}) == \
        "(imagem da web — Wikipedia: https://w/capivara)"


def test_creditar_cai_para_o_titulo_quando_nao_ha_source():
    assert imagem_web.creditar({"title": "Capivara adulta", "url": "https://w/c"}) == \
        "(imagem da web — Capivara adulta: https://w/c)"


def test_creditar_com_um_campo_so():
    assert imagem_web.creditar({"url": "https://w/c"}) == "(imagem da web — https://w/c)"
    assert imagem_web.creditar({"source": "Wikipedia"}) == "(imagem da web — Wikipedia)"


def test_creditar_sempre_admite_a_origem_web():
    """Mesmo sem metadado nenhum, a linha existe: "veio da internet" é a parte que
    não pode faltar; o nome do site é o detalhe."""
    assert imagem_web.creditar({"largura": 800}) == "(imagem da web)"
    assert imagem_web.creditar({}) == ""


# ==========================================================================
# 6. `limpar_antigas` — o cache não cresce para sempre
# ==========================================================================
def _semear(pasta: Path, quantos: int) -> list:
    """Cria `quantos` .webp com mtimes crescentes (o mais NOVO por último)."""
    pasta.mkdir(parents=True, exist_ok=True)
    criados = []
    for i in range(quantos):
        p = pasta / f"{i:02d}.webp"
        p.write_bytes(b"webp")
        os.utime(p, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
        criados.append(p)
    return criados


def test_limpar_antigas_mantem_os_mais_recentes(tmp_path):
    pasta = tmp_path / settings.imagem_web_subpasta
    arquivos = _semear(pasta, 5)

    assert imagem_web.limpar_antigas(2) == 3
    assert sorted(p.name for p in pasta.glob("*.webp")) == \
        sorted(p.name for p in arquivos[-2:])


def test_limpar_antigas_nao_mexe_no_que_ja_esta_dentro_do_teto(tmp_path):
    _semear(tmp_path / settings.imagem_web_subpasta, 2)

    assert imagem_web.limpar_antigas(10) == 0


def test_limpar_antigas_zero_e_o_desligar(tmp_path):
    """0 = "não limpa" (é o que a config documenta). Se 0 fosse lido como teto, a
    faxina apagaria o cache INTEIRO — o oposto do que o botão promete."""
    pasta = tmp_path / settings.imagem_web_subpasta
    _semear(pasta, 3)

    assert imagem_web.limpar_antigas(0) == 0
    assert len(list(pasta.glob("*.webp"))) == 3


def test_limpar_antigas_so_toca_nos_webp_dela(tmp_path):
    """A pasta vive DENTRO do vault. Uma faxina que varresse tudo apagaria nota do
    dono — por isso o glob é fechado na extensão que a própria feature grava.

    A nota é a MAIS ANTIGA de propósito: sendo a mais nova, ela sobreviveria por
    acaso (o teto guarda as recentes) e o teste ficaria verde mesmo com o glob
    aberto — foi assim na 1ª escrita deste arquivo, pego na prova de vacuidade."""
    pasta = tmp_path / settings.imagem_web_subpasta
    _semear(pasta, 3)
    nota = pasta / "anotacao.md"
    nota.write_text("nota do dono", encoding="utf-8")
    os.utime(nota, (1_600_000_000, 1_600_000_000))

    assert imagem_web.limpar_antigas(1) == 2       # os 2 .webp velhos, e só eles
    assert nota.is_file()


def test_limpar_antigas_sem_pasta_nao_explode(tmp_path):
    assert imagem_web.limpar_antigas(5) == 0


# ==========================================================================
# 7. `primeira_util` — link morto é rotina, não é fim de linha
# ==========================================================================
async def test_primeira_util_pula_o_candidato_que_falha(monkeypatch):
    """O caso REAL medido em 2026-07-31: o 1º link do buscador deu erro e o 2º
    funcionou. Se a busca desistisse no primeiro, a feature falharia justamente no
    turno em que ela tinha material — daí `imagem_web_candidatos` ser 5 e não 1."""
    tentadas: list = []

    async def _baixar(url):
        tentadas.append(url)
        return "Figuras/_web/ok.webp" if "vivo" in url else None

    monkeypatch.setattr(imagem_web, "baixar", _baixar)
    candidatos = [
        {"image": "https://morto/1.jpg", "url": "https://morto/pagina"},
        {"image": "https://vivo/2.jpg", "url": "https://vivo/pagina", "source": "Vivo"},
    ]

    rel, candidato = await imagem_web.primeira_util(candidatos)

    assert rel == "Figuras/_web/ok.webp"
    assert candidato["source"] == "Vivo"          # o crédito é do que DEU certo
    assert tentadas == ["https://morto/1.jpg", "https://vivo/2.jpg"]   # em ordem


async def test_primeira_util_para_no_primeiro_que_serve(monkeypatch):
    """Em ordem e não em paralelo: o buscador já ranqueou, e baixar cinco para usar
    uma é gastar rede do dono à toa."""
    tentadas: list = []

    async def _baixar(url):
        tentadas.append(url)
        return "Figuras/_web/ok.webp"

    monkeypatch.setattr(imagem_web, "baixar", _baixar)

    await imagem_web.primeira_util([{"image": "https://a/1.jpg"}, {"image": "https://b/2.jpg"}])

    assert tentadas == ["https://a/1.jpg"]


async def test_primeira_util_usa_url_quando_nao_ha_campo_image(monkeypatch):
    """Backends diferentes do ddgs nomeiam o campo de formas diferentes; cair para
    `url` é o que impede a lista inteira de virar chamadas com string vazia."""
    tentadas: list = []

    async def _baixar(url):
        tentadas.append(url)
        return "Figuras/_web/ok.webp" if url else None

    monkeypatch.setattr(imagem_web, "baixar", _baixar)

    assert await imagem_web.primeira_util([{"url": "https://so-url/2.jpg"}]) is not None
    assert tentadas == ["https://so-url/2.jpg"]


async def test_primeira_util_sem_nada_util_devolve_none(monkeypatch):
    async def _baixar(url):
        return None

    monkeypatch.setattr(imagem_web, "baixar", _baixar)

    assert await imagem_web.primeira_util([{"image": "https://a/1.jpg"}]) is None
    assert await imagem_web.primeira_util([]) is None
    assert await imagem_web.primeira_util(None) is None


# ==========================================================================
# 8. `pedido_de_imagem_web` — formato E canal, nunca um só
# ==========================================================================
def test_pedido_de_imagem_web_exige_formato_E_marcador_de_canal():
    """Duas condições porque uma só erra dos dois lados: "me mostra uma imagem de
    capivara" é o caso AUTOMÁTICO (o vault vence, e deve vencer), e "procura na
    internet sobre adubação" é uma pergunta de texto qualquer. Só a soma das duas
    é a ORDEM de buscar foto fora."""
    assert otimizador.pedido_de_imagem_web("procura na internet uma foto de capivara")
    assert otimizador.pedido_de_imagem_web("acha no google uma imagem de tricoma")

    assert not otimizador.pedido_de_imagem_web("me mostra uma imagem de capivara")
    assert not otimizador.pedido_de_imagem_web("tem foto de um tricoma?")
    assert not otimizador.pedido_de_imagem_web("procura na internet sobre adubação")


def test_sem_formato_tira_tambem_o_marcador_de_canal():
    """Numa busca de IMAGEM, "internet"/"google" é a ordem, não o assunto — deixá-la
    na query mandaria o buscador procurar fotos de roteador e de logotipo."""
    assert otimizador.sem_formato("internet foto capivara") == "capivara"
    assert otimizador.sem_formato("google imagem tricoma") == "tricoma"


def test_sem_formato_nao_zera_uma_ordem_sem_assunto():
    """Se sobrasse string vazia, a busca de imagem sairia sem termo nenhum."""
    assert otimizador.sem_formato("internet foto") == "internet foto"


# ==========================================================================
# 9. Integração no pipeline — quem tem precedência
# ==========================================================================
EMBED_LOCAL = "![[Figuras/livro/livro_p0003_f1.webp]]"


class SpyVS:
    """Vectorstore falso: `fontes` controla o que o estágio Banco entrega."""

    def __init__(self, fontes=()) -> None:
        self.search_calls = 0
        self._fontes = list(fontes)

    async def search(self, termos, texto_busca=None, economico=False):
        self.search_calls += 1
        return LocalResult("átomo do banco sobre capivaras", 0.3, True, list(self._fontes))

    async def sync(self):
        pass


class FakeWeb:
    def __init__(self) -> None:
        self.searches = 0

    async def search(self, termo, consulta=None, on_colheita=None):
        self.searches += 1
        return "- FONTE WEB: genérico."

    async def prefetch(self, tema):
        return "- CONTEXTO AMPLO."


class FakeOptimizer:
    """Devolve os termos fixos. "foto capivara" e não "capivara" de propósito: é o
    que o extrator REAL produz (o gate léxico só enxuga a pergunta, não sabe o que
    é formato), e é a palavra "foto" ali dentro que dá o que provar ao pipeline."""

    def __init__(self, termos: str) -> None:
        self._termos = termos

    async def optimize(self, texto, history):
        return self._termos


def _agent(monkeypatch, tmp_path, vs):
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama(["Capivaras ", "são roedores ", "grandes e semiaquáticos."])
    ctx.tts = FakeTts()
    ctx.web = FakeWeb()
    ctx.vectorstore = vs
    monkeypatch.setattr("mente_digital.agent.db.save_chat", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_latency", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.db.save_lacuna", lambda *a, **k: None)
    monkeypatch.setattr("mente_digital.agent.settings.arquivo_chat_dump",
                        str(tmp_path / "dump.md"))
    agent = Agent(ctx)
    agent.optimizer = FakeOptimizer("foto capivara")
    mem = SessionMemory(settings)
    mem.conhecimento_sessao = deque()      # sem RAM: isola a decisão acervo-vs-web
    return agent, mem


def _espiar_imagem_web(agent) -> list:
    """Troca o gancho da web por um registrador. É o gancho INTEIRO que se espia
    (e não a busca lá dentro): o que este bloco julga é a DECISÃO do pipeline —
    entrar ou não na web —, não o que a web devolveria."""
    chamadas: list = []

    async def _falso(send, termos):
        chamadas.append(termos)
        return True

    agent._imagem_da_web = _falso
    return chamadas


def _vault_com_figura(tmp_path) -> str:
    """Uma nota de figura COM o .webp em disco — o `_preparar_figuras` confere os
    dois (nota indexada sem imagem no disco já aconteceu, com a p4 do Cervantes)."""
    pasta = tmp_path / "Figuras" / "livro"
    pasta.mkdir(parents=True, exist_ok=True)
    nota = pasta / "livro_p0003_f1.md"
    nota.write_text(
        "---\norigem: Livro X\n---\n"
        "## Capivara adulta na margem\n"
        "Capivara adulta na margem do rio.\n"
        f"{EMBED_LOCAL}\n",
        encoding="utf-8")
    (pasta / "livro_p0003_f1.webp").write_bytes(b"fake")
    return str(nota)


async def test_pede_imagem_e_o_acervo_nao_tem_entao_a_web_entra(monkeypatch, tmp_path):
    """O turno que originou a feature: o dono pediu para VER e o vault respondeu
    em texto, sem figura nenhuma. Antes, a pergunta ficava sem resposta."""
    agent, mem = _agent(monkeypatch, tmp_path, SpyVS(fontes=["nota_sem_figura.md"]))
    chamadas = _espiar_imagem_web(agent)
    send, _ = make_send()

    await agent.pipeline_resposta("tem foto de uma capivara?", send, mem)

    # E com os TERMOS já limpos da palavra de formato: o extrator entregou "foto
    # capivara" e o que vai ao buscador de imagem é "capivara". Mandar "foto" junto
    # devolveria fotos de câmera fotográfica — é a mesma palavra que, na busca do
    # vault, trouxe os baldes de YOLO/Ollama em 2026-07-31.
    assert chamadas == ["capivara"]


async def test_o_acervo_tendo_figura_a_web_NAO_entra(monkeypatch, tmp_path):
    """A precedência: a foto do livro do dono responde melhor, já está em disco e
    não gasta rede. A web é o vazio, não a primeira opção."""
    nota = _vault_com_figura(tmp_path)
    agent, mem = _agent(monkeypatch, tmp_path, SpyVS(fontes=[nota]))
    chamadas = _espiar_imagem_web(agent)
    send, enviados = make_send()

    await agent.pipeline_resposta("tem foto de uma capivara?", send, mem)

    assert chamadas == []
    # e o teste não passou por ter quebrado o turno: a figura LOCAL apareceu.
    assert EMBED_LOCAL in textos_de_tokens(enviados)


async def test_pedido_EXPLICITO_de_web_vence_a_figura_local(monkeypatch, tmp_path):
    """Ordem do dono (2026-07-31): quem escreve "procura na internet" não pode
    receber a foto do livro e ficar sem saída. O vault vencer é o certo no caso
    automático e é desobediência quando ele MANDOU buscar fora."""
    nota = _vault_com_figura(tmp_path)
    agent, mem = _agent(monkeypatch, tmp_path, SpyVS(fontes=[nota]))
    chamadas = _espiar_imagem_web(agent)
    send, enviados = make_send()

    await agent.pipeline_resposta("procura na internet uma foto de capivara", send, mem)

    assert chamadas == ["capivara"]
    # e o acervo não foi suprimido: as duas imagens aparecem, cada uma creditada.
    assert EMBED_LOCAL in textos_de_tokens(enviados)


async def test_pergunta_sem_pedido_de_imagem_nunca_chama_a_web_de_imagem(monkeypatch, tmp_path):
    """O gate mais importante do bloco: a busca de imagem é a única porta do
    projeto que traz PIXEL de fora. Uma pergunta de texto qualquer não pode
    abri-la — nem pagar a rede dela."""
    agent, mem = _agent(monkeypatch, tmp_path, SpyVS(fontes=["nota.md"]))
    chamadas = _espiar_imagem_web(agent)
    send, _ = make_send()

    await agent.pipeline_resposta("o que come uma capivara?", send, mem)

    assert chamadas == []


# ==========================================================================
# 9b. O gancho em si — a resposta honesta e o canal por onde ela sai
# ==========================================================================
class WebComImagens:
    def __init__(self, candidatos) -> None:
        self.candidatos = list(candidatos)
        self.pedidos: list = []

    async def buscar_imagens(self, termo, max_results):
        self.pedidos.append((termo, max_results))
        return self.candidatos


async def test_imagem_da_web_emite_wikilink_e_credito(monkeypatch, tmp_path):
    """A saída visível, ponta a ponta: wikilink local (nunca URL externa) mais a
    linha de origem, no canal de TEXTO — o TTS não fala caminho de arquivo."""
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama([])
    ctx.tts = FakeTts()
    # O título menciona o assunto de propósito: desde 2026-07-31 o candidato passa
    # pelo aterramento léxico do `casa_com_o_pedido` antes de ser baixado (o teste de
    # 50 casos pegou um formulário de currículo entregue como "foto de estômato").
    # Um candidato mudo seria descartado antes de chegar ao canal que este teste mede.
    ctx.web = WebComImagens([{"image": "https://w/c.jpg", "url": "https://w/pag",
                              "title": "Capivara na beira do rio",
                              "source": "Wikipedia"}])
    agent = Agent(ctx)

    async def _baixou(url):
        return "Figuras/_web/abc.webp"

    monkeypatch.setattr(imagem_web, "baixar", _baixou)
    send, enviados = make_send()

    assert await agent._imagem_da_web(send, "capivara") is True

    saida = textos_de_tokens(enviados)
    assert "![[Figuras/_web/abc.webp]]" in saida
    assert "Wikipedia: https://w/pag" in saida
    assert "![](" not in saida                 # nunca markdown de URL externa
    assert ctx.web.pedidos == [("capivara", settings.imagem_web_candidatos)]


async def test_imagem_da_web_sem_achado_responde_a_pergunta_feita(monkeypatch):
    """Pergunta feita é pergunta que merece resposta, mesmo negativa: sem esta
    frase o turno responde texto e deixa o "tem foto?" no ar — foi o que o dono
    viu na tela."""
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama([])
    ctx.tts = FakeTts()
    ctx.web = WebComImagens([])
    agent = Agent(ctx)
    send, enviados = make_send()

    assert await agent._imagem_da_web(send, "capivara") is False
    assert "nem na web" in textos_de_tokens(enviados)


async def test_imagem_da_web_e_fail_soft_se_o_buscador_explode(monkeypatch):
    """A imagem entra DEPOIS de a resposta já ter sido falada. Uma exceção aqui
    marcaria o turno inteiro como erro por causa de um bônus que faltou."""
    class WebQuebrada:
        async def buscar_imagens(self, termo, max_results):
            raise RuntimeError("backend do ddgs caiu")

    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama([])
    ctx.tts = FakeTts()
    ctx.web = WebQuebrada()
    agent = Agent(ctx)
    send, enviados = make_send()

    assert await agent._imagem_da_web(send, "capivara") is False
    assert "nem na web" in textos_de_tokens(enviados)
