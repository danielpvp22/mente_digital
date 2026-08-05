"""
O atalho FIXÁVEL do Windows — por que "Fixar na barra de tarefas" virava a
cobrinha do Python.

⚠ NOME: `atalho_windows`, e não `atalho`, porque "atalho" JÁ É outra coisa neste
projeto — o atalho de intenção falada do `mestre.py` ("mestre, atalho compras"),
com sua tabela `mestre_atalhos` e seu `tests/test_atalho.py`. São conceitos sem
nenhuma relação, e dois módulos com o mesmo nome fariam todo import futuro ser uma
adivinhação.

O SINTOMA (dono, 2026-08-05, com print): com o app aberto, botão direito no botão
da barra → "Fixar na barra de tarefas", e no INSTANTE do pin o "MD" vira o ícone
genérico do Python — o mesmo do `python.exe` fixado ao lado.

A CAUSA não é o ícone. O ícone da JANELA já está certo: `app.py` passa
`marca.caminho_ico(...)` ao `webview.start(icon=...)`, e o backend WinForms monta
o `Icon` do formulário com ele (winforms.py:243-244). O que falha é o PIN, e o pin
não é a janela: fixar cria um ATALHO. O Windows identifica aplicativo por
AppUserModelID (AUMID); quando manda fixar uma janela cujo processo declarou um
AUMID explícito — e `app.identidade_windows()` declara —, ele procura no Menu
Iniciar um `.lnk` que carregue o MESMO AUMID e fixa AQUELE. Não achando nenhum,
ele não desiste: sintetiza o pin a partir do EXECUTÁVEL do processo, que aqui é o
`python.exe` da env conda. Daí o ícone da cobrinha e o pin que abre o
interpretador em vez do assistente.

Ou seja, o AUMID sozinho é meio conserto: ele SEPARA a janela do agrupamento do
Python (isso já funcionava), mas sem um atalho que o carregue não há o que fixar.
Este módulo escreve esse atalho.

POR QUE COM EM `ctypes` — E POR QUE `inicializacao.py` NÃO FEZ ISSO
-------------------------------------------------------------------
`inicializacao.py:15-22` registra que `.lnk` ficou de fora porque "exige COM via
pywin32, e pywin32 NÃO está nesta env". A primeira metade da frase está certa e a
conclusão não: pywin32 continua ausente (nada aqui o instala), mas COM não precisa
dele — `ctypes` chama vtable de interface COM diretamente, e é o que se faz abaixo.
Medido em 2026-08-05: `IShellLinkW` + `IPropertyStore` + `IPersistFile` criam o
atalho e o AUMID volta na releitura (`ler_aumid`).

O caminho que pywin32 NÃO resolveria de qualquer jeito é justamente o essencial:
`WScript.Shell.CreateShortcut` (o objeto de automação que o `.vbs` usaria) grava
alvo, ícone e argumentos, mas NÃO expõe o property store — não há como escrever
`System.AppUserModel.ID` por ali. Sem property store, o atalho existe e o pin
continua errado. Por isso aqui é `IPropertyStore` na unha.

O `.vbs` da pasta Inicializar continua certo como está: lá o objetivo é RODAR algo
no logon sem piscar console, não ser fixado, e um `.vbs` é auditável no bloco de
notas. São dois problemas diferentes.

⚠ ARMADILHAS PAGAS (as duas custaram depuração em 2026-08-05)
  1. `IPersistFile::Save` exige caminho ABSOLUTO. Com um relativo ele devolve
     E_INVALIDARG — erro que não menciona caminho e manda procurar no lugar errado
     (eu procurei no property store, que estava correto o tempo todo).
  2. `CoTaskMemAlloc` devolve PONTEIRO e o `ctypes` assume `c_int` sem `restype`,
     TRUNCANDO em 64 bits — o `memmove` seguinte escreve em endereço inválido e o
     processo morre com access violation. Mesma classe do que `energia.py:51` já
     documenta para `GetCurrentProcess`.

Nada aqui é instalado por conta própria: escrever no Menu Iniciar é mexer no
sistema do dono, então acontece só por `python app.py --instalar-atalho`, e o
caminho é impresso para ele poder apagar à mão.

As funções que MONTAM (caminho, argumentos, GUID) são puras e testáveis em
qualquer plataforma; só `criar_lnk`/`instalar`/`remover`/`ler_aumid` tocam o
Windows.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Optional

NOME_ATALHO = "Mente Digital.lnk"

# CLSID/IIDs do shell. Texto, e não bytes, porque é assim que a documentação da
# Microsoft os publica — conferir um contra o MSDN é leitura direta.
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
_IID_IPROPERTY_STORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"

#: `System.AppUserModel.ID` = este FMTID mais o índice 5. É a propriedade que
#: liga o atalho ao processo; sem ela o pin cai no executável.
_FMTID_APP_USER_MODEL = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
_PID_APP_USER_MODEL_ID = 5

_VT_LPWSTR = 31
_CLSCTX_INPROC_SERVER = 1

# Índices na vtable. COM não tem nomes em tempo de execução: chamar um método é
# saltar para a N-ésima entrada da tabela, e a ordem é a da DECLARAÇÃO da
# interface (herança primeiro). Errar um índice não dá erro de nome — chama outra
# função com os argumentos errados. Por isso ficam nomeados aqui em vez de soltos.
_QUERY_INTERFACE, _RELEASE = 0, 2
_SL_SET_DESCRIPTION, _SL_SET_WORKING_DIR = 7, 9
_SL_SET_ARGUMENTS, _SL_SET_ICON_LOCATION, _SL_SET_PATH = 11, 17, 20
_PF_LOAD, _PF_SAVE = 5, 6
_PS_GET_VALUE, _PS_SET_VALUE, _PS_COMMIT = 5, 6, 7


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PROPVARIANT(ctypes.Structure):
    """24 bytes em x64 (8 de cabeçalho + união de 16). Só o cabeçalho e o ponteiro
    são tocados aqui — o resto existe para o tamanho bater com o que o COM espera."""
    _fields_ = [("vt", ctypes.c_uint16), ("_r1", ctypes.c_uint16),
                ("_r2", ctypes.c_uint16), ("_r3", ctypes.c_uint16),
                ("data", ctypes.c_void_p), ("_pad", ctypes.c_void_p)]


def e_windows() -> bool:
    """Seam de plataforma. Quem simula Windows troca ISTO, nunca o `os.name`.

    Espelha `inicializacao.e_windows` — ver a docstring de lá para o estrago que
    trocar `os.name` do processo causa (o `pathlib` o relê a cada `Path(...)`, e a
    sessão do pytest morre em INTERNALERROR num runner POSIX).
    """
    return os.name == "nt"


def guid(texto: str) -> _GUID:
    """Monta um `GUID` a partir do texto `{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}`.

    Em Python puro, e não via `CLSIDFromString`, de propósito: assim a montagem é
    TESTÁVEL fora do Windows (é onde o CI roda) e não precisa de COM inicializado
    só para descrever uma constante.
    """
    partes = texto.strip("{}").split("-")
    if len(partes) != 5:
        raise ValueError(f"GUID malformado: {texto!r}")
    resultado = _GUID()
    resultado.Data1 = int(partes[0], 16)
    resultado.Data2 = int(partes[1], 16)
    resultado.Data3 = int(partes[2], 16)
    resultado.Data4 = (ctypes.c_ubyte * 8)(*bytes.fromhex(partes[3] + partes[4]))
    return resultado


def pasta_menu_iniciar() -> Path:
    """A pasta Programas do Menu Iniciar DO USUÁRIO — é onde o Windows procura o
    `.lnk` com o AUMID na hora de fixar. A de todos os usuários exigiria
    administrador e não traz nada em troca.

    Derivada da pasta Inicializar em vez de remontada: `Startup` é literalmente
    filha de `Programs` no shell, então um caminho errado fica errado num lugar só.
    """
    from mente_digital import inicializacao

    return inicializacao.pasta_inicializar().parent


def pasta_area_trabalho() -> Path:
    """A Área de Trabalho, perguntada ao Windows em vez de montada.

    ⚠ Aqui NÃO dá para fazer como o Menu Iniciar (`%APPDATA%` + caminho fixo): a
    Área de Trabalho é REDIRECIONÁVEL, e o OneDrive faz exatamente isso — vira
    `%USERPROFILE%\\OneDrive\\Desktop`. Montar `%USERPROFILE%\\Desktop` na mão
    criaria o atalho numa pasta que o usuário não vê, e o sintoma seria o pior
    possível: comando diz "instalado", área de trabalho continua vazia.
    `SHGetKnownFolderPath` devolve o caminho REAL, redirecionado ou não.

    Nesta máquina os dois coincidem (medido em 2026-08-05) — o que não torna a
    montagem manual correta, só torna o defeito invisível aqui.
    """
    if e_windows():
        try:
            saida = ctypes.c_void_p()
            # FOLDERID_Desktop
            ctypes.oledll.shell32.SHGetKnownFolderPath(
                ctypes.byref(guid("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}")),
                0, None, ctypes.byref(saida))
            try:
                caminho = ctypes.cast(saida, ctypes.c_wchar_p).value
            finally:
                ctypes.windll.ole32.CoTaskMemFree(saida)
            if caminho:
                return Path(caminho)
        except Exception as exc:                   # noqa: BLE001 - cai no palpite abaixo
            print(f"[ATALHO] não consegui perguntar a Área de Trabalho ao Windows: {exc}")
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(base) / "Desktop"


def caminho_atalho(nome: str = NOME_ATALHO) -> Path:
    return pasta_menu_iniciar() / nome


def caminho_atalho_area_trabalho(nome: str = NOME_ATALHO) -> Path:
    return pasta_area_trabalho() / nome


def argumentos(raiz: Path, alvo: str = "app.py") -> str:
    """Os argumentos do atalho: o script, entre aspas.

    As aspas não são zelo: o caminho do projeto pode ter espaço, e sem elas o
    Python receberia "D:\\meus" e "projetos\\app.py" como dois argumentos e
    morreria dizendo que não achou o arquivo. `alvo` é um NOME RELATIVO à raiz, e
    não um caminho livre, pela mesma razão de `inicializacao.instalar`: quem
    instala escolhe entre os scripts do projeto, não entre arquivos do disco.
    """
    return f'"{raiz / alvo}"'


def _metodo(ponteiro, indice: int, restype, *argtypes):
    """Um método COM pelo índice na vtable. `ponteiro` aponta para a interface;
    o primeiro campo dela é o endereço da vtable, e a vtable é um vetor de
    ponteiros de função."""
    vtable = ctypes.cast(ponteiro, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototipo = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return lambda *a: prototipo(vtable[indice])(ponteiro, *a)


def _liberar(ponteiro) -> None:
    if ponteiro:
        _metodo(ponteiro, _RELEASE, ctypes.c_uint32)()


def _consultar(ponteiro, iid: str):
    """QueryInterface. Devolve o novo ponteiro (o chamador libera)."""
    saida = ctypes.c_void_p()
    _metodo(ponteiro, _QUERY_INTERFACE, ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)(
        ctypes.byref(guid(iid)), ctypes.byref(saida))
    return saida


def _novo_shell_link():
    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    ponteiro = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(guid(_CLSID_SHELL_LINK)), None,
                           _CLSCTX_INPROC_SERVER,
                           ctypes.byref(guid(_IID_ISHELL_LINK_W)),
                           ctypes.byref(ponteiro))
    return ponteiro


def _gravar_aumid(link, app_id: str) -> None:
    """Escreve `System.AppUserModel.ID` no property store do atalho. É ESTE passo
    que faz o pin encontrar o app — sem ele o `.lnk` é só um atalho comum."""
    loja = _consultar(link, _IID_IPROPERTY_STORE)
    try:
        chave = _PROPERTYKEY(guid(_FMTID_APP_USER_MODEL), _PID_APP_USER_MODEL_ID)
        valor = _PROPVARIANT()
        # `InitPropVariantFromString` é INLINE no propvarutil.h, não é export do
        # propsys.dll (medido: AttributeError ao procurá-la). Montamos o que ela
        # monta: VT_LPWSTR + cópia na heap do COM, que o PropVariantClear libera.
        ole32 = ctypes.windll.ole32
        # ⚠ restype OBRIGATÓRIO: sem ele o ctypes assume c_int e TRUNCA o ponteiro
        # em 64 bits — o memmove abaixo vira access violation. Ver o topo do módulo.
        ole32.CoTaskMemAlloc.restype = ctypes.c_void_p
        ole32.CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
        bytes_texto = (len(app_id) + 1) * 2          # UTF-16, com o terminador
        buffer = ole32.CoTaskMemAlloc(bytes_texto)
        if not buffer:
            raise MemoryError("CoTaskMemAlloc devolveu nulo")
        ctypes.memmove(buffer, ctypes.c_wchar_p(app_id), bytes_texto)
        valor.vt, valor.data = _VT_LPWSTR, buffer
        try:
            _metodo(loja, _PS_SET_VALUE, ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)(
                ctypes.byref(chave), ctypes.byref(valor))
            _metodo(loja, _PS_COMMIT, ctypes.HRESULT)()
        finally:
            ctypes.oledll.ole32.PropVariantClear(ctypes.byref(valor))
    finally:
        _liberar(loja)


def criar_lnk(destino: Path, alvo: str, args: str, dir_trabalho: str,
              icone: str, app_id: str, descricao: str) -> Path:
    """Escreve o `.lnk` com o AUMID. Levanta em caso de erro — isto roda por pedido
    EXPLÍCITO do dono, e falhar calado deixaria ele fixando o ícone errado de novo
    sem entender por quê."""
    if not e_windows():
        raise RuntimeError("atalho do Menu Iniciar só existe no Windows.")
    destino = destino.resolve()                  # ⚠ Save exige ABSOLUTO — ver o topo
    destino.parent.mkdir(parents=True, exist_ok=True)
    link = _novo_shell_link()
    try:
        texto = lambda i, v: _metodo(link, i, ctypes.HRESULT, ctypes.c_wchar_p)(v)  # noqa: E731
        texto(_SL_SET_PATH, alvo)
        texto(_SL_SET_ARGUMENTS, args)
        texto(_SL_SET_WORKING_DIR, dir_trabalho)
        texto(_SL_SET_DESCRIPTION, descricao)
        _metodo(link, _SL_SET_ICON_LOCATION, ctypes.HRESULT, ctypes.c_wchar_p,
                ctypes.c_int)(icone, 0)
        _gravar_aumid(link, app_id)
        arquivo = _consultar(link, _IID_IPERSIST_FILE)
        try:
            _metodo(arquivo, _PF_SAVE, ctypes.HRESULT, ctypes.c_wchar_p, ctypes.c_int)(
                str(destino), 1)
        finally:
            _liberar(arquivo)
    finally:
        _liberar(link)
    return destino


def ler_aumid(caminho: Path) -> Optional[str]:
    """O AUMID gravado num `.lnk`, ou None se não houver.

    Existe para PROVAR a instalação: "escreveu o arquivo" não é a mesma afirmação
    que "o Windows vai casar o pin", e a segunda é a que importa. `instalar`
    verifica com isto antes de dizer que deu certo.
    """
    if not e_windows():
        return None
    link = _novo_shell_link()
    try:
        arquivo = _consultar(link, _IID_IPERSIST_FILE)
        try:
            _metodo(arquivo, _PF_LOAD, ctypes.HRESULT, ctypes.c_wchar_p, ctypes.c_uint32)(
                str(Path(caminho).resolve()), 0)
        finally:
            _liberar(arquivo)
        loja = _consultar(link, _IID_IPROPERTY_STORE)
        try:
            chave = _PROPERTYKEY(guid(_FMTID_APP_USER_MODEL), _PID_APP_USER_MODEL_ID)
            valor = _PROPVARIANT()
            _metodo(loja, _PS_GET_VALUE, ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)(
                ctypes.byref(chave), ctypes.byref(valor))
            try:
                if valor.vt != _VT_LPWSTR:
                    return None
                return ctypes.cast(valor.data, ctypes.c_wchar_p).value
            finally:
                ctypes.oledll.ole32.PropVariantClear(ctypes.byref(valor))
        finally:
            _liberar(loja)
    finally:
        _liberar(link)


def instalado(nome: str = NOME_ATALHO) -> bool:
    return caminho_atalho(nome).exists()


def instalar(raiz: Path, app_id: str, nome: str = NOME_ATALHO,
             area_trabalho: bool = True) -> list[Path]:
    """Cria o atalho no Menu Iniciar (e, por default, na Área de Trabalho).
    Devolve os caminhos escritos.

    São DOIS lugares porque resolvem coisas diferentes: o do Menu Iniciar é o que
    o Windows procura para casar o pin pelo AUMID e o que a busca do Windows
    indexa ("Mente Digital" na tecla Windows); o da Área de Trabalho é só a porta
    de entrada que o dono pediu. O mesmo `.lnk` serve aos dois.

    `app_id` é OBRIGATÓRIO e vem de quem declara a identidade do processo
    (`app.APP_ID`). Não tem default de propósito: um default aqui seria uma segunda
    cópia da constante, e no dia em que uma das duas mudasse o pin voltaria a cair
    no `python.exe` — sem erro nenhum, só o sintoma de volta.

    O ícone é o MESMO artefato que a janela e o `/favicon.ico` usam
    (`marca.caminho_ico`, gerado sob demanda a partir de `marca.py`); chamá-lo aqui
    também GARANTE que o arquivo existe no instante em que o atalho passa a
    apontar para ele.
    """
    if not e_windows():
        raise RuntimeError("atalho só existe no Windows.")
    from mente_digital import inicializacao, marca

    comum = dict(
        alvo=inicializacao.interpretador(),       # pythonw.exe: sem console preto
        args=argumentos(raiz),
        dir_trabalho=str(raiz),
        icone=str(marca.caminho_ico(raiz)),
        app_id=app_id,
        descricao="Mente Digital — assistente local",
    )
    destinos = [criar_lnk(destino=caminho_atalho(nome), **comum)]
    # O do Menu Iniciar é o que o pin consulta, então é o único que PRECISA ter o
    # AUMID certo — e é justamente por isso que ele é o verificado.
    gravado = ler_aumid(destinos[0])
    if gravado != app_id:
        raise RuntimeError(
            f"o atalho foi escrito mas o AppUserModelID não confere "
            f"(gravado {gravado!r}, esperado {app_id!r}) — o pin cairia no python.exe.")
    if area_trabalho:
        destinos.append(criar_lnk(destino=caminho_atalho_area_trabalho(nome), **comum))
    return destinos


def remover(nome: str = NOME_ATALHO) -> list[Path]:
    """Apaga os atalhos. Devolve os que existiam de fato."""
    apagados = []
    for destino in (caminho_atalho(nome), caminho_atalho_area_trabalho(nome)):
        if destino.exists():
            destino.unlink()
            apagados.append(destino)
    return apagados
