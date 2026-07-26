"""Monta a amostra ESTRATIFICADA da auditoria a partir dos relatorios de producao.

Estratos, em ordem de risco:
  truncada  — a saida do modelo foi cortada: recall comprovadamente em risco (100%)
  seca      — pagina sem nenhuma figura: onde mora o falso negativo
  rejeitada — pagina com caixa descartada: prova se o filtro comeu figura
  controle  — pagina normal: mede a precisao do enquadramento

Amostragem por passo REGULAR (e nao aleatoria) para ser reproduzivel: rodar de
novo devolve exatamente as mesmas paginas, entao dois auditores comparam o mesmo.

Uso:
    python eval/estratificar_auditoria.py <pasta_dos_relatorios> [seca] [rejeitada] [controle]
    python eval/auditar_figuras.py <pasta>/cervantes_auditoria.json --por-folha 4
"""
import json
import sys
from pathlib import Path

REL = Path(sys.argv[1] if len(sys.argv) > 1 else "eval/relatorios")
# `truncada` tem cota alta de proposito: pagina cortada e o unico caso em que o
# recall esta comprovadamente em risco, entao vai 100% para a auditoria.
COTAS = {"truncada": 99,
         "seca": int(sys.argv[2]) if len(sys.argv) > 2 else 8,
         "rejeitada": int(sys.argv[3]) if len(sys.argv) > 3 else 6,
         "controle": int(sys.argv[4]) if len(sys.argv) > 4 else 4}


def espacado(itens, quantos):
    if len(itens) <= quantos:
        return list(itens)
    passo = len(itens) / quantos
    return [itens[int(i * passo)] for i in range(quantos)]


resumo = {}
for livro in ("cervantes", "amabis"):
    dados = json.loads((REL / f"{livro}.json").read_text(encoding="utf-8"))
    pgs = dados["paginas"]
    estratos = {
        "truncada": [p for p in pgs if p.get("truncou") or p.get("erro")],
        "seca": [p for p in pgs if not p.get("figuras") and not p.get("truncou")],
        "rejeitada": [p for p in pgs if p.get("rejeitadas") and p.get("figuras")],
    }
    ja = {p["pagina"] for e in estratos.values() for p in e}
    estratos["controle"] = [p for p in pgs if p["pagina"] not in ja and p.get("figuras")]

    escolhidas, contagem = [], {}
    for nome, itens in estratos.items():
        pego = espacado(itens, COTAS[nome])
        contagem[nome] = (len(pego), len(itens))
        for p in pego:
            p["_estrato"] = nome
        escolhidas.extend(pego)
    escolhidas.sort(key=lambda p: p["pagina"])
    saida = dict(dados, paginas=escolhidas)
    (REL / f"{livro}_auditoria.json").write_text(
        json.dumps(saida, ensure_ascii=False), encoding="utf-8")
    resumo[livro] = contagem
    print(f"{livro}: {len(escolhidas)} paginas ->",
          " ".join(f"{k}={v[0]}/{v[1]}" for k, v in contagem.items()))
print(json.dumps(resumo))
