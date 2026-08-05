"""O convite — levar alguém do zero ao assistente sem ele travar no meio.

O que estes testes protegem é ORDEM e HONESTIDADE, não formatação: um convite que
omite um passo deixa a pessoa parada numa tela que não explica nada, e quem paga é o
dono, por telefone.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def _carregar():
    caminho = RAIZ / "scripts" / "gerar_convite.py"
    spec = importlib.util.spec_from_file_location("gerar_convite", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gc = _carregar()


def _html(**kw):
    base = dict(apelido="celular da ana", usuario="ana", codigo="ABC-123",
                endereco="https://maquina.tail1.ts.net:8000", validade_min=10.0)
    base.update(kw)
    return gc.montar_html(**base)


def test_traz_os_tres_passos_que_a_pessoa_nao_adivinha():
    """Tailscale, o COMPARTILHAMENTO da máquina e o app. O do meio é o que some das
    instruções e trava todo mundo: a conta na rede não basta — o dono precisa
    compartilhar a máquina, senão o Tailscale conecta e o assistente não aparece."""
    pagina = _html()

    assert "Tailscale" in pagina
    assert "compartilh" in pagina.lower(), "faltou o passo do compartilhamento"
    assert "apk" in pagina.lower() or "aplicativo" in pagina.lower()


def test_diz_que_a_ORDEM_importa():
    """Sem isso a pessoa instala o app primeiro, ele diz "computador desligado", e
    ela conclui que está quebrado — quando só falta a rede."""
    assert "ordem" in _html().lower()


def test_avisa_do_prazo_e_do_uso_unico():
    """Código expirado sem aviso vira "não funcionou". Com aviso, vira "pede outro"."""
    pagina = _html(validade_min=10.0)
    assert "10" in pagina
    assert "uma vez" in pagina.lower() or "uso único" in pagina.lower()


def test_avisa_da_MIUI():
    """Fato do ambiente medido: a MIUI mata o Tailscale em segundo plano e ele segue
    exibindo "Connected" com processo nenhum rodando. Quem não souber disso vai achar
    que o assistente é instável."""
    pagina = _html()
    assert "MIUI" in pagina
    assert "utostart" in pagina


def test_sem_TLS_admite_em_vez_de_inventar_endereco():
    """Sem cert não há endereço alcançável de fora. Imprimir um palpite (o IP da LAN,
    por exemplo) mandaria a pessoa para um endereço que o TLS rejeita por nome."""
    pagina = _html(endereco="")

    assert "peça o endereço" in pagina.lower()
    assert "https://" not in pagina.split("Aponte o app")[1][:400]


def test_escapa_o_que_vem_de_fora():
    """O apelido é digitado por quem convida e vai parar em HTML."""
    pagina = _html(apelido='<script>alert(1)</script>')

    assert "<script>alert(1)</script>" not in pagina
    assert "&lt;script&gt;" in pagina


def test_o_codigo_aparece_uma_vez_e_visivel():
    pagina = _html(codigo="XYZ-789")
    assert pagina.count("XYZ-789") == 1


def test_endereco_sai_do_CERTIFICADO_e_nao_de_config_solta(monkeypatch):
    """O nome tem de ser o MESMO para o qual o cert foi emitido — derivá-lo do
    arquivo faz os dois baterem por construção. Um campo separado poderia divergir,
    e a divergência apareceria como "conexão falhou", sem dizer por quê."""
    # ⚠ O caminho é montado com `Path`, e não cravado como `r"C:\certs\..."`. A
    # primeira versão cravou, e quebrou SÓ no CI: no runner POSIX o `Path.stem` não
    # separa por barra invertida, então o "nome do arquivo" virava a string inteira
    # e o endereço saía `https://C:\certs\maquina...`. Verde no Windows o tempo todo.
    # É a terceira vez que esta família morde este projeto (`os.name`→`pathlib`,
    # `ctypes.wintypes`): teste que crava convenção de plataforma testa a plataforma,
    # não o código.
    cert = Path("certs") / "maquina.tail9.ts.net.crt"
    monkeypatch.setattr(gc.settings, "ssl_cert", str(cert))
    monkeypatch.setattr(gc.settings, "port", 8000)

    assert gc.endereco_do_assistente() == "https://maquina.tail9.ts.net:8000"


def test_sem_cert_o_endereco_e_vazio_e_nao_um_chute(monkeypatch):
    monkeypatch.setattr(gc.settings, "ssl_cert", "")
    assert gc.endereco_do_assistente() == ""
