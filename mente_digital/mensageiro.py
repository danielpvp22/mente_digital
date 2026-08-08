"""O canal usuário -> MESTRE — reportar bug, pedir modificação, falar com o dono.

Pedido de 2026-08-05: o assistente vai abrir para outros usuários, e um usuário que
esbarra num defeito hoje não tem para onde falar. Este módulo é a REGRA desse canal, e
é PURO como `aparelhos.py` e `standby.py`: recebe o que o chamador já resolveu e
devolve um `Veredito` com o MOTIVO. Nada aqui abre socket, toca SQLite ou lê relógio —
o instante é INJETADO, como em `agenda.py`. A parte suja mora em três lugares que já
existem: `telemetry.Database` (persistência), `SchedulerService` (entrega) e `main.py`
(rotas).

⚠ O MESTRE NÃO LÊ A CONVERSA ALHEIA — E ISSO É REGRA, NÃO CORTESIA
------------------------------------------------------------------
`identidade.py` escreve o contrato: o mestre ADMINISTRA (pareia, revoga, audita o
acesso), mas a memória das outras pessoas continua invisível para ele. Um canal de
mensagens é exatamente o lugar onde essa promessa costuma se perder — basta alguém
achar que "o admin precisa ver tudo para dar suporte" e o `pode_ler` ganha um `if
leitor == MESTRE: return True`. A partir daí a promessa feita aos outros quatro é
falsa, e ninguém percebe, porque nada quebra.

Então a régua aqui é a MESMA de qualquer outra caixa de entrada: você lê o que
ESCREVEU e o que foi endereçado A VOCÊ. O mestre recebe tudo porque TODOS escrevem
para ele — não porque ele tem um poder de leitura. É uma diferença que só aparece no
caso A->B, e por isso ele tem teste (`test_o_mestre_nao_le_a_conversa_entre_outros_dois`).

`pode_ler` é a única declaração escrita dessa regra. A SQL do `Database` filtra pelos
mesmos dois lados por PERFORMANCE (não trazer do disco o que vai ser descartado), mas
quem decide é esta função — o `main.py` passa o resultado da consulta por
`visiveis()` antes de responder. Duas guardas para o mesmo furo, de propósito: é a
lição do `acervo_suspeito`, onde um filtro AUSENTE não deu erro nenhum, só devolveu a
nota que não devia.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from mente_digital import identidade

# --- Tipos de mensagem --------------------------------------------------------
# Os três casos que o dono pediu. O tipo é do REMETENTE (ele sabe se está reportando
# um defeito ou pedindo uma mudança) e serve para o mestre triar a caixa sem ler tudo.
TIPO_BUG = "bug"          # "isto está quebrado"
TIPO_PEDIDO = "pedido"    # "queria que fizesse X"
TIPO_LIVRE = "livre"      # qualquer outra coisa — o default
# A quarta gaveta (2026-08-08): "alguém quis usar o PC e o plantão recusou porque
# você estava jogando". Não é `pedido` — aquele é "queria que fizesse X" e espera
# uma decisão de produto; este é um alerta de DISPONIBILIDADE, com validade curta
# e uma resposta esperada ("já libero"). Rótulo próprio é o que deixa o mestre
# triar sem ler: um "a Ana quer entrar agora" perdido no meio de sugestões de
# funcionalidade é o mesmo defeito da colisão de `tipo` que este arquivo já pagou.
# Quem escreve estas é o servidor, ao drenar `dados/pedidos_de_acesso.jsonl` — ver
# `vez.py` e `scheduler._drenar_pedidos_de_acesso`.
TIPO_ACESSO = "acesso"
TIPOS = (TIPO_BUG, TIPO_PEDIDO, TIPO_LIVRE, TIPO_ACESSO)

# Teto do texto. Constante de módulo e não campo de `Settings` porque isto é um piso de
# sanidade contra colar um arquivo inteiro no campo — não uma preferência a calibrar.
# Mesmo espírito do `JANELA_ALERTA_SEGURANCA_SEGUNDOS` do scheduler.
TEXTO_MAX = 4000

# Quanto do texto vai para a FALA. Um relatório de bug de 4.000 caracteres lido em voz
# alta é pior que não avisar: o mestre desliga no meio e perde o resto. A bolha escrita
# leva o texto inteiro; a fala leva o começo e um "..." que diz que há mais.
FALA_MAX = 280

# --- Motivos ------------------------------------------------------------------
MOTIVO_REMETENTE = "remetente"        # eu escrevi
MOTIVO_DESTINATARIO = "destinatario"  # foi endereçada a mim
MOTIVO_ALHEIA = "mensagem_alheia"     # não é minha em nenhuma das duas pontas


@dataclass(frozen=True)
class Mensagem:
    """Uma mensagem já persistida.

    `criada_em`/`lida_em` são ISO (texto), como todo instante gravado neste projeto —
    o SQLite não tem tipo de data e converter na fronteira é o que evita dois formatos
    convivendo na mesma coluna.
    """

    id: int
    remetente: str
    destinatario: str
    texto: str
    tipo: str = TIPO_LIVRE
    criada_em: str = ""
    lida_em: Optional[str] = None

    @property
    def lida(self) -> bool:
        return self.lida_em is not None

    @classmethod
    def de_linha(cls, linha: dict) -> "Mensagem":
        """A linha do `Database` (dict) vira a estrutura. Ponto único de conversão:
        sem ele, cada chamador leria as chaves na mão e um erro de digitação viraria
        `None` silencioso em vez de erro."""
        return cls(
            id=int(linha["id"]),
            remetente=linha["remetente"],
            destinatario=linha["destinatario"],
            texto=linha.get("texto") or "",
            tipo=linha.get("tipo") or TIPO_LIVRE,
            criada_em=linha.get("criada_em") or "",
            lida_em=linha.get("lida_em"),
        )

    def para_json(self) -> dict:
        return {
            "id": self.id,
            "remetente": self.remetente,
            "destinatario": self.destinatario,
            "texto": self.texto,
            "tipo": self.tipo,
            "criada_em": self.criada_em,
            "lida_em": self.lida_em,
            "lida": self.lida,
        }


@dataclass(frozen=True)
class Veredito:
    """`permitido` + o MOTIVO. O motivo existe pelo mesmo que em `standby.py`: isto
    decide privacidade sozinho, e sem rastro a pergunta "por que ele viu isso?" só se
    responde reencenando o pedido."""

    permitido: bool
    motivo: str


# --- Validação da entrada -----------------------------------------------------
def normalizar_tipo(bruto: Optional[str]) -> str:
    """Rótulo digitado -> um dos `TIPOS`. Vazio = `TIPO_LIVRE`.

    Um valor DESCONHECIDO levanta em vez de virar "livre" calado: seguir o
    `identidade.normalizar` aqui é o que impede um cliente novo mandar
    `tipo="urgente"` e o mestre nunca descobrir que existe uma quarta gaveta que
    ninguém está triando."""
    limpo = (bruto or "").strip().lower()
    if not limpo:
        return TIPO_LIVRE
    if limpo not in TIPOS:
        raise ValueError(
            f"tipo de mensagem inválido: {bruto!r} — use um de {', '.join(TIPOS)}")
    return limpo


def limpar_texto(bruto: Optional[str]) -> str:
    """Texto pronto para gravar. Vazio levanta; longo demais é CORTADO em `TEXTO_MAX`.

    Assimetria de propósito: mensagem vazia é erro de quem clicou sem escrever (e
    gravá-la só encheria a caixa do mestre de nada), mas um relatório longo demais
    truncado ainda diz o essencial — recusá-lo faria a pessoa perder o que escreveu
    justamente quando ela tinha MAIS a contar."""
    limpo = (bruto or "").strip()
    if not limpo:
        raise ValueError("mensagem vazia")
    return limpo[:TEXTO_MAX]


def destinatario_padrao() -> str:
    """Para quem vai a mensagem de um usuário: o MESTRE, sempre.

    Não é um `to` que o cliente escolhe, e isso é deliberado — um destinatário livre
    transformaria este canal numa rede social interna entre os cinco, com toda a
    superfície que isso traz (spam entre usuários, bloqueio, denúncia). O dono pediu um
    canal COM ELE. Quando o mestre responde, o caminho é `responder()`, que inverte as
    pontas de uma mensagem que já existe — nunca um destinatário digitado.

    É função e não constante porque `identidade.MESTRE` é lido no momento do envio: um
    teste que troque o mestre (ou uma futura config) não pode ficar preso ao valor que
    valia na hora do import."""
    return identidade.MESTRE


# --- A decisão ----------------------------------------------------------------
def pode_ler(leitor: Optional[str], msg: Mensagem) -> Veredito:
    """A regra do canal. Ver o ⚠ do cabeçalho antes de acrescentar um ramo aqui.

    Sem leitor definido NÃO é "pode tudo" — é recusa. Mesmo default de `identidade`:
    quem esqueceu de marcar quem está falando não pode herdar a caixa de ninguém."""
    if not leitor:
        return Veredito(False, MOTIVO_ALHEIA)
    if leitor == msg.destinatario:
        return Veredito(True, MOTIVO_DESTINATARIO)
    if leitor == msg.remetente:
        return Veredito(True, MOTIVO_REMETENTE)
    return Veredito(False, MOTIVO_ALHEIA)


def pode_marcar_lida(leitor: Optional[str], msg: Mensagem) -> Veredito:
    """Só o DESTINATÁRIO marca como lida — "lida" é um fato sobre quem recebeu.

    Se o remetente pudesse marcar, ele apagaria o não-lido da caixa do mestre sem que o
    mestre tivesse visto nada, e o contador viraria mentira."""
    if leitor and leitor == msg.destinatario:
        return Veredito(True, MOTIVO_DESTINATARIO)
    return Veredito(False, MOTIVO_ALHEIA)


def visiveis(leitor: Optional[str], mensagens: Iterable[Mensagem]) -> list[Mensagem]:
    """O filtro que o `main.py` aplica ao que veio do banco — a segunda guarda descrita
    no cabeçalho. Barato (dezenas de linhas) e é o que faz o `pode_ler` ser o contrato
    de verdade, e não um enfeite testado à parte do caminho real."""
    return [m for m in mensagens if pode_ler(leitor, m).permitido]


def nao_lidas(leitor: Optional[str], mensagens: Iterable[Mensagem]) -> list[Mensagem]:
    """As que chegaram PARA `leitor` e ele ainda não abriu. Não inclui as que ele
    mandou: uma mensagem própria nunca está "não lida" para quem a escreveu."""
    return [m for m in visiveis(leitor, mensagens)
            if not m.lida and m.destinatario == leitor]


# --- A resposta do mestre -----------------------------------------------------
def responder(original: Mensagem, texto: str) -> tuple[str, str, str]:
    """(remetente, destinatario, texto) da RESPOSTA a `original`, com as pontas
    invertidas. Puro: quem grava é o chamador.

    Existe para que responder não seja "montar um POST com o destinatário certo" —
    derivar as pontas da mensagem que se está respondendo é o que impede o mestre
    responder para a pessoa errada com um id trocado."""
    return (original.destinatario, original.remetente, limpar_texto(texto))


# --- A fala -------------------------------------------------------------------
def falar(msg: Mensagem, max_chars: int = FALA_MAX) -> str:
    """O texto da bolha falada do push. Puro para poder ser testado sem TTS.

    Diz QUEM mandou antes do conteúdo: o mestre vai receber de cinco pessoas, e um
    aviso que começa pelo texto obriga a adivinhar de quem é até o fim da frase."""
    corpo = (msg.texto or "").strip()
    if max_chars > 0 and len(corpo) > max_chars:
        corpo = corpo[:max_chars].rstrip() + "..."
    rotulo = {TIPO_BUG: "Relato de problema", TIPO_PEDIDO: "Pedido"}.get(
        msg.tipo, "Mensagem")
    return f"{rotulo} de {msg.remetente}: {corpo}"


def agora_iso(agora: Optional[datetime] = None) -> str:
    """O carimbo, com o relógio INJETÁVEL — é o que deixa o teste afirmar a ordem sem
    dormir. `None` usa o relógio de verdade, para o chamador comum não ter de saber
    disso."""
    return (agora or datetime.now()).isoformat()
