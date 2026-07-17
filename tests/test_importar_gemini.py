"""
Importador do histórico do Gemini (scripts/importar_gemini.py) — partes puras.

O que está em jogo: o import ANTIGO produziu 7.268 notas `_Pt<N>_` com títulos sem
contexto ("# Economia necessária", "# Gemini"), e como o título é indexado junto com
o corpo (split_markdown, strip_headers=False) isso envenena a recuperação. Este
importador regenera da fonte. Aqui travamos o que é testável sem GPU: a extração dos
turnos, o janelamento por fronteira de turno, e o tema vindo do nome do arquivo.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from importar_gemini import (  # noqa: E402
    Deduplicador, garantir_assunto, janelar, ler_turnos, tema_do_arquivo,
)


# --- Deduplicador: uma ideia, uma nota ---------------------------------------
class FakeEmb:
    """Embedding falso e determinístico: o vetor é o conjunto de palavras do texto.

    Dois textos com as mesmas palavras -> cosseno 1.0 (clone). Sem palavra em comum
    -> 0.0. Chega para exercitar a LÓGICA de dedup sem carregar o modelo real.
    """

    _VOCAB = ["tensorrt", "yolo", "acelera", "bolo", "cenoura", "forno", "gpu"]

    def embed_documents(self, textos):
        out = []
        for t in textos:
            low = t.lower()
            v = [1.0 if w in low else 0.0 for w in self._VOCAB]
            out.append(v if any(v) else [1.0] + [0.0] * (len(self._VOCAB) - 1))
        return out


def _dedup(limiar=0.95):
    return Deduplicador(FakeEmb(), limiar)


def test_dedup_funde_clone_exato():
    # O caso REAL: 37 átomos com o título "Race Condition de Threads: Arquitetura de
    # Interface" numa parcial de 495 — a conversa discute a ideia em vários trechos e
    # cada janela a re-atomiza.
    d = _dedup()
    assert d.filtrar(["TensorRT acelera o YOLO"]) == [True]
    assert d.filtrar(["TensorRT acelera o YOLO"]) == [False]   # clone -> fundido
    assert d.fundidos == 1


def test_dedup_mantem_ideias_distintas():
    # O contra-teste: o limiar não pode engolir conhecimento diferente.
    d = _dedup()
    assert d.filtrar(["TensorRT acelera o YOLO"]) == [True]
    assert d.filtrar(["bolo de cenoura no forno"]) == [True]
    assert d.fundidos == 0


def test_dedup_pega_clone_dentro_da_mesma_janela():
    # Uma janela sozinha pode repetir a ideia: o filtro compara contra os anteriores
    # da própria chamada, não só contra o histórico.
    d = _dedup()
    assert d.filtrar(["TensorRT acelera o YOLO", "TensorRT acelera o YOLO"]) == [True, False]


def test_dedup_semear_faz_o_resume_nao_reintroduzir_duplicata():
    # Sem semear, matar e reiniciar o lote traria de volta tudo que já existia.
    d = _dedup()
    d.semear(["TensorRT acelera o YOLO"])          # já estava em disco
    assert d.filtrar(["TensorRT acelera o YOLO"]) == [False]


def test_dedup_lista_vazia_e_noop():
    d = _dedup()
    assert d.filtrar([]) == []
    d.semear([])                                    # não explode sem átomos prévios


# --- garantir_assunto: a trava determinística --------------------------------
def test_garantir_assunto_prefixa_titulo_generico():
    # O caso REAL medido: o modelo largou a regra e escreveu "## Pressão de Oferta"
    # sobre a economia de um jogo NFT. Sem assunto no título, o átomo é recuperado por
    # qualquer pergunta que use a palavra "oferta" — de qualquer domínio.
    bloco = "## Pressão de Oferta\nNo fundo, a oferta é alta, com excesso de itens."
    out = garantir_assunto(bloco, "Economia do jogo NFT Bomb Crypto")
    assert out.startswith("## Economia do jogo NFT Bomb Crypto: Pressão de Oferta")


def test_garantir_assunto_nao_mexe_quando_o_modelo_ja_acertou():
    # Idempotência prática: se o título já nomeia o assunto, não empilha prefixo.
    bloco = "## Economia de munição necessária no Tarkov\nPrecisa economizar 166,2."
    out = garantir_assunto(bloco, "Munição do Escape from Tarkov")
    assert out == bloco                       # 'municao'/'tarkov' em comum -> intacto


def test_garantir_assunto_e_idempotente():
    bloco = "## Pressão de Oferta\ncorpo"
    once = garantir_assunto(bloco, "Economia do Bomb Crypto")
    assert garantir_assunto(once, "Economia do Bomb Crypto") == once   # não prefixa 2x


def test_garantir_assunto_ignora_stopwords_na_comparacao():
    # 'de'/'do' não podem contar como sobreposição — senão quase todo título passaria.
    bloco = "## Cálculo de liquidez total\ncorpo"
    out = garantir_assunto(bloco, "Economia do Bomb Crypto")
    assert out.startswith("## Economia do Bomb Crypto: Cálculo")


def test_garantir_assunto_sem_assunto_util_e_noop():
    bloco = "## Título\ncorpo"
    assert garantir_assunto(bloco, "   ") == bloco


def _json(tmp_path, nome, msgs):
    p = tmp_path / nome
    p.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_tema_vem_do_nome_do_arquivo():
    # O nome é a ÚNICA fonte confiável do assunto — e é o que o import antigo jogou
    # fora, produzindo "# Economia necessária" sem dizer que era Tarkov.
    assert tema_do_arquivo("x/Otimizando-Munição-no-Tarkov-A.json") == "Otimizando Munição no Tarkov A"


def test_ler_turnos_descarta_o_thinking(tmp_path):
    # `thinking` é o rascunho interno do Gemini (0,7M de chars no acervo): não é o que
    # ele respondeu. Atomizar raciocínio abandonado plantaria contradição na base.
    p = _json(tmp_path, "c.json", [
        {"role": "user", "contents": [{"type": "text", "content": "quanto é 2+2?"}]},
        {"role": "assistant", "contents": [
            {"type": "thinking", "content": "deixa eu somar... talvez 5?"},
            {"type": "text", "content": "São 4."},
        ]},
    ])
    turnos = ler_turnos(p)
    assert turnos == [("Usuário", "quanto é 2+2?"), ("Assistente", "São 4.")]


def test_ler_turnos_ignora_blocos_nao_textuais(tmp_path):
    p = _json(tmp_path, "c.json", [
        {"role": "user", "contents": [{"type": "image", "content": "base64..."},
                                      {"type": "text", "content": "o que é isto?"}]},
        {"role": "assistant", "contents": [{"type": "html_widget", "content": "<div/>"}]},
    ])
    assert ler_turnos(p) == [("Usuário", "o que é isto?")]   # a msg só-widget some


def test_janelar_corta_so_em_fronteira_de_turno():
    # Cortar no meio de uma frase daria ao LLM um trecho sem sentido — e trecho sem
    # sentido é exatamente como nasce um átomo com título genérico.
    turnos = [("Usuário", "a" * 100), ("Assistente", "b" * 100), ("Usuário", "c" * 100)]
    janelas = janelar(turnos, limite=250)
    assert len(janelas) == 2
    for j in janelas:                       # nenhuma janela quebra um bloco ao meio
        for bloco in j.split("\n\n"):
            assert bloco.startswith("**Usuário:**") or bloco.startswith("**Assistente:**")


def test_janelar_turno_gigante_vira_janela_sozinho():
    # Não pode sumir com o turno nem misturá-lo com outros: vai sozinho.
    turnos = [("Usuário", "x" * 500), ("Assistente", "ok")]
    janelas = janelar(turnos, limite=100)
    assert len(janelas) == 2
    assert "x" * 500 in janelas[0]


def test_janelar_preserva_todo_o_conteudo():
    turnos = [("Usuário", f"pergunta {i}") for i in range(20)]
    janelas = janelar(turnos, limite=60)
    juntas = "\n\n".join(janelas)
    for i in range(20):                     # nada se perde no janelamento
        assert f"pergunta {i}" in juntas


def test_janelar_vazio():
    assert janelar([]) == []
