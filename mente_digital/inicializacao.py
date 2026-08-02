"""
Subir junto com o Windows — em modo economia, não carregando tudo.

O pedido (dono, 2026-08-02): "o servidor no pc ficar rodando... iniciando junto
com o computador". O que se instala aqui é `app.py --standby`: o servidor sobe,
os modelos são soltos assim que terminam de carregar e a bandeja fica de plantão.
O PC amanhece livre e o assistente acorda quando alguém (o dono na bandeja, ou o
celular pela rede) pedir.

POR QUE UM `.vbs` E NÃO UM ATALHO `.lnk`
----------------------------------------
Criar `.lnk` exige COM (`WScript.Shell` via pywin32), e pywin32 NÃO está nesta
env — medido em 2026-08-02. As alternativas sem dependência nova eram um `.cmd`,
que pisca um console preto em todo logon e ainda deixa a janela do prompt aberta
segurando o processo, ou este `.vbs` de quatro linhas, que roda com o modo de
janela `0` (oculto) e sai na hora. O `.vbs` é também o mais fácil de auditar: o
dono abre o arquivo no bloco de notas e lê exatamente o que vai rodar.

Nada aqui é instalado por conta própria. Escrever na pasta Inicializar é mexer no
sistema do dono, então acontece só por comando explícito (`--instalar-inicio`), e
o caminho do arquivo é impresso — inclusive para ele saber o que apagar à mão se
preferir.

As funções que MONTAM (caminho, interpretador e conteúdo) são puras e testáveis;
só `instalar` e `remover` tocam o disco.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

NOME_ARQUIVO = "Mente Digital.vbs"


def pasta_inicializar() -> Path:
    """A pasta Inicializar DO USUÁRIO — não a de todos os usuários, que exigiria
    administrador. Fora do Windows devolve um caminho que simplesmente não existe;
    quem chama checa a plataforma antes."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def caminho_atalho() -> Path:
    return pasta_inicializar() / NOME_ARQUIVO


def interpretador(executavel: Optional[str] = None) -> str:
    """O `pythonw.exe` ao lado do `python.exe` corrente, se existir.

    Por que o w: o `python.exe` abre uma janela de console que fica aberta o tempo
    todo em que o app viver. O `.vbs` já esconde a janela, mas o console ainda
    existiria — e um Alt+Tab no meio do dia traria um prompt preto do nada. Se o
    `pythonw` não estiver lá (instalação atípica), cai no interpretador normal:
    feio, porém funcional, que é melhor do que não instalar."""
    exe = Path(executavel or sys.executable)
    candidato = exe.with_name("pythonw.exe")
    return str(candidato if candidato.exists() else exe)


def script_vbs(python: str, script: str, diretorio: str, argumentos: str = "--standby") -> str:
    """O conteúdo do `.vbs`. PURO — é o que permite testar as aspas sem escrever
    na pasta de inicialização de ninguém.

    ⚠ VBScript escapa aspas DOBRANDO-AS. Caminhos com espaço ("Program Files",
    "Mente Digital") são a regra, não a exceção, então cada caminho vai entre
    aspas duplicadas. Sem isso o logon falha em silêncio: o Windows não reporta
    erro de script de inicialização em lugar nenhum que o dono veja."""
    def entre_aspas(valor: str) -> str:
        return '""' + valor.replace('"', "") + '""'

    comando = f"{entre_aspas(python)} {entre_aspas(script)} {argumentos}".strip()
    return (
        "' Mente Digital — sobe o assistente em MODO ECONOMIA junto com o Windows.\n"
        "' Gerado por `python app.py --instalar-inicio`. Para desfazer, rode\n"
        "' `python app.py --remover-inicio` ou simplesmente apague este arquivo.\n"
        "'\n"
        "' O 0 do Run é o modo de janela: oculto. O False é 'não espere terminar'.\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.CurrentDirectory = "{diretorio}"\n'
        f'sh.Run "{comando}", 0, False\n'
    )


def instalado() -> bool:
    return caminho_atalho().exists()


def instalar(raiz: Path, argumentos: str = "--standby") -> Path:
    """Escreve o `.vbs` na pasta Inicializar e devolve o caminho. Levanta em vez de
    falhar calado: isto roda por pedido EXPLÍCITO do dono, e "não deu certo" tem de
    aparecer na hora — descobrir no próximo logon que nada subiu seria pior."""
    if os.name != "nt":
        raise RuntimeError("início automático só está implementado no Windows.")
    destino = caminho_atalho()
    destino.parent.mkdir(parents=True, exist_ok=True)
    conteudo = script_vbs(interpretador(), str(raiz / "app.py"), str(raiz), argumentos)
    destino.write_text(conteudo, encoding="utf-8")
    return destino


def remover() -> bool:
    """Apaga o arquivo. Devolve se havia algo para apagar."""
    destino = caminho_atalho()
    if not destino.exists():
        return False
    destino.unlink()
    return True
