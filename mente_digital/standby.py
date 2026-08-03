"""
Modo economia automático — quando o servidor pode soltar tudo e liberar o PC.

O PEDIDO (dono, 2026-08-02): "o servidor no pc ficar rodando e poder controlar
ele pelo celular, alternar entre os modos online e desligado, a partir de um
watcher no pc". Isto é o watcher. Ele mora no servidor (e não na casca `app.py`)
por dois motivos concretos: só o servidor sabe se há uma pergunta em voo, e ele
existe também quando não há casca nenhuma — `python main.py`, `--standby`,
container. Uma economia que só funciona com a janela aberta economiza no caso
errado.

DUAS RÉGUAS QUE PARECEM IGUAIS E NÃO SÃO
----------------------------------------
Todo o resto do trabalho de fundo deste projeto (ingestão, OCR, consolidação,
crawler) usa `ctx.sessoes` vazio como sinal de ócio. Aqui isso seria inútil: o
app foi feito para ficar ABERTO o dia inteiro, então a sessão nunca fecha e o
watcher nunca dispararia. A régua daqui é ATIVIDADE — quanto tempo faz desde o
último turno (`AppContext.ultima_interacao`).

O que NUNCA pode acontecer é dormir por cima de trabalho: uma pergunta em voo,
um ETL atomizando, um OCR com a GPU tomada. Descarregar o modelo debaixo deles
não é economia, é corromper o que estava em curso. Daí os dois vetos abaixo
serem tão importantes quanto o relógio.

Puro e testável, como `agenda.py` e `ocioso.py`: não lê relógio, não chama nada,
não decide sozinho — recebe a situação e devolve o veredito com o MOTIVO. O
motivo existe porque este código age sozinho, na ausência do dono: quando ele
perguntar "por que não dormiu?", a resposta tem de estar no log.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Situacao:
    """Tudo o que a decisão precisa saber, e nada mais."""

    minutos: int                  # configuração; 0 ou menos = watcher desligado
    segundos_sem_uso: float       # desde o último turno interativo
    descansando: bool             # já está dormindo?
    interativo_em_voo: bool       # há pergunta sendo respondida agora?
    fundo_ocupado: bool           # ETL/OCR/ingestão/crawler em curso?


@dataclass(frozen=True)
class Veredito:
    dormir: bool
    motivo: str


def avaliar(s: Situacao) -> Veredito:
    """Este tick deve mandar o servidor descansar?

    A ordem das checagens é a ordem de leitura do log: primeiro o que é
    configuração ("está desligado"), depois o que é estado ("já dorme"), depois
    os vetos de trabalho, e só então o relógio. Assim o motivo impresso é sempre
    o mais informativo dos que se aplicam.
    """
    if s.minutos <= 0:
        return Veredito(False, "watcher desligado (idle_standby_minutos=0)")
    if s.descansando:
        return Veredito(False, "já está descansando")
    if s.interativo_em_voo:
        return Veredito(False, "há pergunta em voo")
    if s.fundo_ocupado:
        return Veredito(False, "trabalho de fundo em curso")
    limite = s.minutos * 60
    if s.segundos_sem_uso < limite:
        faltam = int(limite - s.segundos_sem_uso)
        return Veredito(False, f"usado há {int(s.segundos_sem_uso)}s (faltam {faltam}s)")
    return Veredito(True, f"{int(s.segundos_sem_uso / 60)} min sem uso")


def deve_encerrar(minutos: int, sem_uso_s: float, ocupado: bool, ha_vigia: bool) -> bool:
    """O processo inteiro deve SAIR, devolvendo o PC ao estado zero?

    O degrau acima do standby, e a escolha do dono em 2026-08-02: descansar libera
    a VRAM mas o processo Python segue com ~7,7 GB de RAM comprometidos. Depois de
    45 min sem uso ele sai de vez, e quem atende o celular passa a ser o VIGIA
    (vigia.py), que custa ~30 MB.

    ⚠ `ha_vigia` é condição, não enfeite: encerrar sem alguém de plantão deixaria o
    celular sem ninguém para chamar — o assistente sumiria da rede até o dono
    voltar ao PC e clicar no atalho. Sem vigia, o teto é o standby.

    Puro/testável, como o resto deste módulo."""
    if minutos <= 0 or not ha_vigia:
        return False
    if ocupado:
        return False
    return sem_uso_s >= minutos * 60
