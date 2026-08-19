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


def test_o_app_NAO_e_oferecido_por_link_publico():
    """O dono publicou o APK numa release e RETIROU: o repositório é público, e
    publicar ali deixa o app que fala com o assistente dele baixável por qualquer
    pessoa da internet. O convite não pode reintroduzir isso por um link.

    E um link para release apagada seria pior que nenhum: `/releases/latest` passa a
    responder **HTTP 200** apontando para uma página VAZIA — a pessoa vê "deu certo"
    e não tem arquivo nenhum. Medido ao apagar a release em 2026-08-05."""
    pagina = _html()

    assert "releases" not in pagina, "o convite voltou a apontar para uma release"
    assert "github.com" not in pagina
    # O caminho certo é o arquivo que viaja junto pelo WhatsApp.
    assert "veio junto com este convite" in pagina


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


def test_manda_deixar_o_campo_de_TOKEN_vazio():
    """⚑ Medido em 2026-08-19, com uma pessoa real parada na tela: o convite ensinava
    "informe o endereço" e "cole o código", e a tela de configuração tem TRÊS campos —
    o do meio, "Token de acesso", parece obrigatório e não é. Quem chega ali sem aviso
    pede um token a quem convidou, e o dono não tem um para dar: a única coisa que
    caberia ali é o `MENTE_ACCESS_TOKEN`, que faz a pessoa entrar COMO O DONO, lendo a
    memória dele. O convite tinha de dizer "deixe vazio" e não dizia."""
    pagina = _html()

    assert "Token de acesso" in pagina
    assert "vazio" in pagina.lower()
    assert "sozinho" in pagina, "tem de explicar que o app preenche o token ele mesmo"


def test_nomeia_o_BOTAO_que_a_pessoa_precisa_tocar():
    """"Cole o código" não basta: colar não pareia. O que pareia é o botão — e ele fica
    numa seção própria, abaixo do campo de token, que a pessoa não vê se não rolar."""
    pagina = _html()

    assert "Parear este aparelho" in pagina
    assert "Pareamento por código" in pagina


def test_explica_o_botao_APAGADO_em_vez_de_deixar_a_pessoa_travada():
    """O botão só habilita com endereço preenchido (`base.isNotBlank()` no TelaConfig).
    Sem essa frase, o sintoma é "o app não deixa eu parear" e a causa está dois passos
    acima, invisível."""
    assert "apagado" in _html().lower()


def test_avisa_que_o_IP_esmaecido_do_campo_NAO_e_um_valor():
    """O campo traz `192.168.0.10:8000` como placeholder. Desde o TLS esse endereço não
    serve (o cert cobre o nome do Tailscale e o IP é rejeitado por nome), e um exemplo
    apagado dentro de um campo vazio é lido como "já está preenchido"."""
    pagina = _html()

    assert "192.168.0.10:8000" in pagina
    assert "exemplo" in pagina.lower()


def test_sem_TLS_nao_ensina_o_formato_de_um_endereco_que_nao_existe():
    """Gêmeo do teste abaixo: sem cert o convite admite que não sabe. Falar de
    `https://` ali seria ensinar o formato de um valor que ele não tem — o palpite
    disfarçado de instrução."""
    pagina = _html(endereco="")

    assert "https://" not in pagina.split("Aponte o app")[1][:600]
    assert "peça o endereço" in pagina.lower()


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
