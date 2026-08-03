"""
Mente Digital — casca de APLICATIVO (janela nativa, sem navegador).

    python app.py                     # sobe o servidor e abre a janela
    python app.py --remoto http://192.168.0.10:8000   # só a janela, servidor alheio
    python app.py --sem-janela        # só o servidor (igual `python main.py`)

O que isto é: uma janela WebView2 sem moldura servindo a MESMA SPA de sempre
(`templates/index.html`). Não há segunda interface para manter em sincronia — o
navegador, esta janela, o container e (depois) o app Android consomem o mesmo
front e o mesmo protocolo WebSocket.

Três decisões que valem o comentário:

1. **O uvicorn roda NESTE processo, numa thread.** Não é preguiça: o `main.py`
   guarda o container de serviços em `app.state.ctx` ANTES de carregar qualquer
   modelo, então a tela de boot lê `ctx.llama.ready`/`ctx.stt.ready`/… direto da
   memória e mostra progresso REAL desde o segundo zero — inclusive durante os
   ~12 s em que a porta ainda nem existe (o uvicorn só faz o bind depois que o
   lifespan termina; ver rede.py). Por HTTP esse trecho seria cego, e a barra
   teria de ser cronômetro fingindo de progresso.

2. **`private_mode=False` + `storage_path` próprio.** O default do pywebview
   descarta localStorage e o perfil do WebView2 a cada execução — o token de
   acesso (#7) e a permissão de microfone seriam pedidos de novo toda vez.

3. **`easy_drag=False`.** Com `frameless=True` o pywebview arrastaria a janela
   por QUALQUER ponto, o que num app que rola e seleciona texto é sofrimento.
   Desligado, só o elemento `.pywebview-drag-region` (a barra de título que o
   próprio front desenha) move a janela.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# O relógio do boot começa AQUI, na primeira linha executável do processo — antes
# dos imports pesados, do uvicorn e da janela. É a melhor aproximação que o próprio
# processo tem de "duplo-clique -> utilizável", e é o número que a tela de boot
# mostra. Cronometrar a partir do poller (que só nasce depois de tudo isso) daria
# um número menor que a espera real, o que seria propaganda, não medição.
_T0 = time.time()

# Mesmas guardas do main.py, e ANTES de qualquer import pesado de ML.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

BASE_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# Marcos de boot                                                              #
# --------------------------------------------------------------------------- #
# O peso de cada marco no percentual. Não é estimativa de tempo — é quanto do
# "aplicativo pronto" aquele serviço representa. Somam 100 de propósito: um marco
# que não soma some da conta e a barra nunca fecha.
#
# A tela de boot só libera com TODOS verdes (decisão do dono, 2026-08-02): a
# janela não abre "meio pronta" com o Whisper ainda subindo por baixo.
MARCOS: tuple[tuple[str, str, int], ...] = (
    # chave      rótulo      peso
    ("servidor", "Servidor",   10),
    ("vault",    "Vault",      20),
    ("llm",      "Modelo",     25),
    ("stt",      "Escuta",     15),
    ("voz",      "Voz",        10),
    ("porta",    "Rede",        5),
    # O marco que faltava, e que custou caro: o `_boot` do main.py despacha malha,
    # sync do Chroma e o XTTS para a RAM SEM segurar a porta. Sem contá-los, a tela
    # anunciava "tudo pronto" com três jobs disputando GPU e disco, e a primeira
    # pergunta do dono pagava a conta (44,8 tok/s contra 93-113 normais, medido em
    # 2026-08-02). Segurar a tela até aqui é o que faz a PRIMEIRA pergunta correr
    # tão rápido quanto as seguintes: a espera não some, sai de dentro da resposta
    # e vai para a barra de progresso, onde é esperada.
    ("fundo",    "Índice e ajustes", 15),
)

# Rede de segurança para a degradação graciosa que o projeto inteiro pratica: um
# serviço pode falhar no load e deixar `ready=False` PARA SEMPRE (o `load` de cada
# um tem pára-quedas próprio e o app segue sem ele — ver main.py). Sem este teto,
# "espera tudo" viraria deadlock na tela de boot, contradizendo a arquitetura. Ao
# estourar, a tela oferece entrar assim mesmo e DIZ quem não subiu — não entra
# escondido.
SEGUNDOS_ATE_OFERECER_SAIDA = 150


def calcular_progresso(prontos: dict[str, bool]) -> tuple[int, bool]:
    """(percentual, tudo_pronto) a partir dos marcos alcançados. Puro/testável."""
    total = sum(peso for chave, _, peso in MARCOS if prontos.get(chave))
    return min(total, 100), all(prontos.get(chave) for chave, _, _ in MARCOS)


def rotulo_estagio(prontos: dict[str, bool]) -> str:
    """A frase grande da tela de boot — o próximo marco que falta, na ordem real
    em que o `_boot` do main.py os alcança."""
    if not prontos.get("servidor"):
        return "Acordando a mente"
    if not prontos.get("stt"):
        return "Afinando a escuta"
    if not prontos.get("vault"):
        return "Abrindo o vault"
    if not prontos.get("llm"):
        return "Carregando o modelo"
    if not prontos.get("voz"):
        return "Preparando a voz"
    if not prontos.get("porta"):
        return "Abrindo a porta"
    if not prontos.get("fundo"):
        return "Terminando o índice"
    return "Tudo pronto"


# --------------------------------------------------------------------------- #
# Geometria da janela (lembra tamanho/posição entre execuções)                 #
# --------------------------------------------------------------------------- #
ARQ_GEOMETRIA = BASE_DIR / "dados" / "app_janela.json"


def ler_geometria(padrao: tuple[int, int]) -> dict:
    """Tamanho/posição salvos. Qualquer defeito no arquivo volta ao padrão —
    geometria corrompida jamais impede o app de abrir."""
    dados = {"largura": padrao[0], "altura": padrao[1], "x": None, "y": None}
    try:
        if ARQ_GEOMETRIA.exists():
            salvo = json.loads(ARQ_GEOMETRIA.read_text(encoding="utf-8"))
            for chave in dados:
                if isinstance(salvo.get(chave), int):
                    dados[chave] = salvo[chave]
    except Exception as exc:                       # noqa: BLE001 - nunca fatal
        print(f"[APP] geometria ilegível, usando o padrão: {exc}")
    return dados


def salvar_geometria(janela) -> None:
    try:
        ARQ_GEOMETRIA.parent.mkdir(parents=True, exist_ok=True)
        ARQ_GEOMETRIA.write_text(json.dumps({
            "largura": int(janela.width), "altura": int(janela.height),
            "x": int(janela.x), "y": int(janela.y),
        }), encoding="utf-8")
    except Exception as exc:                       # noqa: BLE001
        print(f"[APP] não consegui salvar a geometria: {exc}")


# --------------------------------------------------------------------------- #
# Ponte JS -> Python (os botões da barra de título que o front desenha)        #
# --------------------------------------------------------------------------- #
class Ponte:
    """Exposta ao front como `window.pywebview.api`.

    Só o que uma barra de título precisa. A presença de `window.pywebview` é
    também o SINAL que o `index.html` usa para decidir desenhar a barra: no
    navegador ela não existe, e o front usa a moldura do navegador.

    ⚠ TODO atributo que NÃO for método vai com `_` na frente. O pywebview varre
    `dir(js_api)` para montar a API do JS e RECURSIONA em atributo não-chamável
    (`util.py:190-206`), pulando só os que começam com `_`. Guardar a janela como
    `self.janela` fez ele descer por `janela.native` até o controle WinForms:
    milhares de linhas de `maximum recursion depth exceeded` e de
    `CoreWebView2 can only be accessed from the UI thread` no log — porque a
    varredura ainda tocava objetos COM de outra thread. Medido em 2026-08-02."""

    def __init__(self) -> None:
        self._janela = None
        self._maximizada = False
        # Levantado pelo botão de escape da tela de boot (só aparece depois de
        # SEGUNDOS_ATE_OFERECER_SAIDA). Lido pelo poller, que roda noutra thread —
        # atribuição de bool em CPython é atômica, não precisa de lock.
        self._entrada_forcada = False
        # Modo `--standby`: o poller PARA aqui depois de mandar o servidor dormir, e
        # só segue quando alguém pede para acordar (botão da tela ou "Abrir" na
        # bandeja). Event e não bool porque a espera é longa e ociosa — um sleep em
        # laço acordaria a CPU a cada tique por horas, exatamente o oposto do que o
        # modo economia existe para fazer.
        self._acordar = threading.Event()

    # Todo MÉTODO público daqui vira função no `window.pywebview.api` — então só
    # entram os que o front realmente chama. O Python lê `_janela`/`_entrada_forcada`
    # direto (mesmo módulo), sem precisar de setter exposto ao JS.
    def forcar_entrada(self) -> None:
        self._entrada_forcada = True

    def acordar(self) -> None:
        self._acordar.set()

    def minimizar(self) -> None:
        if self._janela:
            self._janela.minimize()

    def alternar_maximizar(self) -> bool:
        if not self._janela:
            return False
        # O pywebview não expõe "está maximizada?", então o estado é nosso.
        # `restore()` desmaximiza E desminimiza — aqui só o primeiro caso ocorre.
        if self._maximizada:
            self._janela.restore()
        else:
            self._janela.maximize()
        self._maximizada = not self._maximizada
        return self._maximizada

    def fechar(self) -> None:
        if self._janela:
            salvar_geometria(self._janela)
            self._janela.destroy()

    def info(self) -> dict:
        """O front pergunta em que casca está rodando."""
        return {"nativo": True, "plataforma": sys.platform, "maximizada": self._maximizada}


# --------------------------------------------------------------------------- #
# Tela de boot                                                                 #
# --------------------------------------------------------------------------- #
def _montar_splash(voz_preguicosa: bool) -> str:
    """A tela de espera, com a estrutura do ExitLag e a paleta do Mente Digital.

    Documento SOLTO (`html=`, origem about:blank): não pode depender do servidor,
    porque o motivo dela existir é justamente o servidor ainda não existir. Por
    isso é autocontida — nem CSS nem fonte nem ícone vêm de fora.

    `voz_preguicosa` vira RÓTULO na tela. Com o XTTS preguiçoso (o default), a voz
    só sobe quando o microfone abre: contá-la como pronta é correto, mas fazê-lo
    calado seria um ponto verde mentindo. Ela aparece como "Voz · sob demanda"."""
    def rotular(chave: str, rot: str) -> str:
        return f"{rot} · sob demanda" if (chave == "voz" and voz_preguicosa) else rot

    legenda = "".join(
        f'<div class="srv" data-srv="{chave}"><i></i><span>{rotular(chave, rot)}</span></div>'
        for chave, rot, _ in MARCOS if chave != "porta"
    )
    return _SPLASH_HTML.replace("__LEGENDA__", legenda)


_SPLASH_HTML = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Mente Digital</title></head><body>
<style>
/* ------------------------------------------------------------------ *
 * PALETA — o único lugar a mexer para re-skinar a casca.
 * O gradiente é o do próprio app (azul -> roxo -> rosa), para a janela
 * combinar com as transições de tela lá dentro. Para a leitura literal
 * do ExitLag, troque o bloco por:
 *     --acc:#E8353E; --acc1:#F0323C; --acc2:#E8353E; --acc3:#8E1219;
 * ------------------------------------------------------------------ */
:root{
  --acc1:#4285F4; --acc2:#9B72CB; --acc3:#D96570;
  --acc:#8AB4F8; --acc-glow:rgba(138,180,248,.40);
  --bg:#0B0B0D; --bg2:#141318; --linha:rgba(255,255,255,.07);
  --txt:#F2F2F3; --txt2:#8B8F96; --trilho:#2A2C31;
  --font:'Segoe UI Variable Display','Segoe UI',system-ui,-apple-system,Roboto,sans-serif;
}
@media (prefers-color-scheme: light){
  :root{ --bg:#F7F7F9; --bg2:#FFFFFF; --linha:rgba(0,0,0,.09);
         --txt:#1A1B1E; --txt2:#6B7076; --trilho:#E2E3E7; }
}
*{margin:0;padding:0;box-sizing:border-box;font-family:var(--font);
  -webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--txt);display:flex;flex-direction:column;
  /* O brilho difuso que sobe do rodapé — a assinatura visual do ExitLag,
     aqui na cor do app em vez do vermelho dele. */
  background-image:
    radial-gradient(120% 70% at 50% 118%, color-mix(in srgb, var(--acc2) 26%, transparent) 0%, transparent 62%),
    radial-gradient(90% 60% at 12% 104%, color-mix(in srgb, var(--acc3) 20%, transparent) 0%, transparent 60%);
  opacity:0;animation:entrar .45s ease forwards}
@keyframes entrar{to{opacity:1}}
body.saindo{opacity:0;transition:opacity .32s ease}

/* ---------- barra de título ---------- */
#barra{height:42px;flex-shrink:0;display:flex;align-items:center;
  border-bottom:1px solid var(--linha);position:relative}
#marca{position:absolute;left:50%;transform:translateX(-50%);
  font-size:.78rem;font-weight:600;letter-spacing:.22em;color:var(--txt);
  display:flex;align-items:center;gap:.55em;pointer-events:none}
#marca b{font-weight:600;background:linear-gradient(100deg,var(--acc1),var(--acc2),var(--acc3));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
#arrasto{flex:1;height:100%}
.janela-btn{width:46px;height:42px;border:0;background:none;color:var(--txt2);
  cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s}
.janela-btn:hover{background:rgba(127,127,127,.16);color:var(--txt)}
.janela-btn.sair:hover{background:#E81123;color:#fff}

/* ---------- palco ---------- */
#palco{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:34px;padding:0 34px 12px}
#estagio{font-size:clamp(1.35rem,4.4vw,1.9rem);font-weight:400;letter-spacing:.005em;
  text-align:center;transition:opacity .3s}

/* ---------- anel ---------- */
#anel{position:relative;width:216px;height:216px;flex-shrink:0}
#anel svg{width:100%;height:100%;transform:rotate(-90deg)}
#arco{transition:stroke-dashoffset .55s cubic-bezier(.22,1,.36,1);
  filter:drop-shadow(0 0 7px var(--acc-glow))}
#orbita{position:absolute;inset:0;transition:transform .55s cubic-bezier(.22,1,.36,1)}
#orbita::after{content:'';position:absolute;top:6px;left:50%;width:9px;height:9px;
  margin-left:-4.5px;border-radius:50%;background:var(--acc3);
  box-shadow:0 0 14px 3px var(--acc-glow)}
/* Enquanto o servidor não publicou marco nenhum não há percentual honesto a
   mostrar — então o anel GIRA (indeterminado) em vez de fingir um número. */
#anel.indeterminado #arco{transition:none}
#anel.indeterminado{animation:girar 1.5s linear infinite}
@keyframes girar{to{transform:rotate(360deg)}}
#centro{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:5px}
#pct{font-size:2.75rem;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums;
  background:linear-gradient(100deg,var(--acc1),var(--acc2),var(--acc3));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
#anel.indeterminado #pct{opacity:0}
#detalhe{font-size:.76rem;color:var(--txt2);text-align:center;
  font-variant-numeric:tabular-nums;min-height:1.1em}

/* ---------- barra horizontal ---------- */
#trilho{width:min(100%,470px);height:5px;border-radius:99px;background:var(--trilho);position:relative}
#preenche{height:100%;border-radius:99px;width:0;
  background:linear-gradient(90deg,var(--acc1),var(--acc2),var(--acc3));
  transition:width .55s cubic-bezier(.22,1,.36,1)}
#knob{position:absolute;top:50%;left:0;width:13px;height:13px;margin:-6.5px 0 0 -6.5px;
  border-radius:50%;background:#fff;box-shadow:0 0 0 3px var(--acc-glow),0 1px 5px rgba(0,0,0,.5);
  transition:left .55s cubic-bezier(.22,1,.36,1)}

/* ---------- legenda de serviços ---------- */
#servicos{display:flex;flex-wrap:wrap;justify-content:center;gap:9px 20px}
.srv{display:flex;align-items:center;gap:7px;font-size:.76rem;color:var(--txt2);
  opacity:.5;transition:opacity .35s}
.srv i{width:7px;height:7px;border-radius:50%;background:var(--trilho);transition:.35s}
.srv.ok{opacity:1;color:var(--txt)}
.srv.ok i{background:var(--acc3);box-shadow:0 0 9px 1px var(--acc-glow)}
.srv.carregando i{background:var(--acc);animation:pulsa 1.1s ease-in-out infinite}
@keyframes pulsa{0%,100%{opacity:.35;transform:scale(.8)}50%{opacity:1;transform:scale(1.15)}}

/* Escape da degradação graciosa: aparece só se um serviço não subir no tempo. */
#escape,#dormindo{display:none;flex-direction:column;align-items:center;gap:10px;
  animation:entrar .4s ease}
#escape.on,#dormindo.on{display:flex}
#escape p,#dormindo p{font-size:.78rem;color:var(--txt2);text-align:center;max-width:400px;line-height:1.5}
#escape button,#dormindo button{background:none;border:1px solid var(--linha);color:var(--txt);
  padding:9px 20px;border-radius:22px;font-size:.82rem;cursor:pointer;transition:.18s}
#escape button:hover,#dormindo button:hover{background:color-mix(in srgb, var(--txt) 10%, transparent);border-color:var(--acc)}
/* Modo economia: o anel apaga em vez de sumir — a janela continua sendo a mesma
   coisa, só que em repouso. Esconder tudo daria a impressão de app fechado. */
body.dormindo #anel,body.dormindo #trilho,body.dormindo #servicos{opacity:.22;
  transition:opacity .4s ease}
body.dormindo #anel{animation:none}
#dormindo button.destaque{border-color:var(--acc);color:var(--txt);
  background:color-mix(in srgb, var(--acc) 14%, transparent)}

#rodape{position:absolute;bottom:14px;left:0;right:0;text-align:center;
  font-size:.68rem;color:var(--txt2);opacity:.55}
</style>

<div id="barra">
  <div id="arrasto" class="pywebview-drag-region"></div>
  <div id="marca">MENTE<b>DIGITAL</b></div>
  <button class="janela-btn" onclick="api('minimizar')" title="Minimizar" aria-label="Minimizar">
    <svg width="11" height="11" viewBox="0 0 12 12"><path d="M1 6h10" stroke="currentColor" stroke-width="1.2"/></svg></button>
  <button class="janela-btn sair" onclick="api('fechar')" title="Fechar" aria-label="Fechar">
    <svg width="11" height="11" viewBox="0 0 12 12"><path d="M1.5 1.5l9 9M10.5 1.5l-9 9" stroke="currentColor" stroke-width="1.2"/></svg></button>
</div>

<div id="palco">
  <div id="estagio">Acordando a mente</div>
  <div id="anel" class="indeterminado">
    <svg viewBox="0 0 100 100" aria-hidden="true">
      <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="var(--acc1)"/><stop offset="52%" stop-color="var(--acc2)"/>
        <stop offset="100%" stop-color="var(--acc3)"/></linearGradient></defs>
      <circle cx="50" cy="50" r="45" fill="none" stroke="var(--trilho)" stroke-width="3"/>
      <circle id="arco" cx="50" cy="50" r="45" fill="none" stroke="url(#g)" stroke-width="3.4"
              stroke-linecap="round" stroke-dasharray="282.74" stroke-dashoffset="212"/>
    </svg>
    <div id="orbita"></div>
    <div id="centro"><div id="pct">0%</div></div>
  </div>
  <div id="detalhe">iniciando…</div>
  <div id="trilho"><div id="preenche"></div><div id="knob"></div></div>
  <div id="servicos">__LEGENDA__</div>
  <div id="escape">
    <p id="escape-txt"></p>
    <button onclick="forcar()">Entrar assim mesmo</button>
  </div>
  <!-- Modo economia (`--standby`): o servidor está de pé e os modelos, soltos. -->
  <div id="dormindo">
    <p id="dormindo-txt"></p>
    <button class="destaque" onclick="acordar()">Acordar o assistente</button>
  </div>
</div>
<div id="rodape">100% local · nada sai desta máquina</div>

<script>
const CIRC = 2 * Math.PI * 45;
function api(m){ if (window.pywebview && window.pywebview.api) window.pywebview.api[m](); }

// Chamado do Python (evaluate_js) a cada tique do poller. Nada aqui inventa
// número: pct e serviços vêm dos marcos REAIS lidos do AppContext.
function boot(e){
  const anel = document.getElementById('anel');
  const indet = e.pct <= 0;
  anel.classList.toggle('indeterminado', indet);
  document.getElementById('estagio').textContent = e.estagio;
  document.getElementById('detalhe').textContent = e.detalhe || '';
  document.getElementById('pct').textContent = e.pct + '%';
  if (!indet){
    document.getElementById('arco').style.strokeDashoffset = CIRC * (1 - e.pct/100);
    document.getElementById('orbita').style.transform = 'rotate(' + (e.pct*3.6) + 'deg)';
  }
  document.getElementById('preenche').style.width = e.pct + '%';
  document.getElementById('knob').style.left = e.pct + '%';
  document.querySelectorAll('.srv').forEach(el => {
    const ok = !!(e.servicos || {})[el.dataset.srv];
    el.classList.toggle('ok', ok);
    el.classList.toggle('carregando', !ok);
  });
  // Só depois do teto: o Python manda o texto de quem não subiu.
  const esc = document.getElementById('escape');
  esc.classList.toggle('on', !!e.escape);
  if (e.escape) document.getElementById('escape-txt').textContent = e.escape;
}
function forcar(){
  document.getElementById('escape').classList.remove('on');
  document.getElementById('estagio').textContent = 'Entrando…';
  api('forcar_entrada');
}

// Modo economia. Chamado do Python quando o `--standby` solta os modelos: a
// janela não vira outra tela, ela apaga — o assistente continua de pé, só não
// está ocupando a máquina.
function dormir(e){
  document.body.classList.add('dormindo');
  document.getElementById('anel').classList.remove('indeterminado');
  document.getElementById('estagio').textContent = 'Descansando';
  document.getElementById('detalhe').textContent = e.detalhe || '';
  document.getElementById('escape').classList.remove('on');
  document.getElementById('dormindo-txt').textContent = e.texto || '';
  document.getElementById('dormindo').classList.add('on');
}
function acordar(){
  document.body.classList.remove('dormindo');
  document.getElementById('dormindo').classList.remove('on');
  document.getElementById('estagio').textContent = 'Acordando…';
  document.getElementById('anel').classList.add('indeterminado');
  api('acordar');
}
function sairDoBoot(){ document.body.classList.add('saindo'); }
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# Servidor                                                                     #
# --------------------------------------------------------------------------- #
def _subir_uvicorn(host: str, porta: int, ssl_kwargs: dict) -> None:
    """Roda o uvicorn NESTA thread (chamado como daemon). O uvicorn moderno já
    pula a instalação de signal handlers fora da main thread, então o único
    cuidado é não deixar exceção morrer calada."""
    import uvicorn

    try:
        uvicorn.run("main:app", host=host, port=porta, log_level="error",
                    reload=False, **ssl_kwargs)
    except Exception as exc:                       # noqa: BLE001
        print(f"[APP] o servidor caiu: {exc}")


def _prontos_locais() -> tuple[dict[str, bool], list[str]]:
    """Marcos + trabalho de fundo pendente, lidos do AppContext EM MEMÓRIA.

    É o que o HTTP não alcança durante o lifespan: a porta só abre quando ele
    termina (ver rede.py), então pela rede o progresso é cego — vai de nada a
    tudo num salto. Aqui `app.state.ctx` já existe antes de qualquer modelo
    carregar, e a barra sobe de verdade desde o começo."""
    prontos = dict.fromkeys((c for c, _, _ in MARCOS), False)
    try:
        import main as servidor

        ctx = getattr(servidor.app.state, "ctx", None)
        if ctx is None:
            return prontos, []
        prontos["servidor"] = True
        prontos["vault"] = bool(getattr(ctx.vectorstore, "ready", False))
        prontos["llm"] = bool(getattr(ctx.llama, "ready", False))
        prontos["stt"] = bool(getattr(ctx.stt, "ready", False))
        # Com o XTTS preguiçoso a voz só sobe quando o microfone abre — esperá-la
        # aqui seguraria a janela para sempre. Preguiça declarada CONTA como pronto.
        cfg = ctx.settings
        preguicoso = getattr(cfg, "tts_carga_preguicosa", False) and getattr(cfg, "tts_engine", "") == "xtts"
        prontos["voz"] = bool(getattr(ctx.tts, "ready", False)) or preguicoso
        pendentes = ctx.tarefas_de_fundo()
        prontos["fundo"] = not pendentes
        return prontos, pendentes
    except Exception:                              # noqa: BLE001 - boot em curso
        return prontos, []


def _prontos_remotos(url: str, token: str) -> tuple[dict[str, bool], list[str]]:
    """Mesma leitura, para um servidor de fora (Docker/LAN) via /api/health."""
    import urllib.request

    prontos = dict.fromkeys((c for c, _, _ in MARCOS), False)
    alvo = url.rstrip("/") + "/api/health"
    if token:
        alvo += "?token=" + token
    try:
        with urllib.request.urlopen(alvo, timeout=2.5) as r:   # nosec B310 - http(s) fixo
            corpo = json.loads(r.read().decode("utf-8"))
    except Exception:                              # noqa: BLE001 - ainda subindo
        return prontos, []
    prontos.update({k: bool(v) for k, v in (corpo.get("servicos") or {}).items()})
    prontos["servidor"] = True
    return prontos, list(corpo.get("tarefas_de_fundo") or [])


RAIO_JANELA = 16          # px — o arredondamento do canto da janela


def arredondar_janela(janela, raio: int = RAIO_JANELA) -> bool:
    """Arredonda os cantos da janela sem moldura. Devolve se conseguiu.

    O Windows 10 NÃO arredonda janela sem moldura — isso só chegou no 11, via
    DWM. Aqui o caminho é o clássico: uma região `RoundRect` aplicada ao HWND com
    `SetWindowRgn`. Custa nada e é o que tira o canto em bico que o dono apontou.

    Limitação assumida: a borda da região não tem antialiasing, então de muito
    perto o canto é escadinha. A alternativa (janela transparente + border-radius
    no CSS) renderiza liso, mas no backend edgechromium a transparência é
    problemática e custaria a composição da janela inteira — caro demais para o
    ganho.

    Fail-soft por completo: qualquer erro aqui deixa a janela quadrada, que é
    exatamente o estado anterior. Um canto feio jamais pode impedir o app de abrir.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        # `Handle` é um System.IntPtr do .NET (pythonnet), e `int()` não o converte:
        # "int() argument must be ... not 'IntPtr'" — medido em 2026-08-02. O
        # `ToInt64()` é o caminho oficial; o `int()` fica de reserva para o dia em
        # que o pywebview trocar de backend e devolver um inteiro puro.
        from ctypes import wintypes

        bruto = janela.native.Handle
        hwnd = bruto.ToInt64() if hasattr(bruto, "ToInt64") else int(bruto)
        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
        # ⚠ O TAMANHO VEM DO WIN32, não de `janela.width/height`.
        # O pywebview reporta pixels LÓGICOS (CSS); o `SetWindowRgn` trabalha em
        # pixels FÍSICOS. Numa tela a 150% de escala, os 430x900 lógicos viram
        # 645x1350 físicos — e a região de 430x900 RECORTAVA a janela, deixando o
        # canto inferior direito preto. Foi o que o dono viu em 2026-08-02: "o app
        # não ocupa o espaço todo dele". `GetWindowRect` já devolve físico, então
        # funciona em qualquer escala sem consultar o DPI.
        rect = wintypes.RECT()
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return False
        largura, altura = rect.right - rect.left, rect.bottom - rect.top
        if largura <= 0 or altura <= 0:
            return False
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        # +1 em cada extremo: a API trata o canto inferior/direito como EXCLUSIVO,
        # e sem isso some uma coluna e uma linha de pixel da janela.
        # O raio é lógico e precisa virar físico, senão o canto fica visivelmente
        # menos curvo numa tela a 150% do que numa a 100%. Perguntamos o DPI DIRETO
        # ao Windows em vez de inferi-lo dividindo tamanhos: a inferência dependia
        # de `janela.width` estar em unidade lógica, o que varia com o backend e
        # deixa de valer se o pywebview mudar. `GetDpiForWindow` responde pelo
        # monitor ONDE A JANELA ESTÁ — então arrastá-la para um segundo monitor com
        # outra escala continua certo. Existe desde o Windows 10 1607; em Windows
        # mais velho a chamada some e caímos em 96 (=100%), que é o comportamento
        # anterior, não um erro.
        dpi = 96
        try:
            user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
            user32.GetDpiForWindow.restype = ctypes.c_uint
            lido = user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
            if lido:
                dpi = lido
        except (AttributeError, OSError):
            pass
        raio_fisico = max(2, int(round(raio * dpi / 96.0)))
        regiao = gdi32.CreateRoundRectRgn(0, 0, largura + 1, altura + 1, raio_fisico, raio_fisico)
        if not regiao:
            return False
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        # O sistema passa a ser DONO da região depois do SetWindowRgn — não se
        # deleta (DeleteObject aqui daria uso-depois-de-liberado).
        return bool(user32.SetWindowRgn(ctypes.c_void_p(hwnd), ctypes.c_void_p(regiao), True))
    except Exception as exc:                       # noqa: BLE001 - cosmético, nunca fatal
        print(f"[APP] não consegui arredondar a janela (segue quadrada): {exc}")
        return False


APP_ID = "MenteDigital.Assistente.Local.1"     # ver `identidade_windows`
TITULO = "Mente Digital"                       # o nome que a barra de tarefas mostra


def identidade_windows(app_id: str = APP_ID) -> bool:
    """Declara ao Windows que este processo é um APLICATIVO próprio.

    Sem isto, a barra de tarefas identifica a janela pelo executável — que aqui é
    o `python.exe` da env conda. Consequências visíveis, todas as três:
    o ícone vira a cobrinha do Python (o pywebview até tenta: sem `icon`, ele
    EXTRAI o ícone de `sys.executable` — winforms.py:247), o balão de passar o
    mouse mostra o agrupamento do Python, e fixar na barra fixa o interpretador,
    não o app. Um AppUserModelID próprio separa os três.

    ⚠ Tem de rodar ANTES de a primeira janela existir: o Windows lê o ID no
    momento em que a janela é registrada na barra, e mudá-lo depois não
    reetiqueta o que já foi criado.

    Fail-soft: fora do Windows, ou se a chamada falhar, devolve False e o app
    segue exatamente como antes — com o ícone do interpretador.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception as exc:                       # noqa: BLE001 - cosmético
        print(f"[APP] não consegui declarar a identidade na barra de tarefas: {exc}")
        return False


def _porta_responde(host: str, porta: int) -> bool:
    import socket

    alvo = "127.0.0.1" if host in ("0.0.0.0", "::") else host   # nosec B104
    try:
        with socket.create_connection((alvo, porta), timeout=0.6):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Orquestração                                                                 #
# --------------------------------------------------------------------------- #
def _acompanhar_boot(janela, ponte: Ponte, ler_marcos, host: str, porta: int,
                     t0: Optional[float] = None) -> None:
    """Roda numa thread do pywebview: acompanha os marcos e alimenta a tela de boot
    até TUDO ficar pronto (ou o dono forçar a entrada).

    `t0` existe por causa do religar do modo economia: a MESMA tela serve para o
    boot frio e para acordar do standby, e no segundo caso cronometrar desde o
    início do processo mostraria "pronto em 8.400s" — o número certo ali é quanto
    durou o RELIGAR, não há quanto tempo o app está aberto."""
    ponte._janela = janela
    inicio = _T0 if t0 is None else t0
    ultimo = None
    while True:
        prontos, pendentes = ler_marcos()
        prontos["porta"] = _porta_responde(host, porta)
        pct, tudo_pronto = calcular_progresso(prontos)
        # Cronometrado desde o TOPO do módulo, não desde aqui: o poller só começa
        # depois dos imports e da criação da janela, e medir a partir dele daria um
        # número menor que a espera real do dono. O que a tela mostra é
        # duplo-clique -> utilizável, que é o único número que ele sente.
        decorridos = int(time.time() - inicio)
        faltam = [rot for chave, rot, _ in MARCOS if not prontos.get(chave)]
        # Quando só o trabalho de FUNDO falta, o detalhe nomeia o que está rodando
        # ("Malha e índice", "Voz para a RAM") em vez de repetir o rótulo do marco:
        # o dono pediu para saber em que etapa está, não só que ainda não acabou.
        if pendentes and faltam == ["Índice e ajustes"]:
            detalhe = f"{decorridos}s · {', '.join(pendentes)}"
        elif faltam:
            detalhe = f"{decorridos}s · faltando: {', '.join(faltam)}"
        else:
            detalhe = f"pronto em {decorridos}s"
        escape = ""
        if faltam and decorridos >= SEGUNDOS_ATE_OFERECER_SAIDA:
            escape = (f"{', '.join(faltam)} não subiu em {decorridos}s. O app funciona "
                      f"sem, com a função correspondente indisponível.")
        evento = {"pct": pct, "estagio": rotulo_estagio(prontos),
                  "detalhe": detalhe, "servicos": prontos, "escape": escape}
        if evento != ultimo:
            try:
                janela.evaluate_js("boot(%s)" % json.dumps(evento))
            except Exception:                      # noqa: BLE001 - janela fechada
                return
            ultimo = evento
        if tudo_pronto or ponte._entrada_forcada:
            return
        time.sleep(0.35)


def _entrar_no_app(janela, url: str) -> None:
    """Troca a tela de boot pela interface de verdade."""
    try:
        janela.evaluate_js("sairDoBoot()")
        time.sleep(0.33)                            # deixa o fade terminar
        janela.load_url(url)
    except Exception as exc:                       # noqa: BLE001
        print(f"[APP] não consegui abrir a interface: {exc}")


def _api(base: str, rota: str, acao: str, token: str) -> dict:
    """POST curto nas rotas de controle (/api/energia, /api/idle). Fail-soft:
    erro devolve {} e quem chama segue — perder um tique de automação é aceitável;
    derrubar o laço que a mantém viva não é."""
    import urllib.request

    alvo = base.rstrip("/") + rota
    req = urllib.request.Request(
        alvo, method="POST", data=json.dumps({"acao": acao}).encode(),
        headers={"Content-Type": "application/json", "X-Mente-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:   # nosec B310 - http(s) fixo
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:                       # noqa: BLE001
        print(f"[APP] {rota} ({acao}) falhou: {exc}")
        return {}


def _acordar_se_dormindo(base: str, token: str) -> bool:
    """Abrir a janela num servidor de plantão RELIGA os modelos. Devolve se pediu.

    Sem isto, o modo economia criava um beco: com o `--standby` instalado no
    logon, abrir o app pelo atalho de sempre encontrava a porta ocupada, entrava
    no caminho "janela num servidor que já existe" e a tela de boot ficava
    esperando LLM e Whisper que ninguém tinha mandado carregar — até os 150 s do
    botão de escape. Ninguém abre o assistente para NÃO usá-lo; abrir é o pedido.

    O POST vai para outra thread porque ele só volta com tudo carregado (~30 s), e
    quem chama aqui é o poller que precisa desenhar a barra nesse meio-tempo."""
    import urllib.request

    alvo = base.rstrip("/") + "/api/health"
    try:
        with urllib.request.urlopen(alvo, timeout=2.5) as r:   # nosec B310 - http(s) fixo
            corpo = json.loads(r.read().decode("utf-8"))
    except Exception:                              # noqa: BLE001 - servidor ainda subindo
        return False
    if not corpo.get("descansando"):
        return False
    print("[APP] o servidor estava de plantão — religando os modelos.", flush=True)
    threading.Thread(target=_api, args=(base, "/api/energia", "ligar", token),
                     daemon=True, name="religar").start()
    return True


def _vigia_de_plantao(porta: int, timeout: float = 1.5) -> bool:
    """Há um vigia atendendo? É a pré-condição para o app poder se encerrar: sem
    ele, sair deixaria o celular sem ninguém para chamar."""
    import urllib.request

    try:
        with urllib.request.urlopen(  # nosec B310 - http fixo em loopback
            f"http://127.0.0.1:{porta}/vigia/status", timeout=timeout
        ) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("vigia"))
    except Exception:                              # noqa: BLE001 - sem vigia é resposta válida
        return False


def _laco_ocioso(base: str, token: str, bandeja, parar: threading.Event,
                 ao_encerrar=None) -> None:
    """Observa a inatividade do DONO e conduz o ciclo de consolidação.

    Roda em thread própria porque a leitura é um polling de API do Windows — não
    há evento a assinar. O intervalo é folgado (3 s): a decisão é de minutos, e
    acordar a CPU cinco vezes por segundo num app que fica aberto o dia inteiro
    seria o oposto do objetivo.

    A máquina de estados fica em `ocioso.py`, pura. Aqui só se lê o relógio, se
    chama o Windows e se executa a ação que ela devolve — a separação é o que
    torna testável um comportamento cujos bugs são todos temporais."""
    from mente_digital import ocioso

    maquina = ocioso.MaquinaOciosa(
        segundos_para_perguntar=float(os.environ.get("MENTE_APP_OCIOSO_SEGUNDOS", 300)),
        segundos_de_resposta=15.0,
    )
    bandeja._maquina = maquina        # o item "Agora não" do menu responde por aqui
    tique = 0
    while not parar.wait(3.0):
        # A cada ~30 s, pergunta ao servidor em que estado ele está. Isto existe
        # porque o servidor agora dorme SOZINHO (watcher de economia): sem a leitura,
        # o ícone da bandeja continuaria aceso sobre uma máquina liberada, e o menu
        # ofereceria "Descansar" para quem já descansa.
        tique += 1
        if tique % 10 == 1:
            estado = _api(base, "/api/energia", "estado", token)
            if estado:
                bandeja.marcar(ligado=(estado.get("estado") != "descansando"))
                # ENCERRAR de vez (o degrau acima do standby): o processo sai e o
                # PC volta a zero, com o VIGIA assumindo o plantão. A decisão é
                # pura e mora em standby.py; aqui só se lê o relógio do servidor e
                # se confirma que há quem atenda o celular depois.
                from mente_digital import standby
                from mente_digital.config import settings as cfg

                if ao_encerrar is not None and standby.deve_encerrar(
                    cfg.idle_encerrar_minutos,
                    float(estado.get("sem_uso_s") or 0.0),
                    bool(estado.get("ocupado")),
                    _vigia_de_plantao(cfg.vigia_port),
                ):
                    print(f"[APP] {cfg.idle_encerrar_minutos} min sem uso e vigia de "
                          f"plantão — encerrando; o PC volta a zero.", flush=True)
                    ao_encerrar()
                    return
        # Com o servidor dormindo, consolidar seria contraditório: o ETL religaria o
        # modelo (e só ele — `restaurar_vram` não é chamado por esse caminho), então
        # o vault seria indexado SEM embeddings e a economia teria durado nada. Quem
        # cresce a base com o PC livre é a pesquisa agendada do scheduler, que tem os
        # próprios guardas.
        if not bandeja.ligado:
            continue
        sem_input = ocioso.segundos_sem_input()
        if sem_input is None:
            # "Não sei" é tratado como PRESENÇA. Uma leitura que falhou jamais
            # pode ligar o idle no meio do trabalho do dono.
            continue
        acao = maquina.tick(sem_input, time.time())
        if acao:
            # Rastro OBRIGATÓRIO. Isto age sozinho, na ausência do dono, e mexe na
            # GPU: um ciclo que não deixa linha nenhuma no log é indepurável quando
            # ele fizer a coisa errada — e o único jeito de saber que fez a certa é
            # reencenar a espera inteira.
            print(f"[OCIOSO] {acao} (sem input ha {sem_input:.0f}s, estado={maquina.estado.value})",
                  flush=True)
        if acao == "perguntar":
            viu = bandeja.avisar(
                "Mente Digital",
                "Você está longe há um tempo. Vou consolidar a memória em 15 s — "
                "abra o menu da bandeja e escolha 'Agora não' para adiar.",
            )
            if not viu:
                # Sem notificação, o dono não teve chance de ver a pergunta —
                # então agir sozinho depois de 15 s seria agir às escondidas.
                maquina.responder(sim=False, agora=time.time())
        elif acao == "rodar":
            bandeja.marcar(consolidando=True)
            _api(base, "/api/idle", "rodar", token)
        elif acao == "parar":
            _api(base, "/api/idle", "parar", token)
            bandeja.marcar(consolidando=False)


def _argumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mente Digital — aplicativo de janela nativa.")
    p.add_argument("--remoto", metavar="URL",
                   help="não sobe servidor: conecta num já existente (ex.: o do Docker).")
    p.add_argument("--sem-janela", action="store_true",
                   help="só o servidor, sem interface (equivale a `python main.py`).")
    p.add_argument("--navegador", action="store_true",
                   help="abre no navegador padrão em vez da janela nativa.")
    p.add_argument("--standby", action="store_true",
                   help="sobe o servidor, SOLTA os modelos e fica de plantão na bandeja "
                        "(janela oculta).")
    p.add_argument("--oculto", action="store_true",
                   help="sobe tudo normalmente, mas com a janela ESCONDIDA (bandeja como "
                        "porta de entrada). É como o vigia levanta o app a pedido do celular.")
    p.add_argument("--vigia", action="store_true",
                   help="NÃO sobe o assistente: fica de plantão mínimo (sem torch) esperando "
                        "um pedido autenticado do celular para levantá-lo. É o modo de "
                        "iniciar junto com o Windows.")
    p.add_argument("--instalar-inicio", action="store_true",
                   help="registra o app na pasta Inicializar do Windows (modo standby).")
    p.add_argument("--remover-inicio", action="store_true",
                   help="desfaz o --instalar-inicio.")
    p.add_argument("--largura", type=int, default=430)
    p.add_argument("--altura", type=int, default=900)
    p.add_argument("--com-moldura", action="store_true",
                   help="usa a moldura do Windows (padrão: sem moldura, barra própria).")
    return p.parse_args()


def _gerir_inicio_automatico(args) -> Optional[int]:
    """Instala/remove o início com o Windows e ENCERRA. Devolve o código de saída,
    ou None quando não era esse o pedido.

    Sai em vez de seguir para a janela de propósito: quem digita `--instalar-inicio`
    quer configurar o sistema, não abrir o assistente."""
    if not (args.instalar_inicio or args.remover_inicio):
        return None
    from mente_digital import inicializacao

    if args.remover_inicio:
        havia = inicializacao.remover()
        print(f"[APP] início automático {'removido' if havia else 'já não estava instalado'}: "
              f"{inicializacao.caminho_atalho()}")
        return 0
    try:
        destino = inicializacao.instalar(BASE_DIR)
    except Exception as exc:                       # noqa: BLE001 - pedido explícito: falha aparece
        print(f"[APP] não consegui instalar o início automático: {exc}")
        return 1
    print(f"[APP] início automático instalado: {destino}")
    print("[APP] no próximo logon o servidor sobe em modo economia (modelos soltos) "
          "e fica na bandeja. Para desfazer: python app.py --remover-inicio")
    return 0


def main() -> int:
    args = _argumentos()
    saida = _gerir_inicio_automatico(args)
    if saida is not None:
        return saida

    if args.vigia:
        # Sai por aqui ANTES de qualquer coisa: o valor do vigia é não ter
        # importado nada pesado. Ele só conhece config, acesso e rede.
        from mente_digital import vigia

        vigia.servir(BASE_DIR)
        return 0

    from mente_digital import rede
    from mente_digital.config import settings

    host, porta = settings.host, settings.port
    esquema = "https" if (settings.ssl_cert and settings.ssl_key) else "http"

    if args.remoto:
        url = args.remoto.rstrip("/")
        ler_marcos = lambda: _prontos_remotos(url, settings.access_token)   # noqa: E731
        alvo_host, alvo_porta = _host_porta_da_url(url)
    else:
        url = f"{esquema}://127.0.0.1:{porta}"
        ler_marcos = _prontos_locais
        alvo_host, alvo_porta = host, porta
        if rede.porta_em_uso(host, porta):
            # Já tem um servidor de pé — não sobe um segundo (seria o zumbi de
            # VRAM que o rede.py documenta). Só abre a janela nele.
            print(f"[APP] porta {porta} já ocupada — abrindo a janela no servidor existente.")
            ler_marcos = lambda: _prontos_remotos(url, settings.access_token)   # noqa: E731
        else:
            ssl_kwargs = {}
            if esquema == "https":
                ssl_kwargs = {"ssl_certfile": settings.ssl_cert, "ssl_keyfile": settings.ssl_key}
            threading.Thread(target=_subir_uvicorn, args=(host, porta, ssl_kwargs),
                             daemon=True, name="uvicorn").start()

    if settings.access_token:
        # O front guarda o token no localStorage na primeira visita com ?token=.
        url_abrir = f"{url}/?token={settings.access_token}"
    else:
        url_abrir = url

    if args.sem_janela:
        print(f"[APP] servidor em {url} — sem janela. Ctrl+C encerra.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    if args.navegador:
        import webbrowser

        while not _porta_responde(alvo_host, alvo_porta):
            time.sleep(0.4)
        webbrowser.open(url_abrir)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    import webview

    from mente_digital import marca

    # Antes de `create_window`: o Windows etiqueta a janela na barra de tarefas
    # no instante em que ela nasce (ver docstring de `identidade_windows`).
    identidade_windows()
    icone = marca.caminho_ico(BASE_DIR)

    geo = ler_geometria((args.largura, args.altura))
    ponte = Ponte()
    voz_preguicosa = settings.tts_carga_preguicosa and settings.tts_engine == "xtts"
    janela = webview.create_window(
        TITULO,
        html=_montar_splash(voz_preguicosa),
        js_api=ponte,
        width=geo["largura"], height=geo["altura"],
        x=geo["x"], y=geo["y"],
        min_size=(360, 560),
        # ESCONDIDA quando ninguém pediu uma janela: no `--standby` (plantão) e no
        # `--oculto` (o vigia levantou o app a pedido do celular, e quem pediu está
        # em outro cômodo). A bandeja é a porta de entrada — e, como a janela existe
        # (só não se vê), trazê-la depois custa zero em vez de um boot novo.
        hidden=args.standby or args.oculto,
        frameless=not args.com_moldura,
        easy_drag=False,          # só a .pywebview-drag-region arrasta — ver docstring
        background_color="#0B0B0D",
        text_select=True,
        # `shadow=False`: com a região arredondada aplicada, a sombra/borda que o DWM
        # desenha em volta da janela sem moldura vaza como uma LINHA CLARA de 1px,
        # mais visível nas quinas — onde a curva da região se afasta do retângulo que
        # o DWM continua desenhando. O dono viu isso em 2026-08-02. Sem sombra, a
        # borda é só o recorte da região.
        shadow=False,
    )
    # ---- bandeja: o X esconde, não encerra --------------------------------
    # Sem isto, fechar a janela custaria os ~36 s de boot para reabrir — e o app
    # foi feito para ficar aberto o dia inteiro. `_sair` é a única saída de
    # verdade, e vive no menu da bandeja.
    from mente_digital.bandeja import Bandeja

    parar_laco = threading.Event()
    encerrando = {"agora": False}

    def _sair() -> None:
        encerrando["agora"] = True
        parar_laco.set()
        bandeja.parar()
        try:
            salvar_geometria(janela)
        except Exception:                          # noqa: BLE001
            pass
        janela.destroy()

    def _abrir() -> None:
        try:
            janela.show()
            janela.restore()
        except Exception as exc:                   # noqa: BLE001
            print(f"[APP] não consegui trazer a janela: {exc}")
        # Abrir a janela em standby É o pedido de acordar. Exigir um segundo clique
        # ("agora clique em Acordar") seria burocracia: ninguém abre o assistente
        # para olhar a tela de repouso dele.
        ponte._acordar.set()

    def _energia() -> None:
        ligando = not bandeja.ligado
        if ligando:
            # Religar pela bandeja durante o `--standby` também tira o poller da
            # espera; sem isto o servidor acordaria e a janela ficaria eternamente
            # na tela de repouso, esperando um clique que já aconteceu.
            ponte._acordar.set()
        estado = _api(url, "/api/energia", "ligar" if ligando else "desligar", settings.access_token)
        bandeja.marcar(ligado=(estado.get("estado") != "descansando"))

    def _consolidar() -> None:
        alvo = "parar" if bandeja.consolidando else "rodar"
        _api(url, "/api/idle", alvo, settings.access_token)
        bandeja.marcar(consolidando=(alvo == "rodar"))

    def _adiar() -> None:
        maquina = getattr(bandeja, "_maquina", None)
        if maquina is not None:
            maquina.responder(sim=False, agora=time.time())

    bandeja = Bandeja(ao_abrir=_abrir, ao_sair=_sair,
                      ao_alternar_energia=_energia, ao_consolidar=_consolidar,
                      ao_adiar=_adiar)

    def _ao_fechar() -> bool:
        """Devolver False CANCELA o fechamento (o evento é cancelável no
        pywebview). Só esconde se houver bandeja — sem ela o usuário ficaria com
        uma janela invisível e nenhum jeito de trazê-la de volta."""
        if encerrando["agora"] or not bandeja.ativa:
            return True
        salvar_geometria(janela)
        janela.hide()
        return False

    janela.events.closing += _ao_fechar

    # Cantos arredondados no `shown`, não antes: o `webview.start` roda o callback
    # numa thread ANTES de a janela nativa existir, e `janela.native` ainda é None
    # ali — medido em 2026-08-02 ("'NoneType' object has no attribute 'Handle'").
    # `shown` é o primeiro instante em que o HWND existe de verdade.
    # Reaplicado no `resized` porque a região do Windows é fixa em pixels: sem isso,
    # aumentar a janela deixaria o conteúdo recortado pela região antiga.
    janela.events.shown += lambda: arredondar_janela(janela)
    if hasattr(janela.events, "resized"):
        janela.events.resized += lambda *a: arredondar_janela(janela)

    def _dormir_e_esperar() -> None:
        """O ciclo do `--standby`: solta os modelos e espera alguém querer o assistente.

        A ordem é dormir DEPOIS do boot completo, e não em vez dele, de propósito.
        Descarregar um serviço que ainda está subindo é corrida com o próprio load —
        `liberar_vram` só solta o que está `ready`, então o que estivesse no meio do
        caminho ficaria carregado e a economia seria parcial e imprevisível. Pagar o
        boot uma vez, no logon, e só então soltar tudo deixa a máquina num estado
        conhecido: modelos fora, servidor de pé."""
        resposta = _api(url, "/api/energia", "desligar", settings.access_token)
        medida = resposta.get("depois") or {}
        vram = medida.get("vram_mb")
        detalhe = f"PC livre · {vram / 1024:.1f} GB de VRAM em uso".replace(".", ",") if vram else "PC livre"
        bandeja.marcar(ligado=False)
        try:
            janela.evaluate_js("dormir(%s)" % json.dumps({
                "detalhe": detalhe,
                "texto": "O assistente está de plantão e a máquina, liberada. "
                         "Ele volta quando você abrir — do PC ou do celular.",
            }))
        except Exception as exc:                   # noqa: BLE001 - janela oculta/fechada
            print(f"[APP] não consegui pintar a tela de repouso: {exc}")
        print("[APP] standby: modelos soltos, servidor de plantão.", flush=True)

        ponte._acordar.wait()
        ponte._acordar.clear()
        # O POST vai para OUTRA thread e o poller volta a rodar JÁ. Chamá-lo aqui
        # congelaria a tela de boot pelos ~30 s do religamento — a barra parada no
        # mesmo número é indistinguível de app travado, e é exatamente o defeito que
        # a tela de progresso existe para não ter.
        threading.Thread(
            target=_api, args=(url, "/api/energia", "ligar", settings.access_token),
            daemon=True, name="religar",
        ).start()
        bandeja.marcar(ligado=True)
        _acompanhar_boot(janela, ponte, ler_marcos, alvo_host, alvo_porta, t0=time.time())

    def _iniciar_fundo() -> None:
        """Depois do boot: sobe a bandeja e o laço de ociosidade. Encadeado ao
        `_acompanhar_boot` para não competir com o carregamento dos modelos."""
        # Fora do `--standby` (que vai DORMIR daqui a pouco), abrir a janela num
        # servidor de plantão significa querer usá-lo. Ver `_acordar_se_dormindo`.
        if not args.standby:
            _acordar_se_dormindo(url, settings.access_token)
        _acompanhar_boot(janela, ponte, ler_marcos, alvo_host, alvo_porta)
        # A bandeja sobe ANTES do standby: no `--standby` ela é a única porta de
        # entrada (a janela nasce oculta), então esperar o fim do ciclo para criá-la
        # deixaria o app inalcançável para sempre.
        if bandeja.iniciar():
            threading.Thread(
                target=_laco_ocioso,
                args=(url, settings.access_token, bandeja, parar_laco, _sair),
                daemon=True, name="ocioso",
            ).start()
        if args.standby:
            _dormir_e_esperar()
        _entrar_no_app(janela, url_abrir)

    webview.start(
        _iniciar_fundo, (),
        # private_mode=False + storage_path: preserva o token (#7) e a permissão
        # de microfone entre execuções. Ver docstring do módulo.
        private_mode=False,
        storage_path=str(BASE_DIR / "dados" / "app_webview"),
        # ⚠ O docstring do pywebview diz que `icon` é "supported only on GTK/QT"
        # (__init__.py:205) — está DESATUALIZADO. O backend WinForms lê
        # `_state['icon']` e monta o `Icon` do formulário (winforms.py:243-244);
        # o mesmo trecho mostra que, sem ele, o fallback é extrair o ícone do
        # `sys.executable`. Passar o caminho é o que troca a cobrinha do Python
        # pela marca. `str()` porque a checagem é `os.path.isfile`.
        icon=str(icone),
        debug=bool(os.environ.get("MENTE_APP_DEBUG")),
    )
    return 0


def _host_porta_da_url(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    u = urlparse(url)
    return (u.hostname or "127.0.0.1"), (u.port or (443 if u.scheme == "https" else 80))


if __name__ == "__main__":
    raise SystemExit(main())
