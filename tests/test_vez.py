"""De quem é a vez da máquina — o núcleo puro do pedido de acesso.

O caso que estes testes existem para impedir de voltar: alguém tenta usar o
assistente pelo celular enquanto o dono joga, e o plantão levanta ~7,7 GB no meio
da raid. E o espelho dele: o dono abre um jogo sem saber que havia alguém no meio
de uma conversa.

Nada aqui toca rede, disco ou relógio — o instante é injetado, como em
`agenda.parse_quando`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

import pytest

from mente_digital import vez
from mente_digital.vez import (
    Pedido,
    agrupar,
    deve_liberar,
    ler_linha,
    ler_todos,
    linha,
    resumo_de_uso,
    texto_ao_mestre,
    texto_ao_pedinte,
)

T0 = datetime(2026, 8, 8, 21, 40, 3)


# --------------------------------------------------------------------------- #
# O bilhete: escrito por um processo, lido por outro                           #
# --------------------------------------------------------------------------- #
def test_ida_e_volta_preserva_o_pedido():
    p = Pedido("ana", vez.agora_iso(T0), "celular da ana")
    assert ler_linha(linha(p)) == p


def test_apelido_com_enter_nao_parte_o_arquivo():
    """`json.dumps` escapa a quebra de linha. Sem isso, um apelido com Enter
    viraria duas linhas no JSONL e a segunda seria lixo — o pedido sumiria pela
    metade, calado."""
    p = Pedido("ana\nfalsa", vez.agora_iso(T0))
    assert "\n" not in linha(p)
    assert ler_linha(linha(p)).usuario == "ana\nfalsa"


@pytest.mark.parametrize("bruto", [
    "", "   ", "{", "[]", "null", '"texto"', "123",
    '{"quando": "2026-08-08T21:40:03"}',        # sem usuário
    '{"usuario": "ana"}',                        # sem quando
    '{"usuario": "", "quando": "x"}',            # usuário vazio
    '{"usuario": 42, "quando": "x"}',            # tipo errado
    '{"usuario": ["ana"], "quando": "x"}',
])
def test_entrada_hostil_vira_none_e_nao_excecao(bruto):
    """⚠ O arquivo mora em pasta que o usuário escreve, e quem o lê põe o conteúdo
    na tela do dono e no TTS. Levantar aqui deixaria o assistente sem subir por
    causa de uma linha torta — a régua do `potencia_cpu`, cujo arquivo também não
    é fronteira de segurança e por isso é tratado como se fosse."""
    assert ler_linha(bruto) is None


def test_linha_torta_no_meio_nao_derruba_as_outras():
    """O vigia pode morrer no meio de um `write`. Um arquivo meio escrito não pode
    impedir o dono de ver os pedidos que chegaram inteiros."""
    texto = "\n".join([linha(Pedido("ana", "2026-08-08T21:40:03")),
                       '{"usuario": "quebr',
                       linha(Pedido("bruno", "2026-08-08T21:42:00"))])
    lidos = ler_todos(texto)
    assert [p.usuario for p in lidos] == ["ana", "bruno"]


def test_o_teto_guarda_os_mais_NOVOS():
    """Um celular em laço encheria o disco. Cortar pelos mais VELHOS seria pior que
    não cortar: o dono veria a madrugada e perderia quem quer entrar agora."""
    texto = "\n".join(linha(Pedido(f"u{i}", f"2026-08-08T21:{i:02d}:00")) for i in range(20))
    assert [p.usuario for p in ler_todos(texto, maximo=3)] == ["u17", "u18", "u19"]


def test_nome_gigante_e_cortado():
    p = ler_linha(linha(Pedido("a" * 500, "2026-08-08T21:40:03")))
    assert len(p.usuario) == vez.USUARIO_MAX


# --------------------------------------------------------------------------- #
# Agrupar: cinco tiques de UM celular são UMA pessoa                           #
# --------------------------------------------------------------------------- #
def test_tiques_da_tela_de_carregamento_nao_viram_cinco_pessoas():
    """O celular bate a cada tique enquanto mostra 'ligando o PC'. Contar solto
    faria o dono ler '5 pessoas tentaram' quando foi a Ana esperando."""
    pedidos = [Pedido("ana", f"2026-08-08T21:4{i}:00") for i in range(5)]
    grupos = agrupar(pedidos)
    assert list(grupos) == ["ana"]
    assert len(grupos["ana"]) == 5


def test_o_recado_ao_mestre_conta_quantas_vezes_e_a_ultima_hora():
    pedidos = [Pedido("ana", "2026-08-08T21:40:00"),
               Pedido("ana", "2026-08-08T21:47:00"),
               Pedido("bruno", "2026-08-08T21:50:00")]
    texto = texto_ao_mestre(pedidos, jogo="escapefromtarkov.exe")
    assert "ana (2x)" in texto and "21:47" in texto
    assert "bruno" in texto and "(2x)" not in texto.split("bruno")[1]
    assert "escapefromtarkov.exe" in texto          # SÓ o dono lê esta frase


def test_sem_pedido_o_recado_e_VAZIO():
    """Um 'ninguém tentou' ao fim de toda partida seria ruído que ensina a ignorar
    o canal — e o canal existe justamente para o caso em que importa."""
    assert texto_ao_mestre([]) == ""


# --------------------------------------------------------------------------- #
# ⚠ A privacidade tem DOIS lados, e este é o que costuma ser esquecido         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("liberado", [True, False])
def test_o_pedinte_nunca_descobre_que_o_dono_esta_jogando(liberado):
    """O mensageiro foi desenhado para o poder não vazar numa direção (o mestre
    administra sem ler a conversa alheia). Vazar a ATIVIDADE do dono na outra é o
    espelho do mesmo defeito. Se ele quiser contar, ele conta — o sistema não."""
    texto = texto_ao_pedinte(liberado).lower()
    for proibido in ("jogo", "jogando", "tarkov", "game", "partida", "raid"):
        assert proibido not in texto


def test_o_pedinte_recusado_sabe_que_o_pedido_nao_se_perdeu():
    """Recusa muda que sabe que foi registrada é indistinguível de falha de rede —
    a mesma lição do 401 que virou 'Nenhuma conversa ainda'."""
    texto = texto_ao_pedinte(False).lower()
    assert "registrado" in texto or "avisado" in texto


# --------------------------------------------------------------------------- #
# A borda: só a TRANSIÇÃO libera                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("agora, antes, pendente, esperado", [
    (None, "tarkov.exe", True,  True),    # o jogo fechou e alguém espera -> libera
    (None, "tarkov.exe", False, False),   # fechou sem ninguém esperando -> NÃO acorda
    ("tarkov.exe", "tarkov.exe", True, False),   # ainda jogando
    (None, None, True, False),            # nunca teve jogo: não é transição
    ("tarkov.exe", None, True, False),    # acabou de ABRIR o jogo
])
def test_so_a_transicao_de_saida_do_jogo_libera(agora, antes, pendente, esperado):
    """Agir no ESTADO faria cada tique do plantão tentar subir um app já de pé —
    o mesmo raciocínio de `jogo_ativo.decidir`. E `tem_pendente` é obrigatório:
    levantar 7,7 GB sem ninguém esperando gasta a máquina exatamente no instante
    em que o dono acabou de sair de um jogo."""
    assert deve_liberar(agora, antes, pendente) is esperado


# --------------------------------------------------------------------------- #
# O espelho: o dono precisa VER quem está usando antes de abrir o jogo         #
# --------------------------------------------------------------------------- #
def test_ninguem_usando_nao_poe_rotulo_nenhum():
    """Um 'ninguém usando' fixo no ícone seria ruído permanente para informar o
    caso normal."""
    assert resumo_de_uso([]) == ""
    assert resumo_de_uso(["", "   ", None]) == ""


def test_um_usuario_aparece_pelo_nome():
    assert resumo_de_uso(["ana"]) == "ana está usando agora"


def test_a_mesma_pessoa_em_dois_aparelhos_conta_uma_vez():
    """Ela abriu o celular E o navegador. São duas sessões, uma pessoa — 'ana e
    ana estão usando' faria o dono duvidar do rótulo."""
    assert resumo_de_uso(["ana", "ana"]) == "ana está usando agora"


def test_muita_gente_nao_estoura_o_rotulo_da_bandeja():
    """O tooltip da bandeja é curto; listar oito nomes viraria uma linha cortada
    no meio, que é pior que um número."""
    saida = resumo_de_uso(["ana", "bruno", "carla", "davi"])
    assert saida == "ana, bruno e mais 2 estão usando agora"
    assert len(saida) < 64


# --------------------------------------------------------------------------- #
# ⚠ A trava que protege o plantão                                              #
# --------------------------------------------------------------------------- #
def test_import_nao_traz_o_projeto_junto(tmp_path):
    """Quem importa este módulo é o `vigia.py`, o processo que existe para NÃO ter
    torch dentro (61 MB contra os ~7,7 GB do assistente). Um `from
    mente_digital.config import settings` distraído aqui arrastaria o pydantic e,
    por tabela, meio projeto. Mesma régua de `test_vigia.py`; roda em subprocesso
    porque outro teste já importou tudo neste."""
    prova = tmp_path / "prova_leve.py"
    prova.write_text(
        "import sys\n"
        "from mente_digital import vez  # noqa: F401\n"
        "pesados = [m for m in ('torch', 'pydantic', 'fastapi', 'chromadb',\n"
        "                       'llama_cpp', 'transformers', 'numpy')\n"
        "           if m in sys.modules]\n"
        "print('VAZOU:' + ','.join(pesados) if pesados else 'ok')\n",
        encoding="utf-8",
    )
    saida = subprocess.run(
        [sys.executable, str(prova)], capture_output=True, text=True,
        cwd=str(tmp_path.parent), env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert "ok" in saida.stdout, (
        f"importar vez puxou peso para dentro do plantão. "
        f"stdout={saida.stdout!r} stderr={saida.stderr!r}")
