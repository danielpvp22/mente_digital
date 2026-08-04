"""O wattímetro AVULSO — o que registra o consumo com o assistente fechado.

Ele existe porque a hora em que o PC mais gasta é justamente a que o dono fecha o
assistente para jogar. Os dois testes que importam aqui protegem as duas promessas
que ele faz: **não contar dobrado** e **não atrapalhar o jogo**.
"""
from __future__ import annotations

import importlib.util
import subprocess  # nosec B404
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def _carregar():
    """O script vive em `scripts/`, que não é pacote. Carregar pelo caminho evita
    depender de sys.path montado por fora (que muda entre rodar o pytest da raiz e
    de dentro de tests/)."""
    caminho = RAIZ / "scripts" / "registrar_consumo.py"
    spec = importlib.util.spec_from_file_location("registrar_consumo", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _carregar()


# --------------------------------------------------------------------------- #
# A guarda contra contar dobrado                                               #
# --------------------------------------------------------------------------- #
def test_recusa_subir_com_o_assistente_no_ar(monkeypatch, capsys):
    """`RegistroConsumo` guarda o baseline do contador EM RAM, por instância. Com o
    app no ar e este script junto, os dois leem o MESMO contador monotônico da NVML,
    cada um calcula o delta desde a sua última leitura, e ambos somam no banco — a
    energia conta dobrado.

    O que torna isso perigoso é o sintoma: não aparece como falha, aparece como um
    dia caro. E um dia caro é indistinguível de um dia em que se jogou muito. Por
    isso o script RECUSA em vez de avisar."""
    monkeypatch.setattr(rc, "servidor_no_ar", lambda: True)

    assert rc.main([]) == 2

    saida = capsys.readouterr()
    assert "DOBRADO" in saida.err


def test_deixa_rodar_com_o_assistente_fechado(monkeypatch):
    """O espelho do teste acima: a guarda não pode ser tão zelosa que impeça o uso
    normal. Aqui ela passa, e o motivo de parar é outro (o `--enquanto-jogo` sem
    jogo nenhum), o que prova que a recusa anterior foi da porta e não do resto."""
    monkeypatch.setattr(rc, "servidor_no_ar", lambda: False)
    monkeypatch.setattr(rc.settings, "consumo_habilitado", True)
    monkeypatch.setattr(rc, "_jogo_em_execucao", lambda: None)

    assert rc.main(["--enquanto-jogo", "--espera-jogo", "0", "--silencioso"]) == 0


def test_em_plantao_ele_ESPERA_em_vez_de_recusar(monkeypatch):
    """A guarda tem de se comportar diferente nos dois modos, e o motivo é o ciclo
    de vida: uma execução manual acontece com o app fechado e recusar é certo; o
    plantão atravessa o dia INTEIRO, e o assistente sobe e desce várias vezes nele.

    Recusar em plantão mataria o processo no primeiro boot do app — e o dono só
    descobriria pela cobertura despencando semanas depois, que é o modo de falhar
    mais caro que este subsistema tem (o gráfico mostraria "dia econômico")."""
    monkeypatch.setattr(rc, "servidor_no_ar", lambda: True)      # app NO AR
    monkeypatch.setattr(rc.settings, "consumo_habilitado", True)
    monkeypatch.setattr(rc, "_PASSO_ESPERA_S", 0.0)
    # Sem jogo + espera zero: o laço dá uma volta (na qual só encontra o app no ar,
    # se cala e continua) e então encerra pelo `--enquanto-jogo`. Se a guarda ainda
    # recusasse, o retorno seria 2 e nada disso teria rodado.
    monkeypatch.setattr(rc, "_jogo_em_execucao", lambda: None)

    codigo = rc.main(["--plantao", "--enquanto-jogo", "--espera-jogo", "0",
                      "--silencioso"])

    assert codigo == 0


def test_plantao_nao_amostra_enquanto_o_assistente_mede(monkeypatch):
    """O silêncio precisa ser REAL, não só um log dizendo que está calado: se ele
    amostrasse junto, os dois somariam o mesmo delta do contador."""
    chamou = []
    monkeypatch.setattr(rc, "servidor_no_ar", lambda: True)
    monkeypatch.setattr(rc.settings, "consumo_habilitado", True)
    monkeypatch.setattr(rc, "_PASSO_ESPERA_S", 0.0)
    monkeypatch.setattr(rc, "_jogo_em_execucao", lambda: None)
    monkeypatch.setattr(rc, "uma_amostra", lambda _reg: chamou.append(1))

    rc.main(["--plantao", "--enquanto-jogo", "--espera-jogo", "0", "--silencioso"])

    assert chamou == [], "amostrou com o assistente no ar — é o contar dobrado"


def test_recusa_com_o_wattimetro_desligado(monkeypatch, capsys):
    monkeypatch.setattr(rc, "servidor_no_ar", lambda: False)
    monkeypatch.setattr(rc.settings, "consumo_habilitado", False)

    assert rc.main([]) == 2
    assert "CONSUMO_HABILITADO" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# A CPU ausente é `None`, jamais zero                                          #
# --------------------------------------------------------------------------- #
def test_cpu_sem_ajudante_vira_None_e_nunca_zero(monkeypatch):
    """Durante a partida o `jogo_ativo.py` desabilita o PawnIO para o BattlEye não
    dividir o andar do kernel — sem driver não há MSR, e sem MSR não há watt de
    pacote. `None` faz o `tomada` modelar a faixa `repouso..PPT` e dizer que
    modelou; um zero AFIRMARIA que a CPU não gastou nada durante horas de jogo, e
    essa é uma mentira que soma silenciosamente no gráfico do mês."""
    class _SemWatt:
        watts = None

    monkeypatch.setattr(rc.potencia_cpu, "ler_publicacao", lambda *a, **k: _SemWatt())
    monkeypatch.setattr(rc.potencia, "medida_gpu", lambda *a, **k: type("L", (), {"watts": 120.0})())

    medida = rc.medir_sensores()

    assert medida["cpu_watts"] is None
    assert medida["cpu_watts"] != 0        # explícito: o zero é o erro que se teme
    assert medida["gpu_watts"] == 120.0


def test_uma_amostra_respeita_o_intervalo(monkeypatch):
    """Sem isto o laço gravaria uma linha por segundo num banco que também serve o
    histórico de chat."""
    class _RegistroFalso:
        def __init__(self) -> None:
            self.chamadas = 0

        def devido(self, _intervalo):
            return False

        def registrar(self, *a, **k):      # pragma: no cover - não deve ser chamado
            self.chamadas += 1
            return {}

    reg = _RegistroFalso()
    assert rc.uma_amostra(reg) is None
    assert reg.chamadas == 0


# --------------------------------------------------------------------------- #
# O início automático                                                          #
# --------------------------------------------------------------------------- #
def test_instala_o_plantao_e_nao_o_modo_jogo(monkeypatch, tmp_path):
    """Preso ao jogo, o medidor cobriria a partida e deixaria de fora a MADRUGADA —
    que é o pedaço maior do buraco, porque o `app.py` se encerra sozinho após 45 min
    ocioso. O argumento gravado no `.vbs` é o que decide isso, e ele é fácil de
    trocar por engano num arquivo que ninguém relê."""
    from mente_digital import inicializacao

    gravado = {}

    def _fake_instalar(raiz, argumentos="--vigia", nome=inicializacao.NOME_ARQUIVO,
                       alvo="app.py", cabecalho=""):
        gravado.update(raiz=raiz, argumentos=argumentos, nome=nome, alvo=alvo)
        return tmp_path / nome

    monkeypatch.setattr(inicializacao, "instalar", _fake_instalar)

    assert rc.main(["--instalar-inicio"]) == 0

    assert "--plantao" in gravado["argumentos"]
    assert "--enquanto-jogo" not in gravado["argumentos"]
    # E aponta para ESTE script, não para o app.py (o default da função).
    assert gravado["alvo"].endswith("registrar_consumo.py")
    # Arquivo próprio: desinstalar o wattímetro não pode derrubar o vigia.
    assert gravado["nome"] == inicializacao.NOME_WATTIMETRO
    assert gravado["nome"] != inicializacao.NOME_ARQUIVO


def test_o_vbs_do_wattimetro_nao_colide_com_o_do_vigia():
    """Dois moradores na mesma pasta. Se os nomes empatassem, instalar um apagaria
    o outro em silêncio — e o dono descobriria com o celular não levantando o PC."""
    from mente_digital import inicializacao

    vigia = inicializacao.caminho_atalho()
    watt = inicializacao.caminho_atalho(inicializacao.NOME_WATTIMETRO)

    assert vigia != watt
    assert vigia.parent == watt.parent
    # O default continua sendo o vigia: todo chamador antigo vale sem mudar.
    assert vigia.name == inicializacao.NOME_ARQUIVO


# --------------------------------------------------------------------------- #
# A promessa de não atrapalhar o jogo                                          #
# --------------------------------------------------------------------------- #
def test_nao_arrasta_torch_NEM_DEPOIS_DE_MEDIR():
    """A razão de ser deste script é medir sem custar nada à GPU do jogo.

    ⚠ ESTE TESTE MEDE DEPOIS DA CHAMADA, e não só depois do import — é a diferença
    que o desenvolvimento pagou. A primeira versão do amostrador usava
    `energia.medir()` (a mesma função do scheduler), e ali o torch NÃO entra pelo
    import: entra pela CHAMADA, porque a medição completa do app inclui a leitura de
    VRAM e `torch.cuda` INICIALIZA UM CONTEXTO CUDA — algumas centenas de MB na
    mesma placa em que o jogo está rodando. Um teste calcado só no import (como o do
    `vigia`, onde basta) passaria verde e não protegeria nada.

    Roda em subprocesso pelo motivo já documentado no `test_vigia`: grep no fonte
    mede o que está escrito, `sys.modules` mede o que foi carregado."""
    codigo = (
        "import sys; sys.path.insert(0, r'" + str(RAIZ) + "');"
        "import importlib.util;"
        "spec = importlib.util.spec_from_file_location('rc', r'"
        + str(RAIZ / "scripts" / "registrar_consumo.py") + "');"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "m.medir_sensores();"          # <- a CHAMADA, que é onde o torch entrava
        "print('PESADOS=' + ','.join(x for x in ('torch','transformers',"
        "'sentence_transformers','llama_cpp','chromadb','faster_whisper')"
        " if x in sys.modules))"
    )
    saida = subprocess.run([sys.executable, "-c", codigo], capture_output=True,  # nosec B603
                           text=True, timeout=180)
    assert saida.returncode == 0, saida.stderr
    # ⚠ A RESPOSTA VEM MARCADA, e não como "o stdout inteiro". A primeira versão
    # comparava `saida.stdout.strip() == ""` e quebrou no CI: sem driver NVIDIA o
    # `potencia` emite um telemetry "NVML indisponível" — no stdout, que não é só
    # meu. O teste então acusava peso morto citando uma linha de log. Verde na
    # máquina com GPU o tempo todo; vermelho no runner, pelo motivo errado.
    linhas = [ln for ln in saida.stdout.splitlines() if ln.startswith("PESADOS=")]
    assert linhas, f"o subprocesso não respondeu:\n{saida.stdout}\n{saida.stderr}"
    pesados = linhas[-1].split("=", 1)[1].strip()
    assert pesados == "", f"o amostrador tocou a GPU do jogo: carregou {pesados}"
