"""De quem é a vez da máquina — o pedido de acesso que o plantão recusou.

O PROBLEMA (dono, 2026-08-08): jogando, o PC fica ocupado. Se alguém tenta usar o
assistente pelo celular, o vigia hoje levanta o `app.py` na hora — ~7,7 GB de RAM
e a VRAM inteira, no meio da raid. E o caminho é justamente esse: depois de 20 min
o assistente dorme (`idle_standby_minutos`) e aos 45 ele SAI, então quem joga três
horas quase nunca tem o assistente de pé para um gate dentro dele barrar.

O desenho que o dono pediu tem duas metades, e a segunda é a menos óbvia:

  1. o plantão RECUSA levantar durante o jogo — mas o pedido NÃO SE PERDE: vira
     conversa com o mestre, que responde uma estimativa ("estou acabando aqui,
     já libero"), e quando o jogo fecha o pedinte é avisado de que liberou;
  2. o CONTRÁRIO também precisa aparecer: o dono não pode abrir um jogo e cortar
     sem querer quem já estava no meio de uma conversa. Daí o indicador de quem
     está usando — na BANDEJA, porque jogando a janela não está visível.

⚠ ESTE MÓDULO É STDLIB PURA, SEM UM ÚNICO IMPORT DO PROJETO — a mesma regra de
`jogo_ativo.py`, e pelo mesmo motivo: quem o importa é o `vigia.py`, o processo
que existe para NÃO ter torch dentro. Um `from mente_digital.config import
settings` distraído aqui arrastaria o pydantic (e, por tabela, meio projeto) para
dentro do plantão de 61 MB. Há teste em subprocesso que falha se isso acontecer.

⚠ O PEDINTE NÃO É INFORMADO DE QUE O DONO ESTÁ JOGANDO. O estado é `ocupado`, não
`jogo`, e `texto_ao_pedinte` não menciona a causa. Não é capricho: o mensageiro
inteiro foi desenhado para o poder não vazar numa direção (o mestre administra
sem ler a conversa alheia); vazar a ATIVIDADE do dono na outra é o espelho do
mesmo defeito. Se ele quiser contar, ele conta — `texto_ao_mestre` é que nomeia
o jogo, e ela só é lida por ele.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

#: Nome do arquivo por onde os DOIS PROCESSOS conversam: o vigia escreve, o
#: assistente drena. Uma fonte só de propósito — se cada lado derivasse o caminho
#: por conta, o dia em que um mudasse deixaria o outro lendo um arquivo que nunca
#: recebe nada, e o sintoma seria "o pedido some", não um erro.
ARQUIVO_PEDIDOS = "pedidos_de_acesso.jsonl"


def arquivo_pedidos(raiz: Path) -> Path:
    """Onde o bilhete mora. PURA — recebe a raiz, não a adivinha."""
    return Path(raiz) / "dados" / ARQUIVO_PEDIDOS

#: Tipo de mensagem que o pedido vira no mensageiro. Nome próprio porque
#: `normalizar_tipo` LEVANTA em tipo desconhecido — a gaveta tem de ser declarada,
#: senão o mestre nunca descobre que existe uma que ninguém está triando.
TIPO_ACESSO = "acesso"

#: Teto de pedidos guardados. O bilhete é escrito por um processo que atende rede;
#: sem teto, um celular em laço encheria o disco do dono. Os mais NOVOS ficam: o
#: que interessa é "quem quer usar agora", não a arqueologia da madrugada.
MAX_PEDIDOS = 200

#: Quem pediu quando NÃO DÁ para saber quem pediu — com a identidade por aparelho
#: desligada, o token legado é o mesmo para todos e não deixa rastro por
#: construção. Constante, e não a string solta nos dois lados: `ler_linha` exige
#: `usuario` não-vazio (linha sem dono é lixo), então o anônimo precisa de um NOME,
#: e um nome parece um destinatário. Sem este sentinela, o aviso de "liberou" seria
#: endereçado a um usuário chamado "alguém" que não existe — a mensagem ficaria no
#: banco para sempre, sem entrega e sem erro. Foi um teste que pegou.
ANONIMO = "alguém"

#: Piso de sanidade do nome de usuário no bilhete. O arquivo é lido de volta e o
#: texto vai para a tela do dono — entrada de fora, tamanho conhecido.
USUARIO_MAX = 64


@dataclass(frozen=True)
class Pedido:
    """Alguém tentou usar o assistente e o plantão recusou.

    `quando` é ISO-8601 em texto e não `datetime` de propósito: isto atravessa um
    ARQUIVO entre dois processos, e o formato tem de ser o mesmo dos dois lados
    sem depender de como cada um serializa data."""

    usuario: str
    quando: str
    aparelho: str = ""


def _corta(bruto: object, teto: int) -> str:
    if not isinstance(bruto, str):
        return ""
    return bruto.strip()[:teto]


def agora_iso(agora: Optional[datetime] = None) -> str:
    """O instante é INJETADO, como em `agenda.parse_quando` — sem relógio escondido
    não há teste que reencene a noite de ontem."""
    return (agora or datetime.now()).replace(microsecond=0).isoformat()


def linha(pedido: Pedido) -> str:
    """Um pedido -> uma linha de JSONL. Sem quebra de linha dentro, por construção
    (`json.dumps` escapa `\\n`), senão um apelido com Enter partiria o arquivo em
    duas linhas e a segunda seria lixo que o leitor descarta calado."""
    return json.dumps({"usuario": pedido.usuario, "quando": pedido.quando,
                       "aparelho": pedido.aparelho}, ensure_ascii=False)


def ler_linha(bruto: str) -> Optional[Pedido]:
    """Uma linha -> um pedido, ou None.

    ⚠ ENTRADA HOSTIL. O arquivo mora em pasta que o usuário escreve, e quem o lê é
    o assistente, que vai pôr o conteúdo na tela do dono e no TTS. Então nada aqui
    confia: tipo errado, campo faltando ou JSON quebrado viram None em vez de
    subir exceção — mesma régua do `potencia_cpu`, cujo arquivo também não é
    fronteira de segurança e por isso é tratado como se fosse."""
    try:
        dado = json.loads(bruto)
    except (ValueError, TypeError):
        return None
    if not isinstance(dado, dict):
        return None
    usuario = _corta(dado.get("usuario"), USUARIO_MAX)
    quando = _corta(dado.get("quando"), 32)
    if not usuario or not quando:
        return None
    return Pedido(usuario, quando, _corta(dado.get("aparelho"), USUARIO_MAX))


def ler_todos(texto: str, maximo: int = MAX_PEDIDOS) -> list[Pedido]:
    """Todas as linhas válidas, as MAIS NOVAS primeiro na hora de cortar.

    Linha inválida é PULADA, não fatal: um arquivo meio escrito (o vigia morreu no
    meio de um `write`) não pode impedir o dono de ver os outros pedidos."""
    lidos = [p for p in (ler_linha(ln) for ln in texto.splitlines() if ln.strip()) if p]
    return lidos[-maximo:] if maximo > 0 else lidos


def agrupar(pedidos: Iterable[Pedido]) -> dict[str, list[Pedido]]:
    """Por usuário, preservando a ordem de chegada dentro de cada um.

    Agrupar é o ponto: cinco tiques da tela de carregamento de um celular são UMA
    pessoa querendo entrar, não cinco pedidos. Contá-los soltos faria o dono ler
    "5 pessoas tentaram" quando foi a Ana esperando."""
    saida: dict[str, list[Pedido]] = {}
    for p in pedidos:
        saida.setdefault(p.usuario, []).append(p)
    return saida


def _hora(iso: str) -> str:
    """Só a hora, para o texto falado. 'às 2026-08-08T21:40:03' seria ilegível em
    voz alta, e o dono está lendo isso ao sair de um jogo, não auditando."""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return iso[:16]


def texto_ao_mestre(pedidos: Iterable[Pedido], jogo: str = "") -> str:
    """O que o DONO lê. É a única frase que nomeia o jogo.

    Vazia quando não há pedido: quem chama usa isso para não mandar recado de
    nada. Uma mensagem "ninguém tentou" ao fim de toda partida seria ruído que
    ensina a ignorar o canal — e o canal existe para o caso em que importa."""
    grupos = agrupar(pedidos)
    if not grupos:
        return ""
    partes = []
    for usuario, lista in grupos.items():
        quando = _hora(lista[-1].quando)
        vezes = f" ({len(lista)}x)" if len(lista) > 1 else ""
        partes.append(f"{usuario}{vezes}, a última às {quando}")
    quem = "; ".join(partes)
    prefixo = f"Enquanto o {jogo} estava aberto, " if jogo else "Enquanto o PC estava ocupado, "
    return (f"{prefixo}alguém quis usar o assistente: {quem}. "
            "Responda por aqui se quiser dar uma previsão.")


def texto_ao_pedinte(liberado: bool) -> str:
    """O que a OUTRA PESSOA lê. Nunca menciona jogo, nem o que o dono está fazendo."""
    if liberado:
        return "O assistente está disponível de novo — pode voltar a usar."
    return ("O PC está em uso agora, então não dá para ligar o assistente. "
            "Seu pedido foi registrado e o dono já foi avisado.")


def deve_liberar(jogo_agora: Optional[str], jogo_antes: Optional[str],
                 tem_pendente: bool) -> bool:
    """Chegou a hora de levantar o assistente e avisar quem esperava?

    PURA, e é a BORDA que importa — não o estado. Só a TRANSIÇÃO de "tinha jogo"
    para "não tem" dispara, senão cada tique do plantão tentaria subir um app já
    de pé (o mesmo raciocínio de `jogo_ativo.decidir`).

    `tem_pendente` é obrigatório no gate porque levantar 7,7 GB sem ninguém
    esperando é gastar a máquina do dono no instante em que ele acabou de sair de
    um jogo — exatamente quando ele menos quer o PC ocupado.
    """
    return bool(jogo_antes) and not jogo_agora and tem_pendente


def resumo_de_uso(usuarios: Iterable[str]) -> str:
    """O rótulo da BANDEJA: quem está usando o assistente agora.

    A metade que o dono pediu por último, e a que o desenho original não tinha:
    ele não pode abrir um jogo e cortar sem querer quem estava no meio de uma
    conversa. Jogando, a janela não está visível — a bandeja é a única superfície
    que continua à vista, e ela não depende do toast (que a medição do Assistente
    de Foco sugere que pode nem aparecer em tela cheia).

    Vazio devolve string vazia, e quem chama volta ao rótulo de sempre: um
    "ninguém usando" fixo no ícone seria ruído permanente para informar o caso
    normal."""
    # ⚠ `str(x)` NÃO serve de filtro: `str(None)` é a string "None", que é
    # verdadeira — o rótulo viraria "None está usando agora" na bandeja do dono.
    # A sessão sem usuário resolvido é o caso normal com o multiusuário desligado,
    # então isto não é aresta: é o caminho default.
    nomes = [x.strip() for x in usuarios if isinstance(x, str) and x.strip()]
    if not nomes:
        return ""
    unicos = list(dict.fromkeys(nomes))          # preserva a ordem, tira repetido
    if len(unicos) == 1:
        return f"{unicos[0]} está usando agora"
    if len(unicos) == 2:
        return f"{unicos[0]} e {unicos[1]} estão usando agora"
    return f"{unicos[0]}, {unicos[1]} e mais {len(unicos) - 2} estão usando agora"
