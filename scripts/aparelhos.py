"""
O painel de controle dos aparelhos, em linha de comando.

Por que existe ANTES da tela: o pedido do dono é "eu tenho o controle total disso", e
controle que só existe dentro da SPA é controle que some quando a SPA não abre — que é
exatamente a hora em que revogar importa (celular perdido, aparelho estranho no
registro). Este script fala direto com o SQLite: funciona com o servidor no ar, parado
ou dormindo, e não depende de nenhuma rota.

⚠ Rode-o NA MÁQUINA. Emitir código de pareamento é o ato explícito que autoriza um
aparelho novo; se isso fosse uma rota remota, quem quer que a alcançasse poderia se
auto-inscrever — e o teto de 4 viraria decoração.

USO:
    python scripts/aparelhos.py listar
    python scripts/aparelhos.py convidar "celular do dono"              (usuário: daniel)
    python scripts/aparelhos.py convidar "celular da ana" ana           (usuário: ana)
    python scripts/aparelhos.py convidar "cel do felipe" felipe --minutos 120
    python scripts/aparelhos.py revogar <id>
    python scripts/aparelhos.py trilha [n]

O ÚLTIMO argumento do `convidar` é o USUÁRIO quando ele cabe na regra de nome (a-z0-9_-,
sem espaço). É ele que decide de qual memória o aparelho vai ler: cada usuário tem a sua
em `Pessoal/<usuario>/`, e o acervo de obras é comum aos quatro. Omitido = o dono padrão.

⚠ O usuário é fixado no PAREAMENTO e não muda depois. Trocar o dono de um aparelho já
pareado seria entregar a memória inteira de alguém a outra pessoa — para mudar, revogue
e pareie de novo, que deixa rastro na trilha.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mente_digital import aparelhos as regras  # noqa: E402
from mente_digital import identidade  # noqa: E402
from mente_digital.config import settings  # noqa: E402
from mente_digital.registro_aparelhos import RegistroAparelhos  # noqa: E402
from mente_digital.telemetry import db  # noqa: E402


def _registro() -> RegistroAparelhos:
    # `db.init()` ANTES do registro: a tabela `auditoria` é da telemetria, não deste
    # módulo, e o servidor a cria no lifespan (main.py). Rodando este script numa
    # máquina onde o servidor nunca subiu, todo `registrar_auditoria` falhava — a ação
    # acontecia e a TRILHA não, que é o pior dos dois mundos num painel de segurança.
    # (Medido: "no such table: auditoria" em cada convite/revogação num banco novo.)
    db.init()
    r = RegistroAparelhos(settings.db_telemetria)
    r.init()
    return r


def listar() -> int:
    reg = _registro()
    ativos = reg.listar()
    print(f"\n{len(ativos)} de {settings.aparelhos_teto} vaga(s) em uso.\n")
    if not ativos:
        print("  (nenhum aparelho pareado — use 'convidar')")
    for a in ativos:
        visto = a.ultimo_uso or "nunca"
        onde = a.ultimo_ip or "-"
        expira = a.expira_em or "não expira"
        mestre = "  (MESTRE)" if a.usuario == identidade.MESTRE else ""
        print(f"  {a.id}  {a.apelido}   [usuário: {a.usuario}]{mestre}")
        print(f"      criado: {a.criado_em}   expira: {expira}")
        print(f"      último uso: {visto}   de: {onde}   sessões vivas: {reg.sessoes_vivas(a.id)}")
    revogados = [a for a in reg.listar(incluir_revogados=True) if not a.ativo]
    if revogados:
        print(f"\n  ({len(revogados)} revogado(s) — mantidos no histórico, sem acesso)")
    if not settings.aparelhos_habilitado:
        print("\n⚠ MENTE_APARELHOS_HABILITADO está FALSE: o servidor ainda usa o token único.")
    return 0


def convidar(apelido: str, usuario: str = "", minutos: int = 0) -> int:
    reg = _registro()
    # O usuário decide de QUAL memória este aparelho vai ler e escrever. Validado aqui,
    # antes de tocar o banco: o nome vira pasta (`Pessoal/<usuario>/`) e coleção do
    # Chroma, então um apelido inválido tem de morrer na mão de quem digitou.
    try:
        dono = identidade.normalizar(usuario) if usuario else identidade.DONO_PADRAO
    except ValueError as exc:
        print(f"\n✗ {exc}\n")
        return 1
    try:
        codigo = reg.emitir_codigo(apelido, settings.aparelhos_teto, dono, minutos or None)
    except ValueError as exc:                       # validade fora da faixa
        print(f"\n✗ {exc}\n")
        return 1
    if codigo is None:
        print(f"\n✗ Teto de {settings.aparelhos_teto} aparelhos atingido. Revogue um antes.\n")
        return 1
    validade = regras.validade_efetiva(minutos, settings.aparelhos_codigo_validade_minutos)
    print(f"\n  Código para '{apelido}' (usuário: {dono}):   {codigo}")
    print(f"  Vale por {validade} min e serve UMA vez.")
    if minutos:
        print("  (só este código — os outros seguem no padrão de "
              f"{settings.aparelhos_codigo_validade_minutos} min)")
    print("  Digite-o no aparelho novo (tela de configuração).")
    if dono == identidade.MESTRE:
        print(f"  ⚠ '{dono}' é o usuário MESTRE: administra aparelhos e recebe os alertas.")
    print()
    return 0


def revogar(aparelho_id: str) -> int:
    reg = _registro()
    alvo = reg.buscar(aparelho_id)
    if alvo is None:
        print(f"\n✗ Não existe aparelho {aparelho_id}. Veja 'listar'.\n")
        return 1
    if reg.revogar(aparelho_id):
        print(f"\n✓ '{alvo.apelido}' revogado. Sessões abertas foram derrubadas.\n")
        return 0
    print(f"\n  '{alvo.apelido}' já estava revogado.\n")
    return 0


def trilha(n: int = 30) -> int:
    """O que cada aparelho fez — a tabela `auditoria`, filtrada no que é acesso."""
    linhas = [x for x in db.get_auditoria(limit=n * 4)
              if x["acao"].startswith(("acesso_", "aparelho_"))][:n]
    print()
    if not linhas:
        print("  (nada registrado ainda)")
    for x in linhas:
        print(f"  {x['t'][:19]}  {x['acao']:<28} {x['detalhe']}")
    print()
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "ajuda"}:
        print(__doc__)
        return 0
    comando, resto = argv[0], argv[1:]
    if comando == "listar":
        return listar()
    if comando == "convidar":
        # A flag sai ANTES do parsing posicional (o porquê está em `regras.separar_minutos`:
        # "120" cabe na regra de nome de usuário e seria lido como um, calado).
        try:
            resto, minutos = regras.separar_minutos(resto)
        except ValueError as exc:
            print(f"\n✗ {exc}\n")
            return 1
        if not resto:
            print("Falta o apelido: python scripts/aparelhos.py convidar \"celular da ana\" ana")
            return 2
        # O ÚLTIMO argumento é o usuário quando ele já cabe na regra de nome (a-z0-9_-,
        # sem espaço). Assim `convidar "celular da ana" ana` funciona sem flag, e o uso
        # antigo de uma palavra só (`convidar tablet`) continua valendo — nesse caso ela
        # é o APELIDO e o usuário fica no padrão, que é o comportamento de hoje.
        if len(resto) >= 2 and identidade.valido(resto[-1]) and " " not in resto[-1]:
            return convidar(" ".join(resto[:-1]), resto[-1], minutos or 0)
        return convidar(" ".join(resto), "", minutos or 0)
    if comando == "revogar":
        if not resto:
            print("Falta o id: python scripts/aparelhos.py revogar <id>  (veja 'listar')")
            return 2
        return revogar(resto[0])
    if comando == "trilha":
        return trilha(int(resto[0]) if resto and resto[0].isdigit() else 30)
    print(f"Comando desconhecido: {comando}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
