"""Validade PRÓPRIA de um código de pareamento (2026-08-06).

O caso que abriu isto: convidar alguém que só vai parear daqui a uma hora. Os 10 min do
padrão cobrem "gerar no PC e digitar no celular que está na minha mão", não "mandei pelo
WhatsApp"; a recusa chega como `codigo_expirado`, que do lado de lá é indistinguível de
dedo errado.

A saída ÓBVIA era subir `MENTE_APARELHOS_CODIGO_VALIDADE_MINUTOS` no `.env`, e ela tem
três defeitos que estes testes existem para provar que a nossa não tem: valeria para
TODOS os códigos, seria RETROATIVA (a validade é conferida no pareamento, então
ressuscitaria qualquer código antigo e esquecido) e exigiria lembrar de baixar de volta.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from mente_digital import aparelhos as regras
from mente_digital.registro_aparelhos import RegistroAparelhos

TETO = 4
PADRAO = 10          # o `settings.aparelhos_codigo_validade_minutos` de hoje
T0 = datetime(2026, 8, 6, 12, 0, 0)


@pytest.fixture()
def registro(tmp_path):
    """Relógio INJETADO — o teste precisa andar duas horas sem dormir duas horas."""
    relogio = {"agora": T0}
    reg = RegistroAparelhos(str(tmp_path / "t.db"), relogio=lambda: relogio["agora"])
    reg.init()
    reg.avancar = lambda **kw: relogio.__setitem__("agora", relogio["agora"] + timedelta(**kw))
    return reg


def _parear(reg, codigo, ip="203.0.113.7"):
    return reg.parear(codigo, ip, TETO, PADRAO, expira_dias=90)


# --- O caso do dono ----------------------------------------------------------
def test_codigo_de_duas_horas_ainda_vale_depois_de_uma_hora_e_meia(registro):
    """O pedido literal: um código que dure duas horas."""
    codigo = registro.emitir_codigo("celular do felipe", TETO, "felipe", validade_minutos=120)

    registro.avancar(minutes=90)

    assert _parear(registro, codigo).ok


def test_e_morre_no_prazo_que_pediu_e_nao_depois(registro):
    """Validade maior é uma janela maior, não uma janela aberta."""
    codigo = registro.emitir_codigo("celular do felipe", TETO, "felipe", validade_minutos=120)

    registro.avancar(minutes=121)

    r = _parear(registro, codigo)
    assert not r.ok and r.motivo == regras.MOTIVO_CODIGO_EXPIRADO


def test_o_codigo_LONGO_nao_alarga_a_janela_dos_OUTROS(registro):
    """O ponto inteiro da validade por código, e o que o `.env` não daria: emitir um de
    120 min não pode transformar o próximo código de 10 min num de 120."""
    registro.emitir_codigo("do felipe", TETO, "felipe", validade_minutos=120)
    comum = registro.emitir_codigo("outro", TETO, "ana")

    registro.avancar(minutes=11)

    r = _parear(registro, comum)
    assert not r.ok and r.motivo == regras.MOTIVO_CODIGO_EXPIRADO


def test_sem_pedir_nada_o_comportamento_e_byte_a_byte_o_de_hoje(registro):
    """Compatibilidade: quem não passa `validade_minutos` continua com o padrão."""
    codigo = registro.emitir_codigo("celular", TETO)

    registro.avancar(minutes=9)
    assert _parear(registro, codigo).ok


def test_continua_de_uso_unico_por_mais_longo_que_seja(registro):
    """Vida curta e uso único são os DOIS guardas da rota sem gate. Esticar um não pode
    afrouxar o outro — o segundo pareamento tem de bater em `codigo_usado`."""
    codigo = registro.emitir_codigo("do felipe", TETO, "felipe", validade_minutos=120)

    assert _parear(registro, codigo).ok
    registro.avancar(minutes=5)
    r = _parear(registro, codigo)

    assert not r.ok and r.motivo == regras.MOTIVO_CODIGO_USADO


# --- O teto ------------------------------------------------------------------
def test_validade_absurda_e_RECUSADA_em_vez_de_cortada(registro):
    """"Vida curta" é um guarda nomeado no docstring de `POST /api/aparelhos/parear`.
    Um `--minutos 99999` digitado uma vez o aposentaria em silêncio.

    E a recusa é ERRO, não corte para o teto: o servidor escolhendo um prazo que o dono
    não pediu é ele adivinhando parte de uma decisão de segurança — a mesma razão pela
    qual `normalizar_codigo` se recusa a consertar sósia de caractere."""
    with pytest.raises(ValueError):
        registro.emitir_codigo("eterno", TETO, "felipe", validade_minutos=99999)

    with pytest.raises(ValueError):
        registro.emitir_codigo("negativo", TETO, "felipe", validade_minutos=-5)


def test_a_recusa_acontece_ANTES_de_gravar(registro):
    """Um código com prazo inválido não pode ficar no banco esperando alguém usá-lo."""
    with pytest.raises(ValueError):
        registro.emitir_codigo("eterno", TETO, "felipe", validade_minutos=99999)

    with sqlite3.connect(registro.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM aparelhos_pareamento").fetchone()[0] == 0


def test_o_teto_permite_o_caso_de_uso_real(registro):
    """Duas horas (o pedido) tem de caber — um teto que barra o motivo de existir da
    função seria só a função desligada com passos a mais."""
    assert regras.validar_validade(120) == 120
    assert regras.VALIDADE_MAX_MINUTOS >= 120


# --- Compatibilidade com o banco que já existe -------------------------------
def test_banco_ANTERIOR_a_coluna_migra_e_o_codigo_antigo_segue_valendo_o_de_sempre(tmp_path):
    """A coluna nasceu depois. Um código emitido antes dela tem NULL ali, e NULL
    SIGNIFICA "use o padrão" — daí a migração não fazer backfill (ao contrário da do
    `usuario`): carimbar os antigos congelaria neles um número que o dono ainda pode
    mudar no `.env`, alterando retroativamente códigos já emitidos."""
    caminho = str(tmp_path / "antigo.db")
    with sqlite3.connect(caminho) as conn:      # o schema de ANTES, na mão
        conn.execute("""CREATE TABLE aparelhos_pareamento
                        (codigo TEXT PRIMARY KEY, emitido_em TEXT, usado_em TEXT,
                         apelido TEXT)""")
        conn.execute("INSERT INTO aparelhos_pareamento VALUES (?, ?, NULL, ?)",
                     ("VELHO12345", T0.isoformat(), "celular antigo"))

    relogio = {"agora": T0}
    reg = RegistroAparelhos(caminho, relogio=lambda: relogio["agora"])
    reg.init()                                   # migra: usuario + validade_minutos

    relogio["agora"] = T0 + timedelta(minutes=9)
    assert reg.parear("VELHO12345", "203.0.113.7", TETO, PADRAO, expira_dias=90).ok


def test_init_e_idempotente(tmp_path):
    """Roda a cada boot — a segunda passada não pode estourar no ALTER."""
    reg = RegistroAparelhos(str(tmp_path / "i.db"))
    reg.init()
    reg.init()

    with sqlite3.connect(reg.path) as conn:
        colunas = {c[1] for c in conn.execute("PRAGMA table_info(aparelhos_pareamento)")}
    assert "validade_minutos" in colunas


# --- As funções puras --------------------------------------------------------
def test_validade_efetiva_prefere_a_do_codigo_e_cai_no_padrao_quando_nao_ha():
    assert regras.validade_efetiva(120, PADRAO) == 120
    assert regras.validade_efetiva(None, PADRAO) == PADRAO
    # 0 é tratado como "não pediu" e NÃO como "expira imediatamente": é o que o SQLite
    # devolve de um campo nunca preenchido em banco montado à mão, e um código que nasce
    # morto seria um bug mudo.
    assert regras.validade_efetiva(0, PADRAO) == PADRAO


# --- A flag dos dois painéis -------------------------------------------------
def test_separar_minutos_tira_a_flag_e_devolve_o_resto_intacto():
    assert regras.separar_minutos(["celular do felipe", "felipe", "--minutos", "120"]) == (
        ["celular do felipe", "felipe"], 120)
    assert regras.separar_minutos(["--minutos=90", "cel"]) == (["cel"], 90)


def test_sem_a_flag_nada_muda():
    assert regras.separar_minutos(["celular da ana", "ana"]) == (["celular da ana", "ana"], None)


def test_a_flag_sai_ANTES_do_parsing_posicional():
    """⚠ O motivo de a flag ser extraída primeiro: nos dois scripts o ÚLTIMO argumento
    vira o USUÁRIO quando cabe na regra de nome — e "120" cabe (dígitos são válidos).
    Deixá-la no meio criaria o usuário "120" e um apelido com "--minutos" dentro, calado.
    """
    resto, minutos = regras.separar_minutos(["cel do felipe", "--minutos", "120", "felipe"])

    assert minutos == 120
    assert resto[-1] == "felipe", "o usuário tem de sobreviver à extração da flag"
    assert not any("minutos" in x or x == "120" for x in resto)


def test_flag_sem_numero_ou_com_lixo_falha_em_vez_de_ser_ignorada():
    """Ignorar a flag malformada daria o pior resultado possível: o dono acreditando ter
    pedido 2 h e o código morrendo em 10 min, longe da causa."""
    with pytest.raises(ValueError):
        regras.separar_minutos(["cel", "--minutos"])
    with pytest.raises(ValueError):
        regras.separar_minutos(["cel", "--minutos", "duas horas"])
    with pytest.raises(ValueError):
        regras.separar_minutos(["cel", "--minutos", "99999"])


# --------------------------------------------------------------------------- #
# `--sem-prazo`: vale ATE SER USADO (dono, 2026-08-13)                         #
# --------------------------------------------------------------------------- #
class TestSemPrazo:
    """⚠ ISTO ABRE MAO DE UM DOS DOIS GUARDAS.

    `POST /api/aparelhos/parear` e a unica rota sem gate, e o docstring dela nomeia
    os dois que a sustentam: USO UNICO e VIDA CURTA. Aqui sobra o primeiro. Estes
    testes existem para garantir que o que sobrou continua de pe — um codigo sem
    prazo que tambem deixasse de ser de uso unico nao teria guarda nenhum."""

    def test_o_uso_unico_continua_valendo(self):
        """O guarda que SOBRA. Se este cair, a rota fica sem nenhum."""
        emitido = datetime(2026, 8, 13, 10, 0)
        assert regras.codigo_valido(emitido, usado=True,
                                    agora=datetime(2026, 8, 13, 10, 1),
                                    validade_minutos=regras.SEM_PRAZO) == regras.MOTIVO_CODIGO_USADO

    def test_usado_vence_o_relogio_no_motivo(self):
        """A ordem importa: inverter faria um codigo sem prazo JA USADO responder
        "expirado" — motivo errado para a recusa certa, que e como uma investigacao
        comeca no lugar errado."""
        emitido = datetime(2026, 8, 13, 10, 0)
        motivo = regras.codigo_valido(emitido, usado=True,
                                      agora=datetime(2027, 1, 1),
                                      validade_minutos=regras.SEM_PRAZO)
        assert motivo == regras.MOTIVO_CODIGO_USADO

    @pytest.mark.parametrize("depois", [
        datetime(2026, 8, 13, 10, 11),      # passou o padrao de 10 min
        datetime(2026, 8, 13, 15, 0),       # passou o teto de 4 h
        datetime(2027, 8, 13, 10, 0),       # um ANO depois
    ])
    def test_nao_expira_pelo_relogio(self, depois):
        emitido = datetime(2026, 8, 13, 10, 0)
        assert regras.codigo_valido(emitido, usado=False, agora=depois,
                                    validade_minutos=regras.SEM_PRAZO) is None

    def test_validade_efetiva_ATRAVESSA_o_sem_prazo(self):
        """⚠ O ramo mais facil de esquecer. `-1` reprova em `> 0`, entao sem o ramo
        proprio ele cairia no padrao do settings — expirando em 10 min exatamente o
        convite emitido para nao expirar, e sem erro nenhum avisando."""
        assert regras.validade_efetiva(regras.SEM_PRAZO, 10) == regras.SEM_PRAZO

    def test_o_sentinela_NAO_e_zero(self):
        """`0` ja significa "nao pediu validade propria" — e o que o SQLite devolve
        de campo nunca preenchido. Reusa-lo faria todo codigo antigo virar eterno de
        uma vez, calado."""
        assert regras.SEM_PRAZO != 0
        assert regras.validade_efetiva(0, 10) == 10
        assert regras.validade_efetiva(None, 10) == 10

    def test_numero_absurdo_continua_RECUSADO(self):
        """`--sem-prazo` e uma escolha explicita; `--minutos 99999` e dedo
        escorregando. A distincao e o ponto — se o numero grande virasse "sem
        prazo", a decisao de seguranca aconteceria por acidente."""
        with pytest.raises(ValueError):
            regras.validar_validade(99999)
        with pytest.raises(ValueError):
            regras.validar_validade(-5)          # so o -1 EXATO passa
        assert regras.validar_validade(regras.SEM_PRAZO) == regras.SEM_PRAZO

    def test_a_mensagem_de_erro_ENSINA_a_saida(self):
        """Quem digitou 99999 queria "que dure": a recusa tem de dizer como."""
        with pytest.raises(ValueError, match="sem-prazo"):
            regras.validar_validade(99999)

    def test_a_flag_sai_do_argv_sem_valor(self):
        resto, minutos = regras.separar_minutos(["cel do felipe", "--sem-prazo", "felipe"])
        assert resto == ["cel do felipe", "felipe"]
        assert minutos == regras.SEM_PRAZO

    def test_o_convite_nao_imprime_o_sentinela_cru(self):
        """⚠ Com `{:.0f}` o convite diria "Vale -1 minutos" — um numero negativo na
        unica instrucao que o convidado precisa entender. Mesmo defeito que o
        `vez.ANONIMO` pagou: valor especial precisa de FRASE especial, senao escapa
        pela formatacao."""
        import importlib.util
        from pathlib import Path
        caminho = Path(__file__).resolve().parent.parent / "scripts" / "gerar_convite.py"
        spec = importlib.util.spec_from_file_location("gerar_convite", caminho)
        gc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gc)

        html = gc.montar_html("cel do felipe", "felipe", "ABC123", "", regras.SEM_PRAZO)
        assert "-1" not in html
        assert "até você usá-lo" in html
        # e o caso normal nao regrediu
        assert "120 minutos" in gc.montar_html("x", "y", "ABC", "", 120)
