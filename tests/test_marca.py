"""A marca do app (mente_digital/marca.py) — o desenho é código, então é testável.

Três coisas que estes testes existem para impedir de voltar:

1. **Um `.ico` com um quadro só.** O Pillow aceita `sizes=[...]` e, sem
   `append_images`, RASTERIZA todos a partir do maior — que é justamente a
   redução ruim que se quis evitar. O arquivo continua válido, o Windows continua
   exibindo, e a marca fica borrada na barra de tarefas sem nada falhar.
2. **A escolha do dono virando outra coisa.** Ele pediu a marca SEM azulejo
   (2026-08-02); um `letras` que voltasse a ter fundo passaria despercebido em
   revisão de diff, mas não passa em `test_letras_tem_fundo_transparente`.
3. **Ícone derrubando o app.** Todo caminho aqui é acabamento — nenhum pode
   levantar exceção no boot.
"""
from __future__ import annotations

import pytest

from mente_digital import marca


# --------------------------------------------------------------------------- #
# Gradiente                                                                    #
# --------------------------------------------------------------------------- #
def test_cor_nas_pontas_e_a_paleta_da_interface():
    """As pontas são as cores do `linear-gradient` do index.html, sem desvio."""
    assert marca.cor_em(0.0) == marca.AZUL
    assert marca.cor_em(1.0) == marca.ROSA


def test_cor_no_meio_e_a_parada_do_roxo():
    """0,52 é onde o CSS põe o roxo — não 0,5. Trocar isso desalinha a marca do
    gradiente da interface de um jeito que só se vê lado a lado."""
    assert marca.cor_em(0.52) == marca.ROXO


def test_cor_fora_da_faixa_nao_estoura():
    assert marca.cor_em(-3.0) == marca.AZUL
    assert marca.cor_em(9.0) == marca.ROSA


def test_cor_e_monotonica_no_canal_vermelho():
    """Azul (66) -> rosa (217): o vermelho só sobe. Pega gradiente invertido."""
    vermelhos = [marca.cor_em(i / 20)[0] for i in range(21)]
    assert vermelhos == sorted(vermelhos)


# --------------------------------------------------------------------------- #
# Desenho                                                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("variante", marca.VARIANTES)
@pytest.mark.parametrize("tamanho", (16, 32, 256))
def test_toda_variante_desenha_no_tamanho_pedido(variante, tamanho):
    img = marca.desenhar_marca(tamanho, variante)
    assert img.size == (tamanho, tamanho)
    assert img.mode == "RGBA"


@pytest.mark.parametrize("variante", marca.VARIANTES)
def test_toda_variante_tem_pixel_visivel(variante):
    """Um desenho totalmente transparente passaria em tudo acima e seria um
    ícone INVISÍVEL na barra de tarefas."""
    img = marca.desenhar_marca(64, variante)
    assert max(img.getchannel("A").getdata()) > 200


def test_variante_desconhecida_e_erro_explicito():
    with pytest.raises(ValueError, match="variante desconhecida"):
        marca.desenhar_marca(32, "gradiente_arco_iris")


def test_letras_tem_fundo_transparente():
    """A escolha do dono: SEM azulejo. Os quatro cantos têm de estar vazios."""
    img = marca.desenhar_marca(64, "letras")
    alfa = img.getchannel("A")
    for xy in ((0, 0), (63, 0), (0, 63), (63, 63)):
        assert alfa.getpixel(xy) == 0


def test_variantes_com_azulejo_pintam_o_canto():
    """O contraponto do teste acima: nas variantes de azulejo o canto é OPACO
    (o arredondamento come só a quina). Sem este par, um bug que zerasse o alfa
    de todo mundo passaria batido."""
    alfa = marca.desenhar_marca(64, "monograma").getchannel("A")
    assert alfa.getpixel((32, 2)) > 200


def test_apagado_e_cinza_e_colorido_e_nao():
    """O ícone da bandeja APAGA quando os modelos descansam — é o único retorno
    visual disso com a janela minimizada."""
    def saturacao(img):
        pixels = [p[:3] for p in img.convert("RGBA").getdata() if p[3] > 200]
        return max(max(p) - min(p) for p in pixels)

    assert saturacao(marca.desenhar_marca(64, "letras", ativo=True)) > 40
    assert saturacao(marca.desenhar_marca(64, "letras", ativo=False)) < 25


# --------------------------------------------------------------------------- #
# Arquivo .ico                                                                 #
# --------------------------------------------------------------------------- #
def test_ico_guarda_todos_os_tamanhos_como_quadros_proprios(tmp_path):
    """O teste que justifica o `append_images`. Sem ele o Pillow grava UM quadro
    e deriva os outros por redução própria — arquivo válido, marca borrada."""
    from PIL import Image

    alvo = marca.gerar_ico(tmp_path / "m.ico", "letras")
    with Image.open(alvo) as img:
        assert img.ico.sizes() == {(n, n) for n in marca.TAMANHOS_ICO}


def test_ico_inclui_o_16_que_a_barra_de_tarefas_usa():
    """16 px é o tamanho que a barra de tarefas realmente pede. Perdê-lo da
    lista faria o Windows reduzir o de 256 sozinho, que é o borrão original."""
    assert 16 in marca.TAMANHOS_ICO and 32 in marca.TAMANHOS_ICO


def test_caminho_ico_gera_uma_vez_e_reusa(tmp_path):
    alvo = marca.caminho_ico(tmp_path, "letras")
    assert alvo.exists()
    antes = alvo.stat().st_mtime_ns
    assert marca.caminho_ico(tmp_path, "letras") == alvo
    assert alvo.stat().st_mtime_ns == antes      # não regerou


def test_nome_do_ico_carrega_a_versao_do_desenho(tmp_path):
    """Sem a versão no nome, mexer no traçado não teria efeito em quem já rodou
    o app: `caminho_ico` só gera quando o arquivo não existe."""
    alvo = marca.caminho_ico(tmp_path, "letras")
    assert f"_v{marca.VERSAO_DESENHO}.ico" in alvo.name


def test_falha_ao_gerar_nao_levanta(tmp_path, monkeypatch):
    """Ícone é acabamento. Um erro aqui devolve caminho inexistente e o app abre
    sem marca — nunca uma exceção no boot."""
    def explodir(*_a, **_k):
        raise OSError("disco cheio")

    monkeypatch.setattr(marca, "gerar_ico", explodir)
    alvo = marca.caminho_ico(tmp_path, "letras")
    assert not alvo.exists()


# --------------------------------------------------------------------------- #
# Variante configurada                                                         #
# --------------------------------------------------------------------------- #
def test_variante_padrao_e_a_escolhida_pelo_dono():
    assert marca.VARIANTE_PADRAO == "letras"
    assert marca.VARIANTE_PADRAO in marca.VARIANTES


def test_a_marca_oficial_nao_tem_fundo():
    """Requisito explícito do dono: sem azulejo, como os ícones vizinhos na barra
    de tarefas dele. Um azulejo voltando passaria despercebido em diff."""
    alfa = marca.desenhar_marca(64, marca.VARIANTE_PADRAO).getchannel("A")
    assert all(alfa.getpixel(xy) == 0 for xy in ((0, 0), (63, 0), (0, 63), (63, 63)))


def test_empilhado_existe_e_preenche_o_quadrado():
    """A alternativa que o dono VIU e recusou, mantida no catálogo.

    Ela existe porque "sem fundo igual aos vizinhos" tem duas metades, e o
    deitado só atende uma: os ícones da barra dele ocupam o slot inteiro, e o
    "MD" deitado (~2:1) usa metade da altura. Ele escolheu o deitado assim mesmo,
    com os dois lado a lado nos tamanhos reais — então isto NÃO é o padrão. Fica
    testado para continuar sendo uma troca de uma variável de ambiente."""
    alfa = marca.desenhar_marca(64, "empilhado").getchannel("A")
    caixa = alfa.getbbox()                      # (x0, y0, x1, y1) do não-transparente
    altura, largura = caixa[3] - caixa[1], caixa[2] - caixa[0]
    assert altura / 64 > 0.85                   # usa a altura toda
    assert 0.55 < largura / altura < 1.45       # aproximadamente quadrado
    assert alfa.getpixel((0, 0)) == 0           # e continua sem fundo


def test_env_troca_a_variante(monkeypatch):
    monkeypatch.setenv("MENTE_APP_ICONE", "faisca")
    assert marca.variante_configurada() == "faisca"


def test_env_com_lixo_cai_no_padrao(monkeypatch):
    """Escrever errado no .env não pode deixar o app sem ícone — o projeto já
    pagou caro por chave engolida em silêncio (a pesquisa agendada, 2026-08-02)."""
    monkeypatch.setenv("MENTE_APP_ICONE", "nao_existe")
    assert marca.variante_configurada() == marca.VARIANTE_PADRAO


def test_env_ignora_caixa_e_espaco(monkeypatch):
    monkeypatch.setenv("MENTE_APP_ICONE", "  Faisca ")
    assert marca.variante_configurada() == "faisca"
