"""
Subir junto com o Windows — o VIGIA, não o assistente.

O pedido (dono, 2026-08-02): "o servidor no pc ficar rodando... iniciando junto
com o computador", e depois, escolhendo entre as opções: "os dois, em camadas".
O que se instala aqui é `app.py --vigia`: o processo MÍNIMO (stdlib pura, ~30 MB,
sem torch) que fica de plantão e levanta o assistente quando o celular pede,
autenticado. O PC amanhece em zero de verdade.

⚠ Isto MUDOU em 2026-08-02: antes instalava `--standby`, que sobe o servidor
inteiro e só solta a VRAM — ficavam ~7,7 GB de RAM comprometidos a noite toda. O
dono viu a medida e pediu as duas camadas. `--standby` continua existindo para
quem prefere o assistente sempre a 30 s de distância.

POR QUE UM `.vbs` E NÃO UM ATALHO `.lnk`
----------------------------------------
As alternativas sem dependência nova eram um `.cmd`, que pisca um console preto em
todo logon e ainda deixa a janela do prompt aberta segurando o processo, ou este
`.vbs` de quatro linhas, que roda com o modo de janela `0` (oculto) e sai na hora.
O `.vbs` é também o mais fácil de auditar: o dono abre o arquivo no bloco de notas
e lê exatamente o que vai rodar.

⚠ CORRIGIDO em 2026-08-05: este bloco dizia que `.lnk` "exige COM via pywin32, e
pywin32 NÃO está nesta env". A segunda metade continua verdadeira, a PRIMEIRA não
era — COM se chama por `ctypes` sem pywin32, e é o que `atalho.py` faz desde
então. A escolha do `.vbs` AQUI segue certa por outro motivo, que é o que valia o
tempo todo: nesta pasta o objetivo é RODAR algo no logon, não ser fixado na barra.
Quem precisa de `.lnk` é o atalho fixável, porque só um `.lnk` carrega o
`System.AppUserModel.ID` — ver `atalho.py`.

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

#: O segundo morador da pasta Inicializar (2026-08-04): o wattímetro de plantão.
#: Arquivo PRÓPRIO, e não um argumento a mais no do vigia, porque são dois
#: processos independentes com ciclos de vida diferentes — o vigia morre quando
#: levanta o assistente, o wattímetro atravessa o dia inteiro. Um `.vbs` por
#: processo também deixa o dono desinstalar um sem derrubar o outro, e ler no
#: bloco de notas exatamente o que cada um faz.
NOME_WATTIMETRO = "Mente Digital - Wattimetro.vbs"

_CABECALHO_VIGIA = (
    "' Mente Digital — deixa o VIGIA de plantão junto com o Windows.\n"
    "' Ele não carrega modelo nenhum: só espera o celular pedir (autenticado)\n"
    "' e então levanta o assistente. O PC amanhece em zero.\n"
    "' Gerado por `python app.py --instalar-inicio`. Para desfazer, rode\n"
    "' `python app.py --remover-inicio` ou simplesmente apague este arquivo.\n"
)

_CABECALHO_WATTIMETRO = (
    "' Mente Digital — o WATTÍMETRO de plantão.\n"
    "' Mede o consumo enquanto o assistente NÃO está de pé (jogo, madrugada) e\n"
    "' se cala sozinho quando ele sobe, para a energia não contar dobrado.\n"
    "' Não carrega modelo nenhum e não toca a GPU.\n"
    "' Gerado por `python scripts/registrar_consumo.py --instalar-inicio`.\n"
    "' Para desfazer, rode `--remover-inicio` ou apague este arquivo.\n"
)


def e_windows() -> bool:
    """Se estamos no Windows.

    Existe como FUNÇÃO — e não como um `os.name != "nt"` solto dentro do
    `instalar` — porque o teste precisa simular o Windows, e simular trocando
    `os.name` envenena o processo INTEIRO: o `Path.__new__` do pathlib lê
    `os.name` A CADA CHAMADA para escolher entre `WindowsPath` e `PosixPath`.
    Num runner POSIX, portanto, todo `Path(...)` do processo passa a levantar
    `NotImplementedError` enquanto o patch está de pé — inclusive os que o
    próprio pytest constrói para formatar a falha, o que transforma um teste
    vermelho em INTERNALERROR e mata a sessão inteira (visto no CI em
    2026-08-03: 49 testes e a suíte morreu).

    O detalhe cruel é que isso é INVISÍVEL na máquina do dono, onde `os.name`
    já é "nt" e o patch é no-op: verde no Windows, sessão destruída no Linux.
    Este seam é o ponto único que o teste troca, e `os.name` fica intocado.
    """
    return os.name == "nt"


def pasta_inicializar() -> Path:
    """A pasta Inicializar DO USUÁRIO — não a de todos os usuários, que exigiria
    administrador. Fora do Windows devolve um caminho que simplesmente não existe;
    quem chama checa a plataforma antes."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def caminho_atalho(nome: str = NOME_ARQUIVO) -> Path:
    """O `.vbs` de UM dos moradores da pasta Inicializar. O default é o vigia, que
    é quem existia primeiro — todo chamador antigo continua valendo sem mudar."""
    return pasta_inicializar() / nome


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


def script_vbs(python: str, script: str, diretorio: str, argumentos: str = "--vigia",
               cabecalho: str = _CABECALHO_VIGIA) -> str:
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
        cabecalho
        + "'\n"
        "' O 0 do Run é o modo de janela: oculto. O False é 'não espere terminar'.\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.CurrentDirectory = "{diretorio}"\n'
        f'sh.Run "{comando}", 0, False\n'
    )


def instalado(nome: str = NOME_ARQUIVO) -> bool:
    return caminho_atalho(nome).exists()


def instalar(raiz: Path, argumentos: str = "--vigia", nome: str = NOME_ARQUIVO,
             alvo: str = "app.py", cabecalho: str = _CABECALHO_VIGIA) -> Path:
    """Escreve o `.vbs` na pasta Inicializar e devolve o caminho. Levanta em vez de
    falhar calado: isto roda por pedido EXPLÍCITO do dono, e "não deu certo" tem de
    aparecer na hora — descobrir no próximo logon que nada subiu seria pior.

    `nome`/`alvo`/`cabecalho` existem desde 2026-08-04, quando o wattímetro virou o
    segundo morador da pasta. Os defaults são o vigia, então nada que chamava isto
    antes precisou mudar — e é de propósito que o alvo seja um NOME RELATIVO à raiz
    e não um caminho livre: quem instala escolhe entre os scripts do projeto, não
    entre arquivos quaisquer do disco."""
    if not e_windows():
        raise RuntimeError("início automático só está implementado no Windows.")
    destino = caminho_atalho(nome)
    destino.parent.mkdir(parents=True, exist_ok=True)
    conteudo = script_vbs(interpretador(), str(raiz / alvo), str(raiz), argumentos,
                          cabecalho)
    # ⚠ UTF-16, não UTF-8. O Windows Script Host lê `.vbs` como ANSI a menos que
    # haja BOM de UTF-16 — em UTF-8 os acentos dos comentários chegam como lixo
    # (visto em 2026-08-02: "MODO ECONOMIA â€” sobe o assistente"). Aqui isso só
    # sujaria comentário, mas o dia em que uma string acentuada entrar no script
    # o logon quebra em silêncio, e falha de script de inicialização não aparece
    # em lugar nenhum que o dono veja. UTF-16 é o formato que o WSH garante.
    destino.write_bytes(conteudo.encode("utf-16"))
    return destino


def remover(nome: str = NOME_ARQUIVO) -> bool:
    """Apaga o arquivo. Devolve se havia algo para apagar."""
    destino = caminho_atalho(nome)
    if not destino.exists():
        return False
    destino.unlink()
    return True
