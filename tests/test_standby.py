"""
Modo economia — o watcher que solta os modelos sozinho.

O que estes testes protegem: um comportamento que roda na AUSÊNCIA do dono e mexe
na GPU. Os defeitos naturais dele são todos temporais ("dormiu no meio de uma
resposta", "nunca dormiu", "dormiu por cima do ETL"), e é por isso que a decisão
mora numa função pura — dá para exercitar cada um sem esperar 20 minutos.
"""
from __future__ import annotations

import pytest

from mente_digital import energia, standby
from mente_digital.config import Settings
from mente_digital.state import AppContext


def _situacao(**kwargs) -> standby.Situacao:
    base = dict(minutos=20, segundos_sem_uso=0.0, descansando=False,
                interativo_em_voo=False, fundo_ocupado=False)
    base.update(kwargs)
    return standby.Situacao(**base)


# --------------------------------------------------------------------------- #
# A decisão                                                                    #
# --------------------------------------------------------------------------- #
def test_dorme_depois_do_tempo_configurado():
    veredito = standby.avaliar(_situacao(segundos_sem_uso=20 * 60))
    assert veredito.dormir
    assert "20 min sem uso" in veredito.motivo


def test_nao_dorme_um_segundo_antes():
    veredito = standby.avaliar(_situacao(segundos_sem_uso=20 * 60 - 1))
    assert not veredito.dormir
    assert "faltam 1s" in veredito.motivo


def test_zero_minutos_desliga_o_watcher():
    """O botão de desligar a automação: com 0, nem um dia inteiro parado dorme."""
    veredito = standby.avaliar(_situacao(minutos=0, segundos_sem_uso=86_400))
    assert not veredito.dormir
    assert "desligado" in veredito.motivo


def test_nao_dorme_com_pergunta_em_voo():
    """O veto que importa: descarregar o modelo no meio de um decode não é
    economia, é derrubar a resposta que o dono está esperando."""
    veredito = standby.avaliar(_situacao(segundos_sem_uso=99 * 60, interativo_em_voo=True))
    assert not veredito.dormir
    assert "em voo" in veredito.motivo


def test_nao_dorme_com_trabalho_de_fundo():
    veredito = standby.avaliar(_situacao(segundos_sem_uso=99 * 60, fundo_ocupado=True))
    assert not veredito.dormir
    assert "fundo" in veredito.motivo


def test_nao_dorme_duas_vezes():
    veredito = standby.avaliar(_situacao(segundos_sem_uso=99 * 60, descansando=True))
    assert not veredito.dormir


def test_veto_vence_o_relogio_mesmo_muito_atrasado():
    """Ordem das checagens: nenhum tempo de ócio compra o direito de atropelar
    trabalho em curso."""
    veredito = standby.avaliar(_situacao(
        segundos_sem_uso=10_000 * 60, interativo_em_voo=True, fundo_ocupado=True))
    assert not veredito.dormir


# --------------------------------------------------------------------------- #
# O relógio de uso, no AppContext                                              #
# --------------------------------------------------------------------------- #
def test_contexto_comeca_marcado_como_usado_agora():
    ctx = AppContext(settings=Settings())
    assert ctx.segundos_sem_uso() < 1.0


def test_marcar_uso_rearma_o_relogio():
    ctx = AppContext(settings=Settings())
    ctx.ultima_interacao -= 3600
    assert ctx.segundos_sem_uso() > 3500
    ctx.marcar_uso()
    assert ctx.segundos_sem_uso() < 1.0


def test_segundos_sem_uso_aceita_agora_injetado():
    """Injetável para o teste não depender do relógio — mesmo padrão do
    `agenda.parse_quando` e do `ocioso.MaquinaOciosa`."""
    ctx = AppContext(settings=Settings())
    ctx.ultima_interacao = 1_000.0
    assert ctx.segundos_sem_uso(agora=1_600.0) == 600.0


def test_segundos_sem_uso_nunca_negativo():
    ctx = AppContext(settings=Settings())
    ctx.ultima_interacao = 5_000.0
    assert ctx.segundos_sem_uso(agora=4_000.0) == 0.0


@pytest.mark.asyncio
async def test_aguardar_ocio_volta_na_hora_com_a_gpu_livre():
    ctx = AppContext(settings=Settings())
    assert await ctx.aguardar_ocio(0.05) is True


@pytest.mark.asyncio
async def test_aguardar_ocio_estoura_com_turno_em_voo():
    """É o guarda do botão de MODO ECONOMIA, que agora existe no celular e é
    apertado por quem NÃO vê o que acontece no PC. Soltar os modelos no meio de
    uma resposta não a derruba — deixa-a pior, em silêncio (medido em
    2026-08-02: o embedding sumiu e a busca de figuras caiu para 'só texto')."""
    import asyncio

    ctx = AppContext(settings=Settings())
    ctx.interactive_idle.clear()
    assert await ctx.aguardar_ocio(0.05) is False
    # E volta a passar assim que o turno termina.
    ctx.interactive_idle.set()
    assert await ctx.aguardar_ocio(0.05) is True
    assert isinstance(ctx.interactive_idle, asyncio.Event)


@pytest.mark.asyncio
async def test_aguardar_ocio_espera_o_turno_terminar_em_vez_de_recusar_logo():
    """A espera é o ponto: a maioria dos turnos acaba em segundos, e o dono que
    apertou "economia" quer que o PC durma — só não por cima da resposta."""
    import asyncio

    ctx = AppContext(settings=Settings())
    ctx.interactive_idle.clear()

    async def _terminar_o_turno():
        await asyncio.sleep(0.05)
        ctx.interactive_idle.set()

    asyncio.ensure_future(_terminar_o_turno())
    assert await ctx.aguardar_ocio(2.0) is True


@pytest.mark.asyncio
async def test_turno_interativo_marca_uso_na_saida():
    """O relógio conta do FIM do turno. Marcar só na entrada faria uma síntese
    longa envelhecer enquanto ainda estava sendo gerada."""
    ctx = AppContext(settings=Settings())
    ctx.ultima_interacao -= 3600
    async with ctx.interativo():
        pass
    assert ctx.segundos_sem_uso() < 1.0


# --------------------------------------------------------------------------- #
# O resumo do que foi liberado (vai para o log do watcher)                     #
# --------------------------------------------------------------------------- #
def test_resumo_em_portugues_com_virgula():
    texto = energia.resumo({"vram_mb": 5017, "ram_commit_mb": 7168, "ram_mb": 100})
    assert texto == "4,9 GB de VRAM e 7,0 GB de RAM"


def test_resumo_ignora_metrica_ausente():
    assert energia.resumo({"vram_mb": 2048, "ram_commit_mb": None}) == "2,0 GB de VRAM"


def test_resumo_ignora_o_que_subiu():
    """Uma métrica que SUBIU no intervalo (outro app pegou VRAM) não vira
    '-0,1 GB': o dono lê esta linha para conferir a economia, e ruído negativo
    faz duvidar de uma medição correta."""
    assert energia.resumo({"vram_mb": -120, "ram_commit_mb": 1024}) == "1,0 GB de RAM"


def test_resumo_sem_nada_mensuravel_diz_isso():
    assert energia.resumo({"vram_mb": None, "ram_commit_mb": None}) == "nada mensurável"
