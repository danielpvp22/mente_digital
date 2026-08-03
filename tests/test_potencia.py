"""
Watts — GPU pela NVML, CPU pelo ajudante elevado.

O que estes testes provam SEM GPU, SEM driver de kernel e SEM rede (o CI não tem
placa): a aritmética da integral com relógio INJETADO, o fail-soft quando a DLL
não existe, a 1ª chamada sem baseline, e o ajudante ausente. O IO (ctypes sobre a
nvml.dll) é o único ponto não coberto aqui — foi conferido à mão contra a placa
real em 2026-08-03 e o resultado está no relatório, não no CI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mente_digital import energia, potencia, potencia_cpu


@pytest.fixture(autouse=True)
def _limpo():
    """O módulo guarda estado de PROPÓSITO (o baseline é a memória da janela).
    Sem esta limpeza, um teste herdaria o baseline do vizinho e o resultado
    dependeria da ordem de execução — o flake que custa uma tarde para achar."""
    potencia.reiniciar()
    energia._avisou_cpu = False
    yield
    potencia.reiniciar()


# --------------------------------------------------------------------------- #
# A integral (pura, relógio injetado)                                          #
# --------------------------------------------------------------------------- #
def test_primeira_leitura_nao_inventa_media_e_guarda_o_baseline():
    """Sem leitura anterior não existe janela. Devolver watts aqui seria inventar."""
    r = potencia.media_integral(None, 1_000_000, agora=100.0)

    assert r.watts is None
    assert r.baseline == (1_000_000, 100.0)


def test_janela_valida_devolve_a_media_do_periodo():
    """10 s consumindo 1.000 J = 100 W. O contador fala em MILIjoules."""
    baseline = (0, 100.0)

    r = potencia.media_integral(baseline, 1_000_000, agora=110.0)

    assert r.watts == pytest.approx(100.0)
    assert r.baseline == (1_000_000, 110.0)


def test_janela_curta_PRESERVA_o_baseline():
    """O caso que a rota `/api/energia` cria de verdade: ela chama `medir()` duas
    vezes coladas (antes/depois de soltar os modelos). Se a 2ª chamada comesse o
    baseline, a janela de 20 s que estava se formando morreria e a leitura
    honesta nunca chegaria — a faixa ficaria presa no instante para sempre."""
    baseline = (5_000, 100.0)

    r = potencia.media_integral(baseline, 5_010, agora=100.1)

    assert r.watts is None                       # curta demais para afirmar
    assert r.baseline == baseline                # ⚠ INTACTO


def test_janela_curta_nao_devolve_zero_watt():
    """O contador do driver é amostrado: numa janela de milissegundos ele pode não
    ter avançado nenhum milijoule. Sem a guarda, (0 mJ / 0,05 s) = 0 W apareceria
    na tela como 'medi e não há consumo' — a afirmação que este projeto proíbe."""
    r = potencia.media_integral((5_000, 100.0), 5_000, agora=100.05)

    assert r.watts is None


def test_contador_reiniciado_rebaseia_em_vez_de_reportar_negativo():
    """O acumulador zera quando o driver recarrega. Sem esta guarda a subtração
    daria um negativo enorme e a faixa mostraria um watt impossível."""
    r = potencia.media_integral((9_000_000, 100.0), 12_000, agora=200.0)

    assert r.watts is None
    assert r.baseline == (12_000, 200.0)         # recomeça daqui


def test_valor_absurdo_e_descartado_e_rebaseia():
    """1 milhão de watts em 1 s não é uma placa de vídeo — é lixo ou unidade
    errada. Descartar e recomeçar é melhor que propagar."""
    r = potencia.media_integral((0, 100.0), 10_000_000_000, agora=101.0)

    assert r.watts is None
    assert r.baseline == (10_000_000_000, 101.0)


# --------------------------------------------------------------------------- #
# medida_gpu / watts_gpu / limite_gpu                                          #
# --------------------------------------------------------------------------- #
def test_sem_nvml_devolve_None_e_nao_estoura(monkeypatch):
    """O CI e qualquer máquina sem NVIDIA. Fail-soft é o contrato: campo None, e
    o app segue — nunca 0, que seria 'medi e a GPU não consome nada'."""
    monkeypatch.setattr(potencia, "_ler_energia_mj", lambda: None)
    monkeypatch.setattr(potencia, "_ler_uint", lambda _nome: None)

    leitura = potencia.medida_gpu()

    assert leitura == potencia.Leitura(None, None)
    assert potencia.watts_gpu() is None
    assert potencia.limite_gpu() is None


def test_primeira_chamada_cai_para_o_INSTANTE_com_a_origem_certa(monkeypatch):
    """Ainda não há janela, então o instante é o melhor que existe — mas a origem
    tem de dizer isso, senão o número se passa por média."""
    monkeypatch.setattr(potencia, "_ler_energia_mj", lambda: 1_000_000)
    monkeypatch.setattr(potencia, "_ler_uint", lambda _n: 96_349)

    leitura = potencia.medida_gpu(agora=lambda: 100.0)

    assert leitura.watts == pytest.approx(96.349)
    assert leitura.origem == "nvml_instantaneo"


def test_segunda_chamada_ja_e_a_MEDIA_da_janela(monkeypatch):
    """1ª chamada abre a janela; 20 s depois, 2.000 J = 100 W médios — e o valor
    é diferente do instantâneo de propósito, para o teste falhar se o código
    voltar a reportar o instante."""
    energia_lida = {"mj": 1_000_000}
    monkeypatch.setattr(potencia, "_ler_energia_mj", lambda: energia_lida["mj"])
    monkeypatch.setattr(potencia, "_ler_uint", lambda _n: 320_000)

    potencia.medida_gpu(agora=lambda: 100.0)
    energia_lida["mj"] = 3_000_000
    leitura = potencia.medida_gpu(agora=lambda: 120.0)

    assert leitura.watts == pytest.approx(100.0)
    assert leitura.origem == "nvml_integral"


def test_placa_sem_contador_de_energia_usa_o_instante_para_sempre(monkeypatch):
    """`nvmlDeviceGetTotalEnergyConsumption` só existe de Volta para cima; antes
    disso a NVML devolve NOT_SUPPORTED. O módulo tem de degradar, não sumir."""
    monkeypatch.setattr(potencia, "_ler_energia_mj", lambda: None)
    monkeypatch.setattr(potencia, "_ler_uint", lambda _n: 150_000)

    for instante in (100.0, 120.0, 140.0):
        leitura = potencia.medida_gpu(agora=lambda t=instante: t)
        assert leitura == potencia.Leitura(150.0, "nvml_instantaneo")


def test_maquina_sem_NVIDIA_carrega_o_modulo_e_desiste_limpo(monkeypatch):
    """O caminho que a máquina do CI percorre DE VERDADE, e que os testes acima
    pulavam ao trocar os leitores: sem placa, `ctypes.CDLL('nvml.dll')` levanta
    OSError. Nada pode escapar daí — importar este módulo numa máquina sem NVIDIA
    tem de ser tão inofensivo quanto num servidor com quatro placas."""
    import ctypes

    def sem_dll(_nome):
        raise OSError("não achei nvml.dll")

    monkeypatch.setattr(ctypes, "CDLL", sem_dll)

    assert potencia.medida_gpu() == potencia.Leitura(None, None)
    assert potencia.limite_gpu() is None
    assert potencia._abrir_nvml() is None


def test_dll_presente_mas_init_falhando_tambem_e_fail_soft(monkeypatch):
    """Driver a meio caminho (atualizando, versão incompatível): a DLL carrega e
    o `nvmlInit_v2` devolve erro. É diferente de 'sem placa' e tem de terminar no
    mesmo lugar — None, sem exceção."""
    import ctypes

    class LibFalsa:
        def __getattr__(self, _nome):
            def falha(*_a, **_k):
                return 1                         # qualquer coisa != 0 é erro na NVML
            falha.argtypes = falha.restype = None
            return falha

    monkeypatch.setattr(ctypes, "CDLL", lambda _n: LibFalsa())

    assert potencia.medida_gpu() == potencia.Leitura(None, None)


def test_o_aviso_de_falha_sai_UMA_vez(monkeypatch):
    """`medir()` roda a cada 20 s. Numa máquina sem NVIDIA, avisar a cada passada
    encheria o log com a mesma linha para sempre e afogaria o que importa — mas
    calar de vez violaria a regra de não engolir erro. Uma vez resolve as duas."""
    import ctypes

    avisos = []
    monkeypatch.setattr(ctypes, "CDLL", lambda _n: (_ for _ in ()).throw(OSError("nada")))
    monkeypatch.setattr(potencia, "_avisar_uma_vez", lambda m: avisos.append(m))

    for _ in range(5):
        potencia.medida_gpu()

    # A cobrança real é sobre `_avisou_falha`; aqui provamos que `_abrir_nvml` só
    # TENTA carregar uma vez — é o que impede a repetição do aviso e, de quebra,
    # o que impede 5 tentativas de carregar DLL a cada minuto.
    assert len(avisos) == 1, avisos


def test_limite_gpu_le_o_teto_imposto_pelo_driver(monkeypatch):
    """É o denominador que dá escala: 96 W sozinho não diz se a placa está parada."""
    monkeypatch.setattr(
        potencia, "_ler_uint",
        lambda nome: 320_000 if nome == "nvmlDeviceGetEnforcedPowerLimit" else None)

    assert potencia.limite_gpu() == pytest.approx(320.0)


# --------------------------------------------------------------------------- #
# O ajudante da CPU — o lado que LÊ                                            #
# --------------------------------------------------------------------------- #
def test_ajudante_ausente_e_estado_normal_com_motivo(tmp_path):
    """R1: o servidor não roda elevado, então o ajudante pode simplesmente não
    existir. Isso não é erro — é o default. O motivo é o que diferencia 'não
    está de pé' de 'está de pé publicando lixo', que na tela são o mesmo vazio."""
    leitura = potencia_cpu.ler_publicacao(tmp_path / "nao_existe.json")

    assert leitura == potencia_cpu.Leitura(None, None, "ausente")


def test_publicacao_fresca_e_lida_com_a_origem(tmp_path):
    alvo = tmp_path / "p.json"
    potencia_cpu.publicar(42.5, "CPU Package", alvo, agora=1000.0)

    leitura = potencia_cpu.ler_publicacao(alvo, validade_s=15.0, agora=1002.0)

    assert leitura.watts == pytest.approx(42.5)
    assert leitura.origem == "CPU Package"
    assert leitura.motivo is None


def test_ajudante_MORTO_nao_congela_o_numero_na_tela(tmp_path):
    """O defeito que o prazo de validade evita: um ajudante que caiu deixa o
    último arquivo no disco, e sem prazo a faixa mostraria para sempre o watt do
    instante em que ele morreu — um número parado passando por medição viva."""
    alvo = tmp_path / "p.json"
    potencia_cpu.publicar(42.5, "CPU Package", alvo, agora=1000.0)

    leitura = potencia_cpu.ler_publicacao(alvo, validade_s=15.0, agora=1100.0)

    assert leitura.watts is None and leitura.motivo == "vencido"


def test_carimbo_no_FUTURO_tambem_vence(tmp_path):
    """Relógio adiantado do outro lado é tão suspeito quanto atrasado — e sem a
    comparação por módulo, um carimbo no futuro nunca venceria."""
    alvo = tmp_path / "p.json"
    potencia_cpu.publicar(42.5, "CPU Package", alvo, agora=9000.0)

    assert potencia_cpu.ler_publicacao(alvo, 15.0, agora=1000.0).motivo == "vencido"


def test_json_truncado_nao_estoura(tmp_path):
    """Leitura e escrita não têm combinação nenhuma. O `publicar` é atômico para
    isto não acontecer com o NOSSO ajudante, mas o leitor não confia no escritor."""
    alvo = tmp_path / "p.json"
    alvo.write_text('{"watts": 42.5, "ts": 10', encoding="utf-8")

    assert potencia_cpu.ler_publicacao(alvo, 15.0, agora=10.0).motivo == "invalido"


@pytest.mark.parametrize("payload", [
    {"watts": "42.5", "ts": 1000.0},              # texto, não número
    {"watts": True, "ts": 1000.0},                # bool é int no Python: viraria 1,0 W
    {"watts": -5.0, "ts": 1000.0},                # negativo
    {"watts": 0.0, "ts": 1000.0},                 # zero não é medição
    {"watts": 99_999.0, "ts": 1000.0},            # acima do teto de sanidade
    {"watts": float("nan"), "ts": 1000.0},        # NaN passa por qualquer >/<
    {"watts": 42.5},                              # sem carimbo: não dá para envelhecer
    {"watts": 42.5, "ts": "agora"},               # carimbo que não é número
    [42.5],                                       # nem sequer um objeto
])
def test_arquivo_e_tratado_como_ENTRADA_HOSTIL(tmp_path, payload):
    """O arquivo mora numa pasta que o usuário escreve — um processo comum PODE
    plantar uma mentira ali. O estrago possível é um watt errado na faixa (nada
    vira caminho, comando ou ação), e mesmo esse é barrado: só passa float dentro
    de faixa, com carimbo numérico."""
    alvo = tmp_path / "p.json"
    alvo.write_text(json.dumps(payload), encoding="utf-8")

    leitura = potencia_cpu.ler_publicacao(alvo, 15.0, agora=1000.0)

    assert leitura.watts is None and leitura.motivo == "invalido"


def test_rotulo_de_origem_e_estreitado(tmp_path):
    """A origem atravessa para o front. Mesmo com `textContent` do outro lado, o
    estreitamento de charset é de graça e vale pelo hábito."""
    alvo = tmp_path / "p.json"
    alvo.write_text(json.dumps({
        "watts": 42.5, "ts": 1000.0,
        "origem": "<script>alerta()</script>CPU Package" + "x" * 200,
    }), encoding="utf-8")

    origem = potencia_cpu.ler_publicacao(alvo, 15.0, agora=1000.0).origem

    assert "<" not in origem and ">" not in origem
    assert len(origem) <= potencia_cpu._ROTULO_MAX
    assert "CPU Package" in origem


def test_publicar_e_atomico_nao_deixa_temporario(tmp_path):
    """`os.replace` sobre um tmp no MESMO diretório: o leitor nunca vê meio JSON."""
    alvo = tmp_path / "p.json"
    potencia_cpu.publicar(10.0, "CPU Package", alvo, agora=1.0)
    potencia_cpu.publicar(20.0, "CPU Package", alvo, agora=2.0)

    assert list(tmp_path.iterdir()) == [alvo]
    assert json.loads(alvo.read_text(encoding="utf-8"))["watts"] == 20.0


# --------------------------------------------------------------------------- #
# Escolha do sensor (pura)                                                     #
# --------------------------------------------------------------------------- #
def test_package_ganha_de_cores():
    """Num 7950X3D, 'CPU Cores' deixa de fora o IO die e a Infinity Fabric —
    dezenas de watts. Pegar o primeiro da lista daria um número plausível e
    ERRADO, que é o pior tipo: ninguém desconfia dele."""
    escolhido = potencia_cpu.escolher_sensor([
        ("CPU Cores", 90.0), ("CPU Package", 142.0), ("CPU SoC", 22.0)])

    assert escolhido == ("CPU Package", 142.0)


def test_sem_package_fica_o_que_fala_de_cpu():
    assert potencia_cpu.escolher_sensor(
        [("GPU Power", 300.0), ("CPU Cores", 90.0)]) == ("CPU Cores", 90.0)


def test_sensor_sem_valor_ou_lista_vazia_devolve_None():
    """Sensor com `Value` nulo é o que a LHM entrega quando o driver não subiu —
    ou seja, o caso mais comum de instalação incompleta."""
    assert potencia_cpu.escolher_sensor([]) is None
    assert potencia_cpu.escolher_sensor([("CPU Package", None)]) is None
    assert potencia_cpu.escolher_sensor([("CPU Package", 0.0)]) is None


# --------------------------------------------------------------------------- #
# O peso do módulo que o processo ELEVADO importa                              #
# --------------------------------------------------------------------------- #
def test_potencia_cpu_nao_arrasta_peso_para_o_processo_elevado():
    """A mesma trava do vigia, por um motivo mais forte: quem importa este módulo
    roda como ADMINISTRADOR. `pydantic` está na lista junto do torch porque ele
    vem com `config`, e `config` lê o `.env` — deixar um arquivo de texto comum
    influenciar um processo elevado é o que este isolamento evita.

    Ler o import no editor não prova nada (import transitivo não aparece); daí o
    subprocesso, que importa de verdade e pergunta ao `sys.modules`."""
    import subprocess

    raiz = Path(potencia_cpu.__file__).parents[1]
    codigo = (
        "import sys; sys.path.insert(0, r'" + str(raiz) + "');"
        "from mente_digital import potencia_cpu;"
        "print(','.join(m for m in ('torch','pydantic','pydantic_settings','fastapi',"
        "'uvicorn','transformers','sentence_transformers','llama_cpp','sqlite3')"
        " if m in sys.modules))"
    )
    saida = subprocess.run([sys.executable, "-c", codigo], capture_output=True,  # nosec B603
                           text=True, timeout=120)

    assert saida.returncode == 0, saida.stderr
    assert saida.stdout.strip() == "", f"peso morto no processo elevado: {saida.stdout.strip()}"


# --------------------------------------------------------------------------- #
# energia.medir() — a junção                                                   #
# --------------------------------------------------------------------------- #
def test_medir_traz_os_watts_com_a_origem_e_o_teto(monkeypatch, tmp_path):
    """A junção completa, pelo caminho REAL: o ajudante publica no arquivo que o
    `.env` aponta, e `medir()` o encontra por lá. Publicar com `agora=None` usa o
    relógio de verdade de propósito — é o que prova que a validade de 15 s deixa
    passar uma publicação recém-feita (com carimbo fixo, o teste passaria mesmo
    se o prazo estivesse quebrado)."""
    from mente_digital.config import settings

    alvo = tmp_path / "p.json"
    potencia_cpu.publicar(142.0, "CPU Package", alvo)
    monkeypatch.setattr(settings, "potencia_cpu_arquivo", str(alvo))
    monkeypatch.setattr(potencia, "_ler_energia_mj", lambda: None)
    monkeypatch.setattr(potencia, "_ler_uint", lambda nome:
                        320_000 if "Limit" in nome else 96_349)

    m = energia.medir()

    assert m["gpu_watts"] == pytest.approx(96.3)
    assert m["gpu_watts_origem"] == "nvml_instantaneo"
    assert m["gpu_watts_limite"] == 320
    assert m["cpu_watts"] == pytest.approx(142.0)
    assert m["cpu_watts_origem"] == "CPU Package"


def test_medir_sem_nada_disso_devolve_None_e_NUNCA_zero(monkeypatch):
    """O CI, e a máquina do dono antes de ele instalar o driver. Nenhum campo de
    watt pode sair 0: zero é indistinguível de 'medi e não há consumo'."""
    monkeypatch.setattr(potencia, "_ler_energia_mj", lambda: None)
    monkeypatch.setattr(potencia, "_ler_uint", lambda _n: None)
    monkeypatch.setattr(potencia_cpu, "ler_publicacao",
                        lambda *_a, **_k: potencia_cpu.Leitura(None, None, "ausente"))

    m = energia.medir()

    for campo in ("gpu_watts", "gpu_watts_limite", "cpu_watts"):
        assert m[campo] is None, campo
    assert m["cpu_watts_motivo"] == "ausente"


def test_delta_ignora_os_watts(monkeypatch):
    """Watt é INSTANTÂNEO: "liberou 40 W" não quer dizer nada (a placa oscila
    sozinha entre duas leituras). O `delta` continua só com memória, e este teste
    existe para o próximo que passar por aqui não 'completar' a lista."""
    antes = {"ram_mb": 100, "ram_commit_mb": 200, "vram_mb": 300, "gpu_watts": 320.0}
    depois = {"ram_mb": 40, "ram_commit_mb": 80, "vram_mb": 90, "gpu_watts": 96.0}

    d = energia.delta(antes, depois)

    assert d == {"ram_mb": 60, "ram_commit_mb": 120, "vram_mb": 210}
    assert "gpu_watts" not in d


# --------------------------------------------------------------------------- #
# O ajudante elevado                                                           #
# --------------------------------------------------------------------------- #
def _carregar_ajudante():
    """Importa `scripts/ajudante_watts.py` pelo caminho — `scripts/` não é pacote."""
    import importlib.util

    caminho = Path(potencia_cpu.__file__).parents[1] / "scripts" / "ajudante_watts.py"
    spec = importlib.util.spec_from_file_location("ajudante_watts", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_ajudante_sem_a_dll_explica_e_sai_sem_estourar(capsys, tmp_path):
    """A lib ausente é ESTADO NORMAL (R2: quem instala o driver é o dono), não
    crash. E a saída tem de dizer para onde ir — este script roda uma vez na
    instalação e depois some numa janela minimizada."""
    ajudante = _carregar_ajudante()

    codigo = ajudante.main(["--lhm", str(tmp_path / "nao_existe.dll"), "--uma-vez"])

    assert codigo == 2
    assert "INSTALACAO_WATTS.md" in capsys.readouterr().out


def test_ajudante_acha_a_dll_indicada(tmp_path):
    dll = tmp_path / "LibreHardwareMonitorLib.dll"
    dll.write_bytes(b"nao e uma dll de verdade, mas existe")
    ajudante = _carregar_ajudante()

    assert ajudante.achar_dll(str(dll)) == dll
    assert ajudante.achar_dll(str(tmp_path / "outra.dll")) is None


def test_ajudante_sem_sensor_reclama_do_privilegio(monkeypatch):
    """Sensor vazio é quase sempre 'o driver não subiu porque não sou admin'. Sem
    esta mensagem o dono veria um silêncio e concluiria que a CPU não tem sensor."""
    ajudante = _carregar_ajudante()
    monkeypatch.setattr(ajudante, "ler_sensores", lambda _c: [("CPU Package", None)])

    with pytest.raises(ajudante.SensorIndisponivel) as erro:
        ajudante.medir(object())

    assert "privilégio" in str(erro.value)


def test_ajudante_publica_o_par_sensor_watts(monkeypatch, tmp_path):
    ajudante = _carregar_ajudante()
    monkeypatch.setattr(ajudante, "ler_sensores",
                        lambda _c: [("CPU Cores", 90.0), ("CPU Package", 142.0)])

    nome, watts = ajudante.medir(object())
    potencia_cpu.publicar(watts, nome, tmp_path / "p.json", agora=1000.0)

    lido = potencia_cpu.ler_publicacao(tmp_path / "p.json", 15.0, agora=1001.0)
    assert (lido.watts, lido.origem) == (142.0, "CPU Package")


# --------------------------------------------------------------------------- #
# A SOMA (pedido do dono, 2026-08-03): o chip mostra GPU + CPU num número só    #
# --------------------------------------------------------------------------- #
def test_soma_as_duas_parcelas():
    from mente_digital.energia import somar_watts

    assert somar_watts(96.4, 142.3) == 238.7


def test_sem_a_parcela_da_CPU_NAO_ha_total():
    """O estado NORMAL desta máquina: sem o ajudante elevado de pé, o Windows não
    entrega a potência do pacote da CPU.

    Somar só a GPU e chamar de total reportaria ~96 W onde a máquina gasta bem
    mais — pior que não medir, porque um número plausível não levanta suspeita
    nenhuma. É a mesma regra do `medir()`, que nunca escreve `0 W` para campo
    ausente: o front então mostra a parcela que TEM, rotulada 'GPU'."""
    from mente_digital.energia import somar_watts

    assert somar_watts(96.4, None) is None
    assert somar_watts(None, 142.3) is None
    assert somar_watts(None, None) is None


def test_zero_watt_medido_e_diferente_de_ausente():
    """0 é uma MEDIDA (placa em repouso profundo), não uma ausência — e um `if not
    gpu` no lugar de `is None` apagaria o total inteiro por causa dela."""
    from mente_digital.energia import somar_watts

    assert somar_watts(0.0, 142.3) == 142.3
