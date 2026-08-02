"""
Gera os ícones do app Android a partir da MESMA marca do desktop.

    python scripts/gerar_mipmaps.py

Por que não commitar os PNGs: é a razão que `mente_digital/marca.py` já dá para o
`.ico` — o desenho é o código, o bitmap é artefato, e um binário versionado é a
primeira coisa a divergir quando a paleta mudar. Aqui o preço é que o build do
Android exige rodar isto UMA vez depois de clonar (o `res/mipmap-*` é ignorado
pelo git), e é por isso que este script vive em `scripts/` e não no scratchpad.
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from mente_digital import marca  # noqa: E402

RES = RAIZ / "android" / "app" / "src" / "main" / "res"

# Os cinco baldes de densidade do Android para o ícone do launcher (48 dp).
DENSIDADES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def main() -> int:
    if not RES.exists():
        print(f"[MIPMAP] {RES} não existe — o projeto Android está no lugar?")
        return 1
    for nome, px in DENSIDADES.items():
        pasta = RES / f"mipmap-{nome}"
        pasta.mkdir(parents=True, exist_ok=True)
        img = marca.desenhar_marca(px)
        img.save(pasta / "ic_launcher.png")
        # `ic_launcher_round` é o que launchers de ícone circular procuram. É o
        # MESMO desenho: a marca não tem fundo, então o recorte redondo não corta
        # nada visível.
        img.save(pasta / "ic_launcher_round.png")
        print(f"{nome:8s} {px:3d}px -> {pasta.relative_to(RAIZ)}")
    print(f"OK — variante '{marca.variante_configurada()}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
