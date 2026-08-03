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
    python scripts/aparelhos.py convidar "celular do dono"
    python scripts/aparelhos.py revogar <id>
    python scripts/aparelhos.py trilha [n]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
        print(f"  {a.id}  {a.apelido}")
        print(f"      criado: {a.criado_em}   expira: {expira}")
        print(f"      último uso: {visto}   de: {onde}   sessões vivas: {reg.sessoes_vivas(a.id)}")
    revogados = [a for a in reg.listar(incluir_revogados=True) if not a.ativo]
    if revogados:
        print(f"\n  ({len(revogados)} revogado(s) — mantidos no histórico, sem acesso)")
    if not settings.aparelhos_habilitado:
        print("\n⚠ MENTE_APARELHOS_HABILITADO está FALSE: o servidor ainda usa o token único.")
    return 0


def convidar(apelido: str) -> int:
    reg = _registro()
    codigo = reg.emitir_codigo(apelido, settings.aparelhos_teto)
    if codigo is None:
        print(f"\n✗ Teto de {settings.aparelhos_teto} aparelhos atingido. Revogue um antes.\n")
        return 1
    print(f"\n  Código para '{apelido}':   {codigo}")
    print(f"  Vale por {settings.aparelhos_codigo_validade_minutos} min e serve UMA vez.")
    print("  Digite-o no aparelho novo (tela de configuração).\n")
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
        if not resto:
            print("Falta o apelido: python scripts/aparelhos.py convidar \"celular do dono\"")
            return 2
        return convidar(" ".join(resto))
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
