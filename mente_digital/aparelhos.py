"""
Identidade POR APARELHO — as regras puras do acesso remoto (pedido do dono, 2026-08-03).

O gate de hoje (`acesso.py`) é um SEGREDO ÚNICO compartilhado. Ele resolve "a LAN não
entra", mas não resolve NADA do que o dono pediu para sair de casa:

- não distingue aparelho: os quatro celulares são o mesmo cliente aos olhos do servidor;
- não tem teto: quem tem o segredo é o quinto, o sexto, o décimo aparelho;
- não revoga um sem revogar todos (trocar o token derruba os quatro e obriga a
  reconfigurar cada um na mão);
- não deixa rastro: a tabela `auditoria` sabe QUE uma nota foi salva, não POR QUEM.

Este módulo é a camada de identidade, e é PURO como `acesso.py` e `standby.py`: recebe a
`Situacao` (já resolvida pelo chamador) e devolve o `Veredito` com o MOTIVO. Nada aqui
abre socket, toca SQLite ou lê relógio — o instante é INJETADO, como em `agenda.py`.
A parte suja (persistência, rate limit, derrubar WebSocket) mora em `registro_aparelhos.py`.

⚠ NASCE DESLIGADO. Com `habilitado=False` o veredito é, byte a byte, o de hoje — e não
por reimplementação: `avaliar` CHAMA `acesso.cliente_autorizado`. Duas cópias da regra
legada divergiriam no primeiro conserto de uma delas.

FORMATO DA CREDENCIAL: `mdk1.<id>.<segredo>`.
O `id` viaja em claro de propósito — sem ele o servidor teria de comparar o segredo
recebido contra o hash de CADA aparelho para descobrir quem é (N comparações, e o tempo
gasto vaza a posição na lista). Com o id, é uma busca direta e UMA comparação em tempo
constante. O id não é segredo: sozinho não abre nada.

POR QUE HMAC-SHA256 E NÃO scrypt/bcrypt: o segredo aqui NÃO é senha humana — são 256
bits de `secrets.token_urlsafe`. KDF lento existe para tornar caro o dicionário de
senhas fracas; contra 2^256 ele não compra nada e custa ~100 ms em TODA requisição —
num projeto cujo CLAUDE.md inteiro é sobre TTFA, isso é o preço errado. O sal por
aparelho continua: impede que dois aparelhos com o mesmo segredo (impossível na
prática, mas de graça) tenham a mesma impressão, e mata tabela pré-computada.
⚠ O CÓDIGO DE PAREAMENTO é o oposto — entropia baixa porque é digitado à mão — e por
isso ele é de uso único, de vida curta e coberto pelo bloqueio progressivo.
"""
from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Optional

from mente_digital import acesso, identidade

# --- Motivos ------------------------------------------------------------------
# Dois níveis de propósito. O `motivo` é o da AUDITORIA (fino: o dono precisa saber se
# alguém está sondando id inexistente ou se o aparelho revogado ainda tenta entrar). O
# `motivo_publico` é o que vai na resposta HTTP (grosso: "credencial inválida" não conta
# ao sondador se aquele id existe). O dono pediu "recusa distinguível de falha de rede" —
# é o 401 com corpo que faz isso, não a granularidade do motivo.
MOTIVO_OK = "ok"                                # credencial de aparelho, válida
MOTIVO_LOOPBACK = "loopback"                    # a própria máquina do dono
MOTIVO_TOKEN_LEGADO = "token_legado"            # MENTE_ACCESS_TOKEN de hoje
MOTIVO_DESLIGADO_OK = "legado_ok"               # função nova off: regra de hoje liberou
MOTIVO_DESLIGADO_NEGADO = "legado_negado"       # função nova off: regra de hoje negou
MOTIVO_SEM_CREDENCIAL = "sem_credencial"
MOTIVO_DESCONHECIDA = "credencial_desconhecida"  # id que não existe (sondagem)
MOTIVO_SEGREDO_ERRADO = "segredo_errado"        # id existe, segredo não confere
MOTIVO_REVOGADO = "revogado"
MOTIVO_EXPIRADO = "expirado"
MOTIVO_BLOQUEADO = "bloqueado"                  # rate limit progressivo
MOTIVO_TETO = "teto_aparelhos"
MOTIVO_CODIGO_INVALIDO = "codigo_invalido"
MOTIVO_CODIGO_EXPIRADO = "codigo_expirado"
MOTIVO_CODIGO_USADO = "codigo_usado"

# O que o cliente recebe. "revogado" e "expirado" SÃO expostos: quem os recebe já tinha a
# credencial (não é sondagem), e o app precisa distinguir "o dono me tirou" de "meu
# segredo está errado" para mostrar a tela certa em vez de um 401 mudo.
_PUBLICO = {
    MOTIVO_DESCONHECIDA: "credencial_invalida",
    MOTIVO_SEGREDO_ERRADO: "credencial_invalida",
    MOTIVO_DESLIGADO_NEGADO: "credencial_invalida",
}

# ⚠ …mas SÓ para quem provou posse do segredo. A 1ª versão liberava "revogado" e
# "expirado" para qualquer um, com o argumento de que "quem os recebe já tinha a
# credencial". Uma revisão adversária de 2026-08-03 derrubou o argumento: o `id` é
# PÚBLICO (aparece na tela de pareamento e em `/api/aparelhos`), e a credencial é
# `mdk1.<id>.<segredo>` — então bastava montar `mdk1.<id conhecido>.<lixo>` para o
# servidor responder se aquele aparelho está revogado. Sem acertar segredo nenhum.
# O bloqueio progressivo impede VARRER ids, não uma sondagem única contra um id que
# a pessoa já viu.
#
# A granularidade continua inteira na AUDITORIA (o dono precisa saber que alguém
# sondou um aparelho revogado); o que muda é só o que sai na resposta HTTP.
_SO_COM_POSSE = {MOTIVO_REVOGADO, MOTIVO_EXPIRADO}

PREFIXO = "mdk1"
_RE_CREDENCIAL = re.compile(r"^mdk1\.([0-9a-f]{16})\.([A-Za-z0-9_-]{16,128})$")

# Sem 0/O e 1/I/L: o código é DITADO ou digitado do PC para o celular, e "0 ou O?" vira
# uma falha de pareamento que o dono lê como bug. 31 símbolos ** 10 = ~2^49.
_ALFABETO_CODIGO = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
TAMANHO_CODIGO = 10


@dataclass(frozen=True)
class Aparelho:
    """Um celular autorizado. `impressao` é o HMAC do segredo — o segredo em si existe
    UMA vez, na tela do pareamento, e nunca é gravado (nem no SQLite, nem no log)."""

    id: str
    apelido: str
    impressao: str
    sal: str
    criado_em: str                      # ISO
    ultimo_uso: Optional[str] = None
    ultimo_ip: Optional[str] = None
    revogado_em: Optional[str] = None
    expira_em: Optional[str] = None
    # DE QUEM é este aparelho (multiusuário, 2026-08-03). Fixado no PAREAMENTO e nunca
    # mais mexido: o dono gera o código já dizendo para quem ele é, e o aparelho herda
    # isso ao trocar o código pela credencial.
    #
    # ⚠ Por que não é mutável por uma rota: trocar o usuário de um aparelho já pareado
    # seria transferir de mão a memória inteira de alguém — o celular passaria a ler
    # notas e conversas de outra pessoa sem que ela soubesse. Se um aparelho precisar
    # mudar de dono, o caminho é REVOGAR e parear de novo, que deixa rastro na trilha.
    #
    # O default é o dono padrão porque as linhas que já existem no banco (backfill) são
    # dele — mesmo raciocínio da migração do SQLite.
    usuario: str = identidade.DONO_PADRAO

    @property
    def ativo(self) -> bool:
        return self.revogado_em is None


@dataclass(frozen=True)
class Situacao:
    """Tudo que a decisão precisa, já resolvido pelo chamador. Espelha `standby.Situacao`.

    `segredo_confere` chega pronto porque a comparação exige o sal do banco; deixá-la
    aqui obrigaria este módulo a ler SQLite e deixaria de ser puro.
    """

    habilitado: bool
    host: Optional[str]
    credencial: Optional[str]
    token_legado: str                       # settings.access_token
    aceita_token_legado: bool
    agora: datetime
    aparelho: Optional[Aparelho] = None     # None = id não encontrado
    segredo_confere: bool = False
    bloqueado_ate: Optional[datetime] = None


@dataclass(frozen=True)
class Veredito:
    autorizado: bool
    motivo: str
    aparelho_id: Optional[str] = None
    # O segredo conferiu? Só quem prova posse merece o motivo fino na resposta HTTP
    # (ver `_SO_COM_POSSE`). Nasce False: o padrão seguro é não contar nada.
    provou_posse: bool = False
    # DE QUEM é o turno que este veredito autoriza. É o que o gate passou a DEVOLVER —
    # antes ele dizia só "pode entrar", e a identidade morria ali (medido: o
    # `aparelho_id` era escrito em `request.state` e lido por UMA rota, que só o ecoava).
    #
    # ⚠ SÓ é preenchido quando `autorizado` é True. Num veredito de RECUSA ele fica None
    # de propósito: dizer de quem é um aparelho recusado contaria ao sondador que aquele
    # id existe e a quem pertence — exatamente o vazamento que o `provou_posse` fechou
    # para os motivos. Recusa não conta nada sobre quem está do outro lado.
    usuario: Optional[str] = None

    @property
    def motivo_publico(self) -> str:
        if self.motivo in _SO_COM_POSSE and not self.provou_posse:
            return "credencial_invalida"
        return _PUBLICO.get(self.motivo, self.motivo)


# --- Credencial ---------------------------------------------------------------
def gerar_id() -> str:
    """16 hex. Público — viaja em claro dentro da credencial."""
    return secrets.token_hex(8)


def gerar_segredo() -> str:
    """256 bits. `token_urlsafe` porque isto viaja em query string do WebSocket
    (o browser não manda header custom no handshake — ver main.websocket_endpoint)."""
    return secrets.token_urlsafe(32)


def montar_credencial(aparelho_id: str, segredo: str) -> str:
    return f"{PREFIXO}.{aparelho_id}.{segredo}"


def partir_credencial(credencial: Optional[str]) -> Optional[tuple[str, str]]:
    """(id, segredo) se isto TEM CARA de credencial de aparelho; None se não tem.

    None não é recusa — é "não é deste formato", e o chamador então tenta a regra
    legada. É o que mantém o `MENTE_ACCESS_TOKEN` de hoje funcionando lado a lado.
    """
    if not credencial:
        return None
    m = _RE_CREDENCIAL.match(credencial.strip())
    return (m.group(1), m.group(2)) if m else None


def gerar_sal() -> str:
    return secrets.token_hex(16)


def impressao(segredo: str, sal: str) -> str:
    """HMAC-SHA256(sal, segredo). Ver o cabeçalho do módulo para o porquê de não ser KDF lento."""
    return hmac.new(sal.encode("utf-8"), segredo.encode("utf-8"), sha256).hexdigest()


def confere_segredo(segredo: str, sal: str, esperada: str) -> bool:
    """Comparação em TEMPO CONSTANTE. `==` em hexdigest sai no primeiro byte diferente,
    e a diferença de tempo entre 1 e 32 bytes iguais é medível pela rede com amostras
    suficientes — é o ataque que o `compare_digest` do `acesso.py` já barra lá."""
    return hmac.compare_digest(impressao(segredo, sal), esperada)


# --- Código de pareamento -----------------------------------------------------
def gerar_codigo() -> str:
    """O ato explícito do dono. Gerado NA MÁQUINA dele — nunca por pedido remoto."""
    return "".join(secrets.choice(_ALFABETO_CODIGO) for _ in range(TAMANHO_CODIGO))


def normalizar_codigo(bruto: Optional[str]) -> str:
    """Maiúsculas e sem separador — o dono lê o código do PC e digita no celular, então
    "abcd-2345" e "ABCD 2345" têm de ser o mesmo código.

    O que NÃO se faz aqui: consertar sósia. Trocar um "O" digitado por algum símbolo do
    alfabeto seria o servidor ADIVINHANDO parte de um segredo — exatamente o que este
    módulo existe para impedir. As sósias já foram removidas do alfabeto na emissão
    (não há 0/O nem 1/I/L para confundir); o que sobrar fora dele falha como inválido.
    """
    return re.sub(r"[^0-9A-Z]", "", (bruto or "").upper())


def codigo_valido(
    emitido_em: Optional[datetime],
    usado: bool,
    agora: datetime,
    validade_minutos: int,
) -> Optional[str]:
    """None = pode usar; senão o motivo da recusa. Puro."""
    if emitido_em is None:
        return MOTIVO_CODIGO_INVALIDO
    if usado:
        return MOTIVO_CODIGO_USADO
    if agora >= emitido_em + timedelta(minutes=validade_minutos):
        return MOTIVO_CODIGO_EXPIRADO
    return None


# --- Teto de aparelhos --------------------------------------------------------
def pode_inscrever(n_ativos: int, teto: int) -> bool:
    """O dono pediu 4. `teto <= 0` = sem teto (não é o default; existe para quem
    quiser outra régua sem editar código)."""
    return teto <= 0 or n_ativos < teto


# --- Bloqueio progressivo -----------------------------------------------------
def atraso_bloqueio(falhas: int, base_segundos: float, teto_segundos: float) -> float:
    """Segundos de bloqueio após `falhas` tentativas erradas consecutivas do MESMO IP.

    Progressivo e não fixo porque as duas ameaças são diferentes: o dedo errado do dono
    (2-3 falhas, não pode virar castigo) e a força bruta contra o código de pareamento
    (milhares, tem de ficar inviável). Dobrar resolve as duas com um número só —
    a 3ª falha custa segundos, a 20ª custa mais que a validade do código.

    Só a partir de `_FALHAS_LIVRES` para o erro de digitação não bloquear ninguém.
    """
    if falhas <= _FALHAS_LIVRES:
        return 0.0
    expoente = min(falhas - _FALHAS_LIVRES - 1, 20)     # trava o shift antes do overflow
    return min(base_segundos * (2.0 ** expoente), teto_segundos)


_FALHAS_LIVRES = 2


def bloqueado(bloqueado_ate: Optional[datetime], agora: datetime) -> bool:
    return bloqueado_ate is not None and agora < bloqueado_ate


# --- A decisão ----------------------------------------------------------------
def avaliar(s: Situacao) -> Veredito:
    """O gate. Ordem das checagens é regra de segurança, não estilo — ver comentários."""
    # DESLIGADO = o gate de hoje, sem desvio. Chamar `acesso` em vez de reimplementar
    # é o que garante que ligar/desligar a função nova não muda o caminho legado.
    if not s.habilitado:
        ok = acesso.cliente_autorizado(s.host, s.credencial, s.token_legado)
        # Sem identidade por aparelho não há de quem separar: quem passa é o dono da
        # máquina. Devolver `DONO_PADRAO` aqui (em vez de None) é o que faz o caminho
        # legado continuar ENXERGANDO o vault de hoje quando a função nova está off.
        return Veredito(ok, MOTIVO_DESLIGADO_OK if ok else MOTIVO_DESLIGADO_NEGADO,
                        usuario=identidade.DONO_PADRAO if ok else None)

    partes = partir_credencial(s.credencial)

    # O bloqueio vem ANTES de qualquer validação, mas DEPOIS de reconhecer o formato:
    # quem está em castigo não passa nem com o segredo certo (senão a força bruta que
    # acerta no meio do castigo entra), e o custo de conferir nem é pago.
    # ⚠ O loopback é a exceção deliberada: o painel de controle roda na máquina do dono,
    # e um bloqueio que tranca o dono para fora impede justamente a ação de revogar o
    # invasor. Um atacante em 127.0.0.1 já está dentro da máquina — o gate não é a defesa.
    if bloqueado(s.bloqueado_ate, s.agora) and not _e_loopback(s.host):
        return Veredito(False, MOTIVO_BLOQUEADO, partes[0] if partes else None)

    if partes is not None:
        aparelho_id, _segredo = partes
        ap = s.aparelho
        if ap is None:
            return Veredito(False, MOTIVO_DESCONHECIDA, aparelho_id)
        # Revogação e expiração ANTES do segredo: o segredo de um aparelho revogado
        # continua correto para sempre (nada o apaga do celular do ladrão). É o estado
        # do registro que manda, não a posse do segredo.
        # A ORDEM (estado antes de segredo) não muda: um aparelho revogado é negado
        # mesmo com o segredo certo. O que o `provou_posse` decide é apenas se o
        # motivo FINO pode sair na resposta — o celular do dono, que tem o segredo,
        # continua vendo "revogado" e mostrando a tela certa; quem só chutou o id
        # recebe o genérico.
        if ap.revogado_em is not None:
            return Veredito(False, MOTIVO_REVOGADO, aparelho_id, s.segredo_confere)
        if _expirado(ap.expira_em, s.agora):
            return Veredito(False, MOTIVO_EXPIRADO, aparelho_id, s.segredo_confere)
        if not s.segredo_confere:
            return Veredito(False, MOTIVO_SEGREDO_ERRADO, aparelho_id)
        return Veredito(True, MOTIVO_OK, aparelho_id, usuario=ap.usuario)

    # Não é credencial de aparelho. Sobra a regra legada — que o dono pode desligar
    # (`aceita_token_legado=False`) depois de migrar os quatro celulares, e aí o
    # segredo único deixa de abrir a porta sem que ninguém precise editar código.
    if s.aceita_token_legado and acesso.cliente_autorizado(s.host, s.credencial, s.token_legado):
        # Loopback (a máquina do dono) e o segredo único legado mapeiam os dois para o
        # dono padrão. ⚠ E é exatamente por isso que o token legado precisa MORRER depois
        # de parear os quatro celulares: enquanto ele abre a porta, qualquer um que o
        # tenha entra como o DONO — a identidade por aparelho vira decoração. O
        # interruptor é `MENTE_APARELHOS_TOKEN_LEGADO=false`.
        return Veredito(True, MOTIVO_LOOPBACK if not s.credencial else MOTIVO_TOKEN_LEGADO,
                        usuario=identidade.DONO_PADRAO)
    if not s.credencial:
        return Veredito(False, MOTIVO_SEM_CREDENCIAL)
    return Veredito(False, MOTIVO_DESCONHECIDA)


def _e_loopback(host: Optional[str]) -> bool:
    return (host or "") in acesso._LOOPBACK


def _expirado(expira_em: Optional[str], agora: datetime) -> bool:
    """Sem data = não expira. Data ilegível = EXPIRADO (fail-closed): um campo
    corrompido não pode virar credencial eterna."""
    if not expira_em:
        return False
    try:
        return agora >= datetime.fromisoformat(expira_em)
    except ValueError:
        return True
