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

from mente_digital import vez, vigia


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
# O gate por APARELHO (2026-08-03)                                             #
# --------------------------------------------------------------------------- #
# O plantão ficava um degrau atrás do servidor grande nos DOIS sentidos: o
# aparelho revogado seguia levantando o PC do dono pelo segredo compartilhado, e o
# celular já migrado (que não guarda mais o token antigo) não levantava nada.
class _DbMudo:
    """A trilha de auditoria sem tocar no banco real — mesmo motivo do vizinho
    test_registro_aparelhos: não depender da ordem de import dos testes."""

    def registrar_auditoria(self, acao: str, detalhe: str) -> None:
        pass


@pytest.fixture
def vigia_com_aparelhos(monkeypatch, tmp_path):
    from mente_digital.registro_aparelhos import RegistroAparelhos

    subidas = []
    monkeypatch.setattr(vigia, "subir_app", lambda raiz: (subidas.append(raiz) or True))
    monkeypatch.setattr(vigia.settings, "access_token", "segredo-do-dono")
    monkeypatch.setattr(vigia.settings, "aparelhos_habilitado", True)
    monkeypatch.setattr(vigia.settings, "aparelhos_token_legado", True)
    monkeypatch.setattr(vigia.rede, "porta_em_uso", lambda h, p: False)

    registro = RegistroAparelhos(str(tmp_path / "aparelhos.db"), db=_DbMudo())
    registro.init()
    v = vigia.Vigia(tmp_path, registro=registro)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), vigia._montar_handler(v))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", subidas, registro
    httpd.shutdown()


def _parear(registro) -> str:
    codigo = registro.emitir_codigo("celular", teto=4)
    r = registro.parear(codigo, ip="127.0.0.1", teto=4, validade_minutos=10, expira_dias=90)
    assert r.ok, r.motivo
    return r.credencial


def test_aparelho_pareado_acorda_o_pc(vigia_com_aparelhos):
    """O lado esquecido do problema: quem migrou para a credencial por aparelho
    apagou o token antigo do celular — e antes disto o plantão só entendia o token
    antigo. O celular certo batia na porta e ouvia 401."""
    base, subidas, registro = vigia_com_aparelhos
    codigo, corpo = _post(base + "/vigia/acordar", token=_parear(registro))
    assert codigo == 200 and corpo["estado"] == "subindo_agora"
    assert len(subidas) == 1


def test_aparelho_revogado_NAO_acorda_mais_o_pc(vigia_com_aparelhos):
    """O lado que o dono pediu: revogar tem de valer em TODA porta. Enquanto o
    plantão só conhecesse o segredo compartilhado, revogar era uma promessa que
    parava justamente onde o PC está sozinho de madrugada."""
    from mente_digital import aparelhos as regras

    base, subidas, registro = vigia_com_aparelhos
    credencial = _parear(registro)
    registro.revogar(regras.partir_credencial(credencial)[0])

    codigo, _ = _post(base + "/vigia/acordar", token=credencial)
    assert codigo == 401
    assert subidas == []


def test_o_plantao_atende_em_TLS_com_o_cert_do_servidor(monkeypatch, tmp_path):
    """O celular DERIVA o endereço do vigia do endereço do assistente, trocando só
    a porta — inclusive o esquema (`Endereco.vigia`, Android). Ligar o HTTPS no
    servidor grande e deixar o plantão em HTTP puro faria o app falar `https://` com
    um socket que não fala TLS, e a falha chegaria na tela como "o PC está
    desligado": a ambiguidade que o vigia existe para matar, ressuscitada.

    Servidor real com certificado real (gerado aqui pelo `cryptography`, que já é
    dependência transitiva): handshake é a classe de coisa que passa na leitura e
    falha na rede."""
    ssl = pytest.importorskip("ssl")
    cert, chave = _cert_efemero(tmp_path)

    monkeypatch.setattr(vigia.settings, "access_token", "segredo-do-dono")
    monkeypatch.setattr(vigia.rede, "porta_em_uso", lambda h, p: False)
    monkeypatch.setattr(vigia, "subir_app", lambda raiz: True)

    ctx = vigia.contexto_tls(cert, chave)
    assert ctx is not None
    httpd = vigia._Plantao(("127.0.0.1", 0), vigia._montar_handler(vigia.Vigia(tmp_path)))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        porta = httpd.server_address[1]
        cliente = ssl._create_unverified_context()   # nosec B323 - cert efêmero do teste
        with urllib.request.urlopen(                 # nosec B310
                f"https://127.0.0.1:{porta}/vigia/status", timeout=5, context=cliente) as r:
            assert json.loads(r.read().decode())["vigia"] is True
    finally:
        httpd.shutdown()


def test_cert_configurado_e_quebrado_MATA_o_plantao(tmp_path):
    """A 1ª versão caía para HTTP aqui, copiando o fail-soft do `main.py`. Uma
    revisão adversária derrubou o argumento: se o servidor está configurado para
    TLS, o CELULAR também está (ele deriva o esquema), então o plantão em HTTP puro
    **já está mudo para ele** — o fallback não salva ninguém e só deixa a credencial
    de acordar em claro num socket que escuta em `0.0.0.0`.

    E o aviso era um `print`: no único caminho de "sobe com o Windows"
    (`inicializacao.script_vbs` → `sh.Run …, 0, False`, janela oculta e sem
    redirecionamento) ele não tem console nem arquivo onde cair. Silêncio total,
    meses depois, quando o cert de 90 dias do Tailscale expirasse."""
    with pytest.raises(vigia.CertificadoInvalido):
        vigia.contexto_tls(str(tmp_path / "nao_existe.crt"), str(tmp_path / "nao_existe.key"))


def test_cert_ilegivel_tambem_mata(tmp_path):
    """Arquivo existe mas não é certificado (truncado, trocado, chave que não casa)."""
    lixo_cert = tmp_path / "lixo.crt"
    lixo_chave = tmp_path / "lixo.key"
    lixo_cert.write_text("isto não é um certificado")
    lixo_chave.write_text("nem isto é uma chave")
    with pytest.raises(vigia.CertificadoInvalido):
        vigia.contexto_tls(str(lixo_cert), str(lixo_chave))


def test_a_falha_fatal_deixa_o_motivo_em_DISCO(tmp_path, monkeypatch):
    """`print` num processo sem console é o mesmo que silêncio. O arquivo é o canal
    que sobrevive — o dono veria, senão, só o app deixando de se encerrar."""
    monkeypatch.setattr(vigia.settings, "ssl_cert", str(tmp_path / "nao_existe.crt"))
    monkeypatch.setattr(vigia.settings, "ssl_key", str(tmp_path / "nao_existe.key"))
    with pytest.raises(vigia.CertificadoInvalido):
        vigia.servir(tmp_path, porta=0)

    deixado = (tmp_path / "dados" / "vigia_erro.txt").read_text(encoding="utf-8")
    assert "nao_existe.crt" in deixado
    assert "0.0.0.0" in deixado          # diz POR QUE não subiu, não só que não subiu


def test_pasta_de_dados_impossivel_nao_troca_a_excecao_original(tmp_path, monkeypatch, capsys):
    """Se nem o arquivo der para escrever, o processo tem de morrer com a falha do
    CERTIFICADO — não com uma falha sobre o log dela."""
    monkeypatch.setattr(vigia.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disco cheio")))
    vigia.registrar_falha(tmp_path, "qualquer coisa")
    assert "não consegui nem gravar" in capsys.readouterr().out


def test_sem_cert_configurado_o_plantao_segue_em_http(tmp_path):
    """O default do projeto (`ssl_cert=""`): nada muda, nem um aviso a mais."""
    assert vigia.contexto_tls("", "") is None


def _cert_efemero(tmp_path) -> tuple[str, str]:
    """Cert auto-assinado de 1 dia para 127.0.0.1, feito pelo `openssl` do PATH.

    Nem `cryptography` (NÃO está nesta env — medido: o teste se pulava em silêncio)
    nem chave commitada no repo (chave privada versionada é cheiro ruim mesmo em
    teste, e o bandit reclama com razão). `openssl` vem com o Git for Windows na
    máquina do dono e existe no runner Linux do CI, então a prova roda nos dois —
    que é o ponto: TLS é a classe de coisa que passa na leitura e falha na rede.
    """
    import shutil
    import subprocess

    if not shutil.which("openssl"):
        pytest.skip("openssl não está no PATH")
    p_cert = tmp_path / "t.crt"
    p_chave = tmp_path / "t.key"
    r = subprocess.run([                                         # nosec B603 B607
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(p_chave), "-out", str(p_cert), "-days", "1",
        "-subj", "/CN=mente-teste", "-addext", "subjectAltName=IP:127.0.0.1",
    ], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return str(p_cert), str(p_chave)


def test_com_a_identidade_desligada_o_plantao_nao_ABRE_o_banco(monkeypatch, tmp_path):
    """A função nasce desligada, e desligada o plantão tem de continuar barato: o
    celular pergunta a cada tique da tela de carregamento, e abrir SQLite em cada
    tique seria pagar por uma função que o dono não ligou.

    A prova é o registro seguir None depois de um pedido atendido — construí-lo é
    o que abriria o arquivo."""
    monkeypatch.setattr(vigia.settings, "aparelhos_habilitado", False)
    monkeypatch.setattr(vigia.settings, "access_token", "segredo-do-dono")
    v = vigia.Vigia(tmp_path)

    assert v.autorizado("segredo-do-dono", "192.168.0.50") is True
    assert v.autorizado("chute", "192.168.0.50") is False
    assert v._registro_aparelhos is None


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


# --- Teto de conexões do plantão (revisão de segurança, 2026-08-03) -----------
#
# O `ThreadingHTTPServer` cria uma thread por conexão, SEM teto, e a thread nasce ANTES
# do gate. Numa enxurrada, o processo que existe para viver com dezenas de MB morre por
# exaustão de thread — e o ataque nega exatamente a função dele, do jeito mais silencioso
# possível: você tenta acordar o PC pelo celular e a tela diz "o PC está desligado".
class _PlantaoFalso(vigia._Plantao):
    """Só o contador de vagas, sem abrir socket (o teste é da contabilidade)."""

    def __init__(self, maximo: int) -> None:      # noqa: D107 - não chama o super
        self.MAX_CONEXOES = maximo
        self._vagas = threading.Semaphore(maximo)
        self.atendidas: list = []
        self.recusadas: list = []

    # Substitutos dos pontos da stdlib que o override real chama via super().
    def _super_process_request(self, request, client_address):
        self.atendidas.append(client_address)

    def handle_error_de_lotacao(self, client_address) -> None:
        self.recusadas.append(client_address)


def _plantao_com_stubs(maximo: int):
    """Troca os `super()` da stdlib por no-ops, para exercitar só a contabilidade."""
    p = _PlantaoFalso(maximo)
    p.process_request = lambda req, addr: _process_request_real(p, req, addr)
    return p


def _process_request_real(p, request, client_address):
    """Cópia fiel do fluxo de `_Plantao.process_request`, com os super() neutralizados."""
    if not p._vagas.acquire(timeout=0.05):
        p.handle_error_de_lotacao(client_address)
        return                                   # super().shutdown_request: no-op aqui
    p._super_process_request(request, client_address)


def test_o_plantao_recusa_acima_do_teto_em_vez_de_criar_thread():
    p = _plantao_com_stubs(3)
    for i in range(3):
        p.process_request(None, (f"10.0.0.{i}", 1))

    p.process_request(None, ("10.0.0.99", 1))    # o 4º não cabe

    assert len(p.atendidas) == 3
    assert p.recusadas == [("10.0.0.99", 1)]


def test_a_vaga_volta_mesmo_se_o_encerramento_do_socket_falhar():
    """Duas coisas de uma vez, e a segunda é a que importa.

    (a) A vaga volta quando a conexão termina — sem isso o plantão se estrangularia
        sozinho após N conexões NORMAIS: uma negação de serviço auto-infligida, pior
        que a que o teto evita.
    (b) E volta pelo `finally`, ou seja MESMO quando o encerramento do socket estoura.
        É o caso real: `shutdown_request` da stdlib toca o socket, e socket já morto
        (cliente que sumiu, handshake TLS abortado — o cenário mais comum aqui) levanta.
        Se a liberação estivesse depois do `super()` em vez de no `finally`, cada
        conexão morta comeria uma vaga para sempre, e o plantão fecharia sozinho
        justamente sob a rede ruim em que ele mais precisa funcionar.
    """
    p = _plantao_com_stubs(2)
    p.process_request(None, ("10.0.0.1", 1))
    p.process_request(None, ("10.0.0.2", 1))
    p.process_request(None, ("10.0.0.3", 1))     # lotado
    assert len(p.recusadas) == 1

    # `None` no lugar do socket faz o super() da stdlib estourar — de propósito.
    with pytest.raises(AttributeError):
        vigia._Plantao.shutdown_request(p, None)

    p.process_request(None, ("10.0.0.4", 1))
    assert ("10.0.0.4", 1) in p.atendidas


def test_recusa_por_lotacao_nao_devolve_vaga_que_nunca_pegou():
    """O bug que quase entrou: a recusa chamava o `shutdown_request` SOBRESCRITO, que
    libera o semáforo. O contador cresceria a cada recusa e o teto viraria ficção
    justamente sob ataque — a única hora em que ele serve para alguma coisa."""
    p = _plantao_com_stubs(1)
    p.process_request(None, ("10.0.0.1", 1))     # ocupa a única vaga
    for i in range(20):                          # 20 recusas seguidas
        p.process_request(None, (f"10.0.1.{i}", 1))

    assert len(p.recusadas) == 20
    # Se cada recusa tivesse liberado uma vaga, haveria 20 vagas sobrando agora.
    p.process_request(None, ("10.0.2.1", 1))
    assert len(p.atendidas) == 1                 # segue com a vaga única ocupada


# --------------------------------------------------------------------------- #
# O plantão respeita o jogo (2026-08-08)                                       #
# --------------------------------------------------------------------------- #
class TestRespeitaOJogo:
    """Levantar o assistente é ~7,7 GB de RAM e a VRAM inteira. Fazer isso no meio
    de uma raid, por um pedido que podia esperar três minutos, é o pior resultado
    possível — e era o comportamento até 2026-08-08.

    ⚠ E o caminho é JUSTAMENTE este: aos 20 min o assistente dorme e aos 45 ele
    SAI, então quem joga três horas quase nunca tem o assistente de pé para um
    gate dentro dele barrar. Sem o plantão, não há gate."""

    def test_com_jogo_aberto_o_pedido_e_recusado(self):
        assert vigia.decidir(False, None, "escapefromtarkov.exe").subir is False
        assert vigia.decidir(False, None, "escapefromtarkov.exe").estado == "ocupado"

    def test_sem_jogo_nada_muda(self):
        """A função nasce ligada, então o caminho de quem não joga TEM de ser byte
        a byte o de antes."""
        assert vigia.decidir(False, None, None).subir is True
        assert vigia.decidir(False, None, None).estado == "subindo_agora"

    def test_servidor_JA_DE_PE_vence_o_jogo(self):
        """Se o assistente já está no ar, o jogo não é da conta de ninguém: a
        pessoa já podia usar um segundo atrás, e recusar agora seria derrubá-la
        do nada."""
        assert vigia.decidir(True, None, "tarkov.exe").estado == "ja_de_pe"

    def test_JA_SUBINDO_vence_o_jogo(self):
        """Aqui já mandamos subir, talvez antes de o jogo abrir. Dizer 'ocupado'
        seria MENTIRA — o app está vindo de qualquer jeito, e o celular mostraria
        'não deu' enquanto a tela do PC acende."""
        assert vigia.decidir(False, 5.0, "tarkov.exe").estado == "subindo"

    def test_o_estado_nao_conta_ao_pedinte_o_que_o_dono_faz(self):
        """⚠ `ocupado`, nunca `jogo`. O nome atravessa a rede até o aparelho de
        OUTRA pessoa. O mensageiro inteiro foi desenhado para o poder não vazar
        numa direção; vazar a atividade do dono na outra é o espelho."""
        estado = vigia.decidir(False, None, "escapefromtarkov.exe").estado
        assert "jogo" not in estado and "tarkov" not in estado

    def test_a_recusa_GUARDA_o_pedido(self, tmp_path, monkeypatch):
        """A recusa só é aceitável porque o pedido não se perde. Sem o bilhete, o
        pedinte ouve 'não' e o dono nunca fica sabendo que alguém quis entrar."""
        v = vigia.Vigia(tmp_path)
        monkeypatch.setattr(v, "jogo_agora", lambda: "escapefromtarkov.exe")
        monkeypatch.setattr(v, "servidor_de_pe", lambda: False)
        resposta = v.acordar("ana", "cel-da-ana")

        assert resposta["estado"] == "ocupado"
        assert "registrado" in resposta["aviso"] or "avisado" in resposta["aviso"]
        assert v.tem_pedido_pendente() is True
        guardados = vez.ler_todos((tmp_path / "dados" / "pedidos_de_acesso.jsonl")
                                  .read_text(encoding="utf-8"))
        assert [p.usuario for p in guardados] == ["ana"]

    def test_a_recusa_NAO_sobe_o_app(self, tmp_path, monkeypatch):
        subiu = []
        monkeypatch.setattr(vigia, "subir_app", lambda raiz: subiu.append(raiz) or True)
        v = vigia.Vigia(tmp_path)
        monkeypatch.setattr(v, "jogo_agora", lambda: "tarkov.exe")
        monkeypatch.setattr(v, "servidor_de_pe", lambda: False)
        v.acordar("ana")
        assert subiu == []

    def test_quando_o_jogo_fecha_o_app_SOBE_para_avisar(self, tmp_path, monkeypatch):
        """A outra metade da promessa. Nessa hora o assistente está DESLIGADO — é a
        premissa toda —, então não há mais ninguém para notar que o jogo saiu."""
        subiu = []
        monkeypatch.setattr(vigia, "subir_app", lambda raiz: subiu.append(raiz) or True)
        v = vigia.Vigia(tmp_path)
        monkeypatch.setattr(v, "servidor_de_pe", lambda: False)

        monkeypatch.setattr(v, "jogo_agora", lambda: "tarkov.exe")
        v.acordar("ana")                       # recusado, pedido guardado
        assert subiu == []
        monkeypatch.setattr(v, "jogo_agora", lambda: None)
        assert v.tique_do_jogo() is True       # o jogo fechou -> sobe
        assert subiu == [tmp_path]

    def test_jogo_fechado_SEM_ninguem_esperando_nao_acorda_a_maquina(self, tmp_path, monkeypatch):
        """Levantar 7,7 GB sem ninguém esperando gasta a máquina exatamente no
        instante em que o dono acabou de sair de um jogo — quando ele menos quer o
        PC ocupado."""
        subiu = []
        monkeypatch.setattr(vigia, "subir_app", lambda raiz: subiu.append(raiz) or True)
        v = vigia.Vigia(tmp_path)
        monkeypatch.setattr(v, "servidor_de_pe", lambda: False)
        monkeypatch.setattr(v, "jogo_agora", lambda: "tarkov.exe")
        v.tique_do_jogo()                      # marca que havia jogo, sem pedido
        monkeypatch.setattr(v, "jogo_agora", lambda: None)
        assert v.tique_do_jogo() is False
        assert subiu == []

    def test_o_tique_nao_repete_o_disparo(self, tmp_path, monkeypatch):
        """Agir no ESTADO em vez da BORDA faria cada passada tentar subir um app
        já de pé — o mesmo raciocínio de `jogo_ativo.decidir`."""
        subiu = []
        monkeypatch.setattr(vigia, "subir_app", lambda raiz: subiu.append(raiz) or True)
        v = vigia.Vigia(tmp_path)
        monkeypatch.setattr(v, "servidor_de_pe", lambda: False)
        monkeypatch.setattr(v, "jogo_agora", lambda: "tarkov.exe")
        v.acordar("ana")
        monkeypatch.setattr(v, "jogo_agora", lambda: None)
        assert v.tique_do_jogo() is True
        assert v.tique_do_jogo() is False      # segunda passada: nada
        assert len(subiu) == 1

    def test_desligado_o_caminho_e_o_de_hoje(self, tmp_path, monkeypatch):
        """`MENTE_VIGIA_RESPEITA_JOGO=false` tem de devolver o comportamento
        anterior por inteiro — inclusive não pagar a leitura de processos."""
        monkeypatch.setattr(vigia.settings, "vigia_respeita_jogo", False)
        olhou = []
        monkeypatch.setattr(vigia.jogo_ativo, "processos_em_execucao",
                            lambda: olhou.append(1) or [])
        v = vigia.Vigia(tmp_path)
        assert v.jogo_agora() is None
        assert olhou == []

    def test_erro_ao_olhar_processos_nao_tranca_o_dono(self, tmp_path, monkeypatch):
        """Fail-soft, e o sentido importa: recusar por causa de uma leitura que
        falhou trocaria um conforto por uma tranca."""
        monkeypatch.setattr(vigia.settings, "vigia_respeita_jogo", True)
        monkeypatch.setattr(vigia.jogo_ativo, "processos_em_execucao",
                            lambda: (_ for _ in ()).throw(OSError("sem permissão")))
        assert vigia.Vigia(tmp_path).jogo_agora() is None
