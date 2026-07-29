"""GRAVAÇÃO DO TURNO: o que o app fez e o que o usuário VIU.

Pedido do dono em 2026-07-29. O log de telemetria contava o comportamento e o
SQLite guardava a resposta em texto, mas nenhum dos dois dizia QUAL imagem
apareceu na tela nem ONDE — que era exatamente a dúvida dele (imagens sem
relação com a pergunta, e a mesma sequência se repetindo entre perguntas).
"""
import json

from mente_digital.transcricao import GravadorTurno, imagens_em, montar_registro

EMBED = "![[Figuras/livro/livro_p0122_f1.webp]]"
OUTRO = "![[Figuras/livro/livro_p0311_f2.webp]]"


# --- a parte pura ------------------------------------------------------------
def test_imagens_saem_com_nome_e_posicao():
    texto = f"O solo argiloso retém água.\n{EMBED}\nOutra frase."

    assert imagens_em(texto) == [
        {"arquivo": "livro_p0122_f1", "char": texto.index(EMBED)},
    ]


def test_imagens_em_ordem_de_aparicao():
    """A ORDEM é o que revela a 'mesma sequência de imagens' repetida entre
    perguntas diferentes — a contagem sozinha esconde isso."""
    texto = f"a {EMBED} b {OUTRO} c"

    assert [i["arquivo"] for i in imagens_em(texto)] == [
        "livro_p0122_f1", "livro_p0311_f2"]


def test_texto_sem_imagem_nao_inventa():
    assert imagens_em("resposta comum, sem figura") == []
    assert imagens_em("") == []


def test_registro_ve_so_o_que_foi_para_a_TELA():
    """`audio` e `status` não são o texto lido: a resposta é a concatenação dos
    tokens, exatamente como ficou na tela."""
    eventos = [
        {"tipo": "status", "texto": "pensando"},
        {"tipo": "token", "texto": "O pH ideal "},
        {"tipo": "audio", "bytes": 4096},
        {"tipo": "token", "texto": "fica entre 6,5 e 7,0."},
    ]

    reg = montar_registro("qual o pH?", "conv1", eventos, "2026-07-29T15:00:00")

    assert reg["resposta"] == "O pH ideal fica entre 6,5 e 7,0."
    assert reg["chars"] == len(reg["resposta"])
    assert reg["pergunta"] == "qual o pH?"
    assert reg["conversa_id"] == "conv1"


# --- o gravador --------------------------------------------------------------
def test_push_fora_de_turno_nao_e_gravado(tmp_path):
    """O scheduler faz push proativo (alarme/briefing) sem pergunta nenhuma —
    aquilo não é resposta de turno e não pode virar um registro órfão."""
    g = GravadorTurno(tmp_path / "turnos.jsonl")

    g.ver({"tipo": "proativo", "texto": "🔔 seu lembrete"})

    assert g.fechar() is None
    assert not (tmp_path / "turnos.jsonl").exists()


def test_audio_entra_como_TAMANHO_nunca_o_base64(tmp_path):
    """O base64 de um turno passa de 1 MB e não diagnostica nada — guardá-lo
    tornaria o arquivo ilegível e inflaria o disco por nada."""
    g = GravadorTurno(tmp_path / "turnos.jsonl")
    g.abrir("pergunta", "conv1")

    g.ver({"tipo": "audio", "base64": "A" * 5000})
    reg = g.fechar()

    assert reg["eventos"] == [{"tipo": "audio", "bytes": 5000}]
    assert "AAAA" not in json.dumps(reg)


def test_turno_completo_grava_a_posicao_da_imagem(tmp_path):
    """O caso que motivou tudo: a imagem caiu no MEIO da resposta ou no rodapé?
    A posição sai do texto final, sem instrumentar o emissor."""
    arquivo = tmp_path / "turnos.jsonl"
    g = GravadorTurno(arquivo)
    g.abrir("Como faço um teste de pH do solo?", "conv1")

    g.ver({"tipo": "token", "texto": "Misture a amostra com água destilada. "})
    g.ver({"tipo": "token", "texto": f"\n{EMBED}\n"})
    g.ver({"tipo": "token", "texto": "Depois use um medidor eletrônico."})
    reg = g.fechar()

    (imagem,) = reg["imagens"]
    assert imagem["arquivo"] == "livro_p0122_f1"
    assert 0 < imagem["char"] < reg["chars"]        # no MEIO, não no rodapé

    gravado = json.loads(arquivo.read_text(encoding="utf-8").strip())
    assert gravado == reg


def test_dois_turnos_viram_duas_linhas(tmp_path):
    arquivo = tmp_path / "turnos.jsonl"
    g = GravadorTurno(arquivo)
    for pergunta in ("primeira", "segunda"):
        g.abrir(pergunta, "conv1")
        g.ver({"tipo": "token", "texto": "resposta"})
        g.fechar()

    linhas = arquivo.read_text(encoding="utf-8").strip().splitlines()

    assert [json.loads(l)["pergunta"] for l in linhas] == ["primeira", "segunda"]


def test_disco_recusado_nao_derruba_o_turno(tmp_path):
    """Gravação é instrumentação: não pode ter poder de quebrar uma resposta.
    (Caminho impossível: um ARQUIVO no lugar da pasta-pai.)"""
    bloqueio = tmp_path / "arquivo"
    bloqueio.write_text("nao sou pasta", encoding="utf-8")
    g = GravadorTurno(bloqueio / "sub" / "turnos.jsonl")
    g.abrir("pergunta", "conv1")
    g.ver({"tipo": "token", "texto": "resposta"})

    assert g.fechar()["resposta"] == "resposta"      # devolveu, mesmo sem gravar


def test_turno_seguinte_nao_herda_eventos_do_anterior(tmp_path):
    g = GravadorTurno(tmp_path / "turnos.jsonl")
    g.abrir("primeira", "conv1")
    g.ver({"tipo": "token", "texto": "resposta longa da primeira"})
    g.fechar()

    g.abrir("segunda", "conv1")
    g.ver({"tipo": "token", "texto": "curta"})

    assert g.fechar()["resposta"] == "curta"
