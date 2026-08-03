"""
O VIGIA — o processo mínimo que espera o celular com o PC em zero.

Três coisas são testadas, e as três podem falhar em silêncio na vida real:

1. **A decisão** (`decidir`): pura, e é o que evita subir dois `app.py` porque a
   tela de carregamento do celular pergunta a cada tique.
2. **O gate**: `acordar` é a única rota que FAZ algo. Se ela vazar, qualquer
   aparelho da LAN levanta o assistente de alguém — e o pedido do dono foi
   explícito: "só abra o servidor quando for autenticado".
3. **O peso**: o valor deste módulo é não ter torch. Um import distraído
   destruiria a razão de ele existir, e nada no runtime avisaria.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from mente_digital import vigia


# --------------------------------------------------------------------------- #
# A decisão                                                                    #
# --------------------------------------------------------------------------- #
def test_servidor_de_pe_nao_sobe_nada():
    v = vigia.decidir(servidor_de_pe=True, subindo_ha=None)
    assert not v.subir and v.estado == "ja_de_pe"


def test_servidor_ausente_sobe():
    v = vigia.decidir(servidor_de_pe=False, subindo_ha=None)
    assert v.subir and v.estado == "subindo_agora"


def test_nao_sobe_duas_vezes_enquanto_o_boot_acontece():
    """A tela de carregamento do celular pergunta a cada tique; sem esta janela,
    cada tique subiria um `app.py` — e o segundo morreria no teste de porta
    DEPOIS de importar meio projeto à toa."""
    v = vigia.decidir(servidor_de_pe=False, subindo_ha=10.0)
    assert not v.subir and v.estado == "subindo"


def test_desiste_de_esperar_depois_da_janela():
    """Boot que não terminou em 90 s falhou; insistir é melhor que ficar preso."""
    v = vigia.decidir(servidor_de_pe=False, subindo_ha=vigia.SEGUNDOS_SUBINDO + 1)
    assert v.subir


# --------------------------------------------------------------------------- #
# O comando que ele dispara                                                    #
# --------------------------------------------------------------------------- #
def test_comando_usa_oculto_e_nao_standby(tmp_path):
    """`--oculto` e não `--standby`: quem pede isto QUER usar o assistente agora,
    então os modelos sobem — o que não faz sentido é abrir uma janela na cara de
    quem está em outro cômodo."""
    cmd = vigia.comando_para_subir(tmp_path, executavel=str(tmp_path / "python.exe"))
    assert cmd[-1] == "--oculto"
    assert cmd[-2].endswith("app.py")


def test_comando_prefere_pythonw(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    cmd = vigia.comando_para_subir(tmp_path, executavel=str(tmp_path / "python.exe"))
    assert cmd[0].endswith("pythonw.exe")


# --------------------------------------------------------------------------- #
# O gate — a parte que não pode vazar                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def vigia_no_ar(monkeypatch, tmp_path):
    """Sobe o vigia de verdade numa porta efêmera, com o `subir_app` trocado por
    um contador. Servidor HTTP real, e não mock: o que pode dar errado aqui é a
    conversa (rota, header, código de status)."""
    subidas = []
    monkeypatch.setattr(vigia, "subir_app", lambda raiz: (subidas.append(raiz) or True))
    monkeypatch.setattr(vigia.settings, "access_token", "segredo-do-dono")
    # Sem servidor de pé: é o cenário do PC em zero.
    monkeypatch.setattr(vigia.rede, "porta_em_uso", lambda h, p: False)

    v = vigia.Vigia(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), vigia._montar_handler(v))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, subidas
    httpd.shutdown()


def _post(url: str, token: str | None) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="POST", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    if token is not None:
        req.add_header("X-Mente-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:      # nosec B310
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}


def test_acordar_sem_token_e_recusado(vigia_no_ar):
    """O pedido do dono, em uma linha: um aparelho qualquer da LAN não levanta o
    assistente de ninguém."""
    base, subidas = vigia_no_ar
    codigo, _ = _post(base + "/vigia/acordar", token=None)
    assert codigo == 401
    assert subidas == []


def test_acordar_com_token_errado_e_recusado(vigia_no_ar):
    base, subidas = vigia_no_ar
    codigo, _ = _post(base + "/vigia/acordar", token="chute")
    assert codigo == 401
    assert subidas == []


def test_acordar_autenticado_levanta_o_assistente(vigia_no_ar):
    base, subidas = vigia_no_ar
    codigo, corpo = _post(base + "/vigia/acordar", token="segredo-do-dono")
    assert codigo == 200
    assert corpo["estado"] == "subindo_agora"
    assert len(subidas) == 1


def test_dois_pedidos_seguidos_sobem_UM_assistente(vigia_no_ar):
    base, subidas = vigia_no_ar
    _post(base + "/vigia/acordar", token="segredo-do-dono")
    codigo, corpo = _post(base + "/vigia/acordar", token="segredo-do-dono")
    assert codigo == 200 and corpo["estado"] == "subindo"
    assert len(subidas) == 1


def test_status_nao_tem_gate(vigia_no_ar):
    """Mesmo critério do `/api/health`: só booleanos, e é o que separa "o PC está
    fora da rede" de "o assistente está dormindo e dá para acordar"."""
    base, _ = vigia_no_ar
    with urllib.request.urlopen(base + "/vigia/status", timeout=5) as r:   # nosec B310
        corpo = json.loads(r.read().decode())
    assert corpo["vigia"] is True and corpo["servidor"] is False


def test_rota_desconhecida_nao_faz_nada(vigia_no_ar):
    base, subidas = vigia_no_ar
    codigo, _ = _post(base + "/vigia/qualquer", token="segredo-do-dono")
    assert codigo == 404 and subidas == []


# --------------------------------------------------------------------------- #
# O peso                                                                       #
# --------------------------------------------------------------------------- #
def test_o_vigia_nao_arrasta_o_mundo():
    """O valor deste módulo é ser barato. Um `mente_digital.rag` distraído traria o
    torch para dentro do processo que existe justamente para não ter torch — e nada
    no runtime avisaria.

    ⚠ A primeira versão deste teste procurava os imports proibidos no TEXTO do
    arquivo, e falhou casando com o próprio comentário que avisa para não
    importá-los. Grep em fonte mede o que está escrito; o que importa é o que foi
    CARREGADO. Daí o subprocesso: ele importa o módulo de verdade e pergunta ao
    `sys.modules`. É a diferença entre ler a placa e medir a estrada."""
    import subprocess

    codigo = (
        "import sys; sys.path.insert(0, r'" + str(Path(vigia.__file__).parents[1]) + "');"
        "from mente_digital import vigia;"
        "print(','.join(m for m in ('torch','transformers','fastapi','uvicorn',"
        "'sentence_transformers','llama_cpp') if m in sys.modules))"
    )
    saida = subprocess.run([sys.executable, "-c", codigo], capture_output=True,  # nosec B603
                           text=True, timeout=120)
    assert saida.returncode == 0, saida.stderr
    pesados = saida.stdout.strip()
    assert pesados == "", f"o vigia carregou peso morto: {pesados}"


# --------------------------------------------------------------------------- #
# Encerrar de vez (o degrau acima do standby)                                  #
# --------------------------------------------------------------------------- #
from mente_digital import standby                                    # noqa: E402


def test_encerra_apos_o_tempo_com_vigia_de_plantao():
    assert standby.deve_encerrar(45, 45 * 60, ocupado=False, ha_vigia=True)


def test_nao_encerra_sem_vigia():
    """Sair sem ninguém de plantão deixaria o celular sem quem chamar — o
    assistente sumiria da rede até o dono voltar ao PC. Sem vigia, o teto é o
    standby."""
    assert not standby.deve_encerrar(45, 99 * 60, ocupado=False, ha_vigia=False)


def test_nao_encerra_ocupado():
    assert not standby.deve_encerrar(45, 99 * 60, ocupado=True, ha_vigia=True)


def test_zero_desliga_o_encerramento():
    assert not standby.deve_encerrar(0, 99 * 60, ocupado=False, ha_vigia=True)


def test_nao_encerra_antes_do_tempo():
    assert not standby.deve_encerrar(45, 44 * 60, ocupado=False, ha_vigia=True)
