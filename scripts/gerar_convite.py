"""
Gera o CONVITE: um arquivo HTML autocontido para mandar a quem vai usar o assistente.

    python scripts/gerar_convite.py "celular da ana" ana

Sai um `convite_<usuario>.html` em `dados/convites/`. Mande ele + o APK pelo WhatsApp;
a pessoa abre no celular dela e segue os passos até estar conversando.

⚠ POR QUE UM ARQUIVO, E NÃO UMA PÁGINA SERVIDA PELO ASSISTENTE. A página teria de
ensinar a instalar o Tailscale — e só seria alcançável DEPOIS do Tailscale instalado,
porque o assistente só existe dentro da tailnet (e desde o TLS o endereço de LAN não
serve mais: o certificado cobre o nome do Tailscale, então o IP é rejeitado por nome).
O tutorial ficaria atrás da própria porta que ele ensina a abrir. Um arquivo abre
offline, no celular de quem quer que seja, e escapa disso por construção.

⚠ E O QUE NENHUMA PÁGINA RESOLVE: para OUTRA PESSOA alcançar esta máquina, ela precisa
de conta Tailscale própria E o dono precisa compartilhar a máquina com ela no admin
console. É ato do dono, e o convite diz isso na cara em vez de deixar a pessoa
descobrir travada no meio do caminho.

O código de pareamento vale poucos minutos (`MENTE_APARELHOS_CODIGO_VALIDADE_MINUTOS`)
e serve UMA vez — então gere o convite quando for de fato mandar, não antes.
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mente_digital import identidade                                   # noqa: E402
from mente_digital.config import settings                              # noqa: E402
from mente_digital.registro_aparelhos import RegistroAparelhos         # noqa: E402

DESTINO = Path(__file__).resolve().parents[1] / "dados" / "convites"
URL_APK = "https://github.com/danielpvp22/mente_digital/releases/latest"
URL_TAILSCALE = "https://play.google.com/store/apps/details?id=com.tailscale.ipn"


def endereco_do_assistente() -> str:
    """`https://<nome>:<porta>` — o nome sai do CERTIFICADO, não de config solta.

    É o mesmo nome para o qual o cert foi emitido, então bate por construção. Sem
    TLS configurado não há endereço alcançável de fora, e devolver algo aqui seria
    inventar — o chamador avisa e segue sem o endereço."""
    if not settings.ssl_cert:
        return ""
    nome = Path(settings.ssl_cert).stem
    return f"https://{nome}:{settings.port}"


def montar_html(apelido: str, usuario: str, codigo: str, endereco: str,
                validade_min: float) -> str:
    """O convite. PURO — recebe tudo pronto, para o teste conferir o conteúdo sem
    emitir código de verdade nem tocar o banco."""
    e = html.escape
    aviso_endereco = (
        f'<code class="alvo">{e(endereco)}</code>' if endereco else
        '<span class="ruim">(o assistente ainda não tem TLS configurado — '
        'peça o endereço a quem te convidou)</span>'
    )
    return f"""<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Convite — Mente Digital</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#111; --card:#f5f5f7;
           --linha:#ddd; --ok:#0a7; --ruim:#c33; --acento:#3b6ef5; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#15161a; --fg:#e8e8ea; --card:#1e1f25; --linha:#33343c; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:20px 16px 60px; background:var(--bg); color:var(--fg);
         font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:640px; margin:0 auto; }}
  h1 {{ font-size:1.5rem; margin:0 0 4px; }}
  .sub {{ opacity:.7; margin:0 0 28px; }}
  ol {{ padding-left:0; list-style:none; counter-reset:p; }}
  li.passo {{ counter-increment:p; position:relative; padding:16px 16px 16px 52px;
              background:var(--card); border-radius:12px; margin-bottom:14px; }}
  li.passo::before {{ content:counter(p); position:absolute; left:14px; top:16px;
    width:26px; height:26px; border-radius:50%; background:var(--acento); color:#fff;
    font-weight:700; font-size:.85rem; display:grid; place-items:center; }}
  li.passo h2 {{ font-size:1.05rem; margin:0 0 6px; }}
  li.passo p {{ margin:0 0 8px; }}
  a.botao {{ display:inline-block; background:var(--acento); color:#fff;
    text-decoration:none; padding:10px 16px; border-radius:8px; font-weight:600;
    margin:4px 0; }}
  code {{ background:rgba(128,128,128,.18); padding:2px 6px; border-radius:5px;
          font-size:.92em; word-break:break-all; }}
  code.alvo {{ display:block; padding:10px; margin:6px 0; }}
  .codigo {{ font:700 1.35rem/1.4 ui-monospace,Menlo,Consolas,monospace;
    background:var(--bg); border:2px dashed var(--acento); border-radius:10px;
    padding:14px; text-align:center; word-break:break-all; margin:10px 0; }}
  .aviso {{ border-left:4px solid var(--ruim); padding:10px 14px; margin:10px 0;
            background:rgba(204,51,51,.08); border-radius:0 8px 8px 0; }}
  .ruim {{ color:var(--ruim); font-weight:600; }}
  footer {{ margin-top:30px; opacity:.65; font-size:.86rem; }}
</style></head><body><main>

<h1>Bem-vindo ao Mente Digital</h1>
<p class="sub">Convite para <strong>{e(apelido)}</strong> — você vai entrar como
<strong>{e(usuario)}</strong>, com memória própria e privada.</p>

<div class="aviso">
  <strong>Faça na ordem.</strong> O passo 3 não funciona antes do 1 e do 2 — o
  assistente só existe dentro da rede privada, e sem ela o app vai dizer que o
  computador está desligado.
</div>

<ol>
  <li class="passo">
    <h2>Instale o Tailscale e crie sua conta</h2>
    <p>É a rede privada que liga seu celular ao computador do assistente, de
       qualquer lugar. Instale e entre com uma conta (Google serve).</p>
    <a class="botao" href="{e(URL_TAILSCALE)}">Instalar o Tailscale</a>
  </li>

  <li class="passo">
    <h2>Peça o compartilhamento da máquina</h2>
    <p>Avise quem te convidou de que sua conta Tailscale já existe. <strong>Ele
       precisa compartilhar o computador com você</strong> — sem isso, sua conta
       está na rede mas não enxerga a máquina. Depois, aceite o convite que chega
       por e-mail e confirme que o Tailscale está <em>conectado</em>.</p>
  </li>

  <li class="passo">
    <h2>Instale o aplicativo</h2>
    <p>O Android vai avisar que é de "fonte desconhecida" — é esperado, o app não
       está na Play Store. Autorize a instalação.</p>
    <a class="botao" href="{e(URL_APK)}">Baixar o aplicativo</a>
    <p>Se você recebeu o arquivo <code>.apk</code> junto com este convite, pode
       instalar direto por ele.</p>
  </li>

  <li class="passo">
    <h2>Aponte o app para o assistente</h2>
    <p>Na primeira tela, informe o endereço:</p>
    {aviso_endereco}
  </li>

  <li class="passo">
    <h2>Cole o código de pareamento</h2>
    <p>É ele que dá a você uma identidade própria — sua memória não se mistura com
       a de mais ninguém.</p>
    <div class="codigo">{e(codigo)}</div>
    <div class="aviso">
      Vale <strong>{validade_min:.0f} minutos</strong> e serve <strong>uma vez
      só</strong>. Se expirar, peça outro — não é problema, é de propósito.
    </div>
  </li>
</ol>

<div class="aviso">
  <strong>Se o celular for Xiaomi/Redmi (MIUI):</strong> abra os ajustes do
  Tailscale e ligue o <strong>Autostart</strong> / início automático. Sem isso o
  sistema mata o Tailscale em segundo plano — ele continua mostrando
  "Connected" na tela <em>sem estar rodando</em>, e o assistente some do nada.
</div>

<footer>
  Este arquivo funciona sem internet e não envia nada para lugar nenhum. O código
  acima é pessoal: não repasse.
</footer>
</main></body></html>
"""


def main(argv: list) -> int:
    if not argv:
        print(__doc__)
        return 0
    if len(argv) >= 2 and identidade.valido(argv[-1]) and " " not in argv[-1]:
        apelido, bruto = " ".join(argv[:-1]), argv[-1]
    else:
        apelido, bruto = " ".join(argv), ""
    try:
        usuario = identidade.normalizar(bruto) if bruto else identidade.DONO_PADRAO
    except ValueError as exc:
        print(f"\n✗ {exc}\n", file=sys.stderr)
        return 1

    reg = RegistroAparelhos(settings.db_telemetria)
    codigo = reg.emitir_codigo(apelido, settings.aparelhos_teto, usuario)
    if codigo is None:
        print(f"\n✗ Teto de {settings.aparelhos_teto} aparelhos atingido. "
              f"Revogue um antes (scripts/aparelhos.py listar).\n", file=sys.stderr)
        return 1

    endereco = endereco_do_assistente()
    if not endereco:
        print("⚠ Sem MENTE_SSL_CERT no .env — o convite sai sem o endereço.",
              file=sys.stderr)

    DESTINO.mkdir(parents=True, exist_ok=True)
    alvo = DESTINO / f"convite_{usuario}.html"
    alvo.write_text(
        montar_html(apelido, usuario, codigo, endereco,
                    settings.aparelhos_codigo_validade_minutos),
        encoding="utf-8",
    )
    print(f"\n  Convite para '{apelido}' (usuário: {usuario}):")
    print(f"     {alvo}")
    print(f"  Código embutido, válido por "
          f"{settings.aparelhos_codigo_validade_minutos:.0f} min e de uso único.")
    print("  Mande este arquivo + o APK pelo WhatsApp.")
    if usuario == identidade.MESTRE:
        print(f"  ⚠ '{usuario}' é o usuário MESTRE: administra aparelhos e recebe alertas.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
