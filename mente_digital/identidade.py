"""De QUEM é este turno — o contrato único do multiusuário.

Nasceu do pedido de 2026-08-03: quatro usuários, vault de conhecimento dividido, e
ninguém enxergando a memória do outro. Este módulo é a peça que todos os outros
importam para saber de quem é o átomo, a conversa, o lembrete.

POR QUE ContextVar E NÃO UM PARÂMETRO EM TODA CHAMADA
-----------------------------------------------------
O projeto JÁ tomou esta decisão uma vez, por escrito, em `state.turno_falado`: a
informação converge para poucos primitivos mas é ACIONADA de 40+ lugares, e enfiar um
argumento por essa árvore inteira é uma mudança espalhada e fácil de esquecer num ramo
novo. Aqui é pior: são ~40 métodos do `Database` mais ~10 pontos de query do RAG. Um
parâmetro esquecido num deles não dá erro — dá VAZAMENTO, que é silencioso.

Cada turno já roda no seu próprio `asyncio.Task` (`ws._start_pipeline`) e `create_task`
COPIA o contexto, então o `set()` feito na entrada da sessão vive e morre naquele turno,
sem uma sessão contaminar a outra. É a mesma garantia que o `turno_falado` usa.

⚠ O DEFAULT É `None`, E ISSO É A METADE QUE IMPORTA
---------------------------------------------------
`turno_falado` tem default `True` porque falar demais é um incômodo. Aqui o default
seguro é o oposto: NINGUÉM. Um caminho que esqueça de marcar o dono não pode herdar o
dono anterior nem "o primeiro que aparecer" — ele tem de FALHAR, alto, em vez de
devolver a memória de outra pessoa.

Daí `exigir_dono()`, que levanta `DonoIndefinido`. Quem lê dado pessoal chama ela;
quem lê o ACERVO COMUM não precisa de dono nenhum e chama `dono_atual()`.

Esta é a lição do `acervo_suspeito` aplicada antes do estrago: naquele bug, uma chave
de metadado AUSENTE não casava o `where` do Chroma e a nota sumia da busca. O gêmeo
maligno dessa falha, num sistema com quatro pessoas, é o filtro ausente devolver a
nota de todo mundo. Um default que "funciona" é exatamente como isso aconteceria.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from typing import Iterator, Optional

# O dono das 14.492 notas pessoais que já existem no vault (medido em 2026-08-03) e de
# todas as linhas do SQLite anteriores à migração. O backfill carimba este valor.
DONO_PADRAO = "daniel"

# O USUÁRIO MESTRE (pedido do dono, 2026-08-03). Não é "quem lê tudo" — é quem ADMINISTRA:
# parear e revogar aparelho, criar usuário, ler a trilha de auditoria INTEIRA e receber os
# alertas de segurança. A memória pessoal dele continua privada como a dos outros três, e a
# dos outros continua invisível para ele.
#
# ⚠ Essa separação entre ADMINISTRAR e LER é deliberada, e é a parte que costuma se perder:
# quase todo sistema multiusuário acaba dando ao admin um "ver como" que na prática anula a
# promessa feita aos outros. Aqui o poder do mestre é sobre o ACESSO (quem entra, quem sai),
# não sobre o CONTEÚDO. Se um dia isso mudar, tem de ser uma decisão explícita do dono e um
# campo próprio — nunca um efeito colateral de "o admin precisava depurar".
#
# É o mesmo nome do "mestre" da palavra-mestre (`mestre.py`), mas OUTRA coisa: lá é o gatilho
# falado que aciona os agentes, aqui é papel de administrador. Não confundir os dois.
MESTRE = DONO_PADRAO

# A pasta do acervo COMUM, dentro do vault: átomos de obra e figuras que os quatro leem.
# Medido: 10.721 notas (Cannabis Encyclopedia + 1.868 figuras). Ninguém escreve aqui pelo
# uso normal — só a ingestão de obras, que é ato do dono da máquina.
DONO_ACERVO = "_acervo"

# ⚠ O nome do dono vira NOME DE PASTA (`Pessoal/<dono>/`) e NOME DE COLEÇÃO no Chroma.
# Sem esta trava, um apelido com "..\" escaparia do vault e um com espaço/acento quebraria
# a coleção — o mesmo tipo de furo que `_dentro_do_vault` já guarda no main.py. Aceita só
# o que é seguro nos dois destinos ao mesmo tempo.
_RE_DONO = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


class DonoIndefinido(RuntimeError):
    """Pediu-se dado pessoal sem saber de quem. Falha ALTA de propósito — ver o ⚠ acima."""


_dono: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "dono_atual", default=None)


def dono_atual() -> Optional[str]:
    """Quem está falando, ou None. Use quando a ausência de dono é ACEITÁVEL —
    leitura do acervo comum, telemetria da máquina, trabalho de fundo sem usuário."""
    return _dono.get()


def exigir_dono() -> str:
    """Quem está falando. Levanta `DonoIndefinido` se ninguém marcou.

    Use em TODO acesso a dado pessoal (chat, agendamento, nota privada). É o que
    transforma "esqueci o filtro" de vazamento silencioso em erro visível."""
    d = _dono.get()
    if not d:
        raise DonoIndefinido(
            "acesso a dado pessoal sem dono definido — o chamador precisa entrar em "
            "identidade.usar_dono(...) antes (ver mente_digital/identidade.py)"
        )
    return d


def definir_dono(dono: Optional[str]) -> contextvars.Token:
    """Marca o dono do contexto atual. Devolve o token para `restaurar`.

    Prefira o gerenciador `usar_dono`; esta forma existe para quem não tem um bloco
    `with` conveniente (o accept do WebSocket, que marca uma vez para a conexão toda)."""
    return _dono.set(normalizar(dono) if dono else None)


def restaurar(token: contextvars.Token) -> None:
    _dono.reset(token)


@contextlib.contextmanager
def usar_dono(dono: Optional[str]) -> Iterator[None]:
    """Roda um bloco como sendo de `dono`. Restaura no fim, inclusive em exceção."""
    token = definir_dono(dono)
    try:
        yield
    finally:
        _dono.reset(token)


def normalizar(bruto: str) -> str:
    """Apelido digitado -> identificador seguro para pasta E coleção.

    Não "conserta" o que não dá: um nome que não caiba na regra vira erro, porque a
    alternativa (adivinhar) é o servidor escolhendo em que pasta a memória de alguém
    vai parar. Mesmo espírito de `aparelhos.normalizar_codigo`, que se recusa a
    adivinhar sósia de caractere."""
    limpo = (bruto or "").strip().lower().replace(" ", "_")
    if not _RE_DONO.match(limpo):
        raise ValueError(
            f"nome de usuário inválido: {bruto!r} — use de 1 a 31 caracteres entre "
            "a-z, 0-9, '_' e '-', começando por letra ou número"
        )
    return limpo


def valido(bruto: str) -> bool:
    """Versão que não levanta — para validar entrada de formulário antes de gravar."""
    try:
        normalizar(bruto)
        return True
    except ValueError:
        return False
