"""
Três problemas achados no primeiro teste em produção:
1. web genérica apresentada como dado do usuário (rótulo da RAM + regra da fusão);
2. contaminação de query (histórico vazando em pergunta auto-contida);
3. ruído de idioma (átomos em chinês diluindo a recuperação).
"""
from datetime import datetime

import agent
import textutils
from rag import NENHUM, LocalResult


# --- Problema 3: idioma errado ----------------------------------------------
def test_fracao_cjk_alta_para_chines_zero_para_portugues():
    assert textutils.fracao_cjk("以太坊的价格市场需求") > 0.8
    assert textutils.fracao_cjk("O TensorRT otimiza o modelo YOLO") == 0.0
    # PT que cita UM termo estrangeiro não dispara (proporção baixa)
    assert textutils.fracao_cjk("O modo 中 é usado, mas raramente na prática toda") < 0.15


def test_normalizar_rejeita_atomo_em_chines():
    corpo = "以太坊的价格：全球市场的需求可以与以太坊 GPU 矿工的黄金时期进行比较。"
    assert agent.normalizar_atomo(f"## 生成回答\n{corpo}", "x.json", datetime(2026, 7, 17)) == ""


def test_normalizar_mantem_atomo_em_portugues():
    out = agent.normalizar_atomo(
        "## Mineração de Ethereum\nA demanda global se compara à era de ouro dos mineradores.",
        "x.json", datetime(2026, 7, 17))
    assert "demanda global" in out


# --- Problema 2: contaminação de query --------------------------------------
def test_pergunta_auto_contida_nao_referencia_contexto():
    # 'como funciona o tensor RT?' não tem pronome -> não deve puxar histórico
    assert agent.referencia_contexto("como funciona o tensor RT?") is False
    assert agent.referencia_contexto("quais os itens da lista de compra do esp32?") is False


def test_pergunta_com_pronome_referencia_contexto():
    assert agent.referencia_contexto("como ele funciona?") is True
    assert agent.referencia_contexto("e disso, qual o custo?") is True
    assert agent.referencia_contexto("explique melhor essa parte") is True


# --- Problema 1: web genérica rotulada --------------------------------------
def test_contexto_rotula_ram_como_web_generica():
    ctx = agent.Agent._montar_contexto(
        LocalResult(NENHUM, None, False), ram=["um kit ESP32 qualquer com sensor PIR"])
    assert "GENÉRICA" in ctx
    assert "WEB" in ctx
    # o conteúdo segue lá, só que atribuído
    assert "sensor PIR" in ctx


def test_contexto_banco_local_nao_vira_web():
    # átomo do vault NÃO pode ser rotulado como web genérica
    local = LocalResult("[Local - Confiança: 1.0] O TensorRT otimiza YOLO", 0.3, True)
    ctx = agent.Agent._montar_contexto(local, ram=[])
    assert "Banco Local" in ctx
    assert "GENÉRICA" not in ctx
