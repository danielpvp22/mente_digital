"""
Importador do histórico do Gemini (scripts/importar_gemini.py) — partes puras.

O que está em jogo: o import ANTIGO produziu 7.268 notas `_Pt<N>_` com títulos sem
contexto ("# Economia necessária", "# Gemini"), e como o título é indexado junto com
o corpo (split_markdown, strip_headers=False) isso envenena a recuperação. Este
importador regenera da fonte. Aqui travamos o que é testável sem GPU: a extração dos
turnos, o janelamento por fronteira de turno, e o tema vindo do nome do arquivo.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from importar_gemini import (  # noqa: E402
    Deduplicador, Vigia, _nome_atomo, e_fiel, entidades, garantir_assunto, janelar, ler_turnos,
    tema_do_arquivo,
)


# --- _nome_atomo: nunca sobrescrever conhecimento ----------------------------
def test_nome_e_legivel_e_nao_repete_o_assunto():
    # O nome NÃO leva o slug da conversa de propósito: o garantir_assunto já prefixa o
    # assunto no título, e pôr os dois dava
    # 'como_funciona_a_pool_de_pool_de_mineracao_de_zcash_distribuicao_da_tarefa_0_1.md'.
    n = _nome_atomo("Como-Funciona-a-Pool-de-Mineracao-Zcash.json",
                    "## Pool de mineração de Zcash: Distribuição da Tarefa", 0, 1)
    assert n == "pool_de_mineracao_de_zcash_distribuicao_da_tarefa_0_1.md"
    assert "como_funciona" not in n           # a origem vive no frontmatter, não no nome


def test_colisao_entre_conversas_nao_sobrescreve(tmp_path, monkeypatch):
    # O bug REAL e silencioso: dois arquivos com um átomo de título parecido na mesma
    # posição geravam o MESMO nome, e open(w) truncava o anterior. A unicidade é do
    # _caminho_livre — o nome pode repetir, o arquivo não.
    import importar_gemini as m
    monkeypatch.setattr(m, "DIR_SAIDA", str(tmp_path))
    nome = _nome_atomo("a.json", "## Conexão com a Pool\nUsa Stratum.", 0, 0)

    p1 = m._caminho_livre(nome)
    Path(p1).write_text("átomo da conversa A", encoding="utf-8")
    p2 = m._caminho_livre(nome)          # mesma origem de nome, outra conversa

    assert p1 != p2
    assert Path(p1).read_text(encoding="utf-8") == "átomo da conversa A"   # intacto


def test_nome_distingue_janela_e_indice():
    atomo = "## X\ncorpo"
    assert _nome_atomo("c.json", atomo, 0, 0) != _nome_atomo("c.json", atomo, 1, 0)
    assert _nome_atomo("c.json", atomo, 0, 0) != _nome_atomo("c.json", atomo, 0, 1)


def test_nome_preserva_o_que_distingue_os_atomos():
    # Com o prefixo do garantir_assunto, 40 chars cortavam justamente o "MAI AP"/"BP".
    a = _nome_atomo("t.json", "## Tarkov Arena Munições: Custo de 40 balas de MAI AP", 0, 0)
    b = _nome_atomo("t.json", "## Tarkov Arena Munições: Custo de 30 balas de BP", 0, 1)
    assert "mai_ap" in a and "bp" in b
    assert a != b


def test_nome_nao_corta_no_meio_da_palavra():
    n = _nome_atomo("c.json", "## Diferenca entre dolar comercial e turismo internacional", 0, 0)
    miolo = n.split("_0_0.md")[0]
    assert not miolo.endswith("_")
    for pedaco in ("turism", "interna"):          # cortes no meio que víamos antes
        assert not miolo.endswith(pedaco)


def test_nome_cabe_no_windows():
    # MAX_PATH=260 e o caminho até o vault já gasta ~58.
    n = _nome_atomo("C" * 80 + ".json", "## " + "T" * 200, 999, 99)
    assert len(n) < 130


# --- e_fiel: o portão de qualidade -------------------------------------------
def test_fiel_pega_numero_inventado():
    # O caso REAL: a conversa "520 rpm num quadro 700" teve a resposta TRUNCADA na
    # origem, e o modelo inventou "a circunferência da roda de 700c é ~2,1 metros".
    trecho = "**Usuário:** 520 rpm num quadro 700. Quantos km/h dará?"
    atomo = "## Circunferência da roda\nA circunferência da roda de 700c é de aproximadamente 2,1 metros."
    ok, motivo = e_fiel(atomo, trecho)
    assert ok is False
    assert "2.1" in motivo or "21" in motivo


def test_fiel_aceita_numero_que_esta_na_fonte():
    trecho = "uma cadência de 520 RPM é absurdamente alta; profissionais mal chegam a 170 ou 200 RPM"
    atomo = "## Cadência de ciclistas profissionais\nProfissionais de pista chegam a 170 ou 200 RPM."
    assert e_fiel(atomo, trecho)[0] is True


def test_fiel_aceita_atomo_sem_numero():
    # Sem número não há o que verificar: o portão não pode reprovar por omissão.
    assert e_fiel("## Stratum\nO Stratum é o protocolo usado para conectar à pool.", "texto qualquer")[0] is True


def test_fiel_normaliza_virgula_ponto_e_milhar():
    # '2,1' na fonte e '2.1' no átomo são o mesmo número; '1.200' e '1200' também.
    assert e_fiel("## X\nO valor é 2.1 metros.", "o valor é 2,1 metros")[0] is True
    assert e_fiel("## X\nCusta 1200 reais.", "custa 1.200 reais")[0] is True


def test_fiel_ignora_digito_solto_de_proposito():
    # Limite ASSUMIDO: '1' e '7' aparecem em qualquer texto (listas, datas), então
    # exigi-los reprovaria tudo. A rede pega número de 2+ dígitos ou com decimal.
    ok, _ = e_fiel("## X\nVaria de 1 a 7 dias.", "texto sem esses numeros")
    assert ok is True          # documenta o furo conhecido, não o esconde


# --- janelar: não terminar em pergunta ---------------------------------------
def test_janela_nao_termina_em_pergunta_do_usuario():
    # O caso REAL: a janela 1 de "Pool de Mineração Zcash" acabava em "qual é o tempo
    # de liquidez de cada pool?" (a resposta estava na janela seguinte) e o modelo
    # inventou "1 a 7 dias" para respondê-la.
    turnos = [
        ("Usuário", "p1 " * 20), ("Assistente", "r1 " * 40),
        ("Usuário", "p2 " * 20), ("Assistente", "r2 " * 40),
    ]
    janelas = janelar(turnos, limite=300)
    assert len(janelas) > 1
    for j in janelas[:-1]:                    # a última pode terminar em pergunta
        assert not j.rstrip().split("\n\n")[-1].startswith("**Usuário:**")


def test_janela_leva_a_pergunta_orfa_para_a_proxima():
    turnos = [("Assistente", "r1 " * 40), ("Usuário", "pergunta orfa"), ("Assistente", "r2 " * 40)]
    janelas = janelar(turnos, limite=200)
    assert len(janelas) >= 2
    assert "pergunta orfa" in janelas[1]       # foi junto da resposta dela
    assert "pergunta orfa" not in janelas[0]


def test_janela_so_de_perguntas_nao_trava():
    # Se não há resposta nenhuma, não há o que adiar: emite em vez de empurrar pra sempre.
    turnos = [("Usuário", "p%d " % i * 20) for i in range(6)]
    janelas = janelar(turnos, limite=200)
    assert janelas
    assert all(len(j) <= 200 for j in janelas)


def test_janela_adiada_ainda_respeita_o_limite():
    # A garantia de TAMANHO vence a de "não terminar em pergunta": estourar o n_ctx
    # some com a janela inteira; terminar em pergunta só arrisca um átomo.
    turnos = [("Assistente", "r " * 10), ("Usuário", "p " * 80), ("Assistente", "r2 " * 80)]
    janelas = janelar(turnos, limite=200)
    assert all(len(j) <= 200 for j in janelas)


# --- Vigia: detectar vazamento MÍNIMO e parar --------------------------------
def _vigia(amostras, total=2885, tol_rss=1500.0):
    """Vigia com RSS roteirizado. O amostrador é injetável pelo mesmo motivo que o
    clock do LatencyTracker: sem isso, esta lógica só seria testável com 4h de GPU."""
    it = iter(amostras)
    ultimo = [amostras[0]]

    def sampler():
        try:
            ultimo[0] = next(it)
        except StopIteration:
            pass
        return ultimo[0]

    v = Vigia(total, tol_rss, 600.0, amostrador=sampler)
    v.base_rss = amostras[0]
    v.base_vram_usada = None          # VRAM fora deste teste
    return v


def test_vigia_nao_para_processo_que_apenas_ASSENTA():
    # O FALSO POSITIVO REAL que derrubou um lote saudável na janela ~244: a 1ª versão
    # ancorava na janela 50 e extrapolava dali em reta. Mas o processo assenta — medido
    # com o Deduplicador real, a inclinação cai 14x entre a janela 50 e a 3386 (0,099 ->
    # 0,007 MB/janela) enquanto alocador, CUDA e llama.cpp acomodam. A janela deslizante
    # enxerga isso: a inclinação RECENTE vai a zero.
    import math
    base = 6000.0
    # curva de assentamento: cresce rápido no começo, achata (log)
    amostras = [base + 120 * math.log1p(i / 40) for i in range(1200)]
    v = _vigia(amostras, total=3386)
    for _ in range(1000):
        assert v.checar() is None


def test_vigia_pega_vazamento_minimo_pela_projecao():
    # 0,5 MB/janela é invisível como valor absoluto (150 MB na janela 300, bem abaixo do
    # limite de 1500) mas são +1,5 GB nas 3386. É a PROJEÇÃO que o pega.
    amostras = [6000.0 + 0.5 * i for i in range(1200)]
    v = _vigia(amostras, total=3386)
    motivo = None
    for _ in range(1200):
        motivo = v.checar()
        if motivo:
            break
    assert motivo is not None
    assert "MB/janela" in motivo
    assert v.n <= 400            # pega cedo, com 3.000 janelas ainda pela frente


def test_vigia_pega_estouro_absoluto_mesmo_sem_inclinacao():
    # Um salto único (não uma inclinação) tem que parar pelo limite absoluto.
    amostras = [5000.0] * 10 + [9000.0] * 50
    v = _vigia(amostras)
    motivos = [v.checar() for _ in range(30)]
    assert any(m and "cresceu" in m for m in motivos)


def test_vigia_nao_projeta_antes_de_ter_amostras():
    # Antes de MIN_AMOSTRAS a inclinação recente é ruído: melhor calar que abortar à toa.
    amostras = [5000.0 + 50.0 * i for i in range(400)]  # vazamento brutal
    v = _vigia(amostras, tol_rss=1e9)                   # limite absoluto fora do caminho
    for _ in range(Vigia.MIN_AMOSTRAS - 1):
        assert v.checar() is None                       # ainda aquecendo


def test_vigia_status_nao_quebra_sem_amostra():
    v = Vigia(100, 1500.0, 600.0, amostrador=lambda: None)
    assert "indisponível" in v.status()


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


def test_janelar_parte_turno_gigante_em_vez_de_estourar_o_contexto():
    # O bug que matou o lote real: um paste de código virava uma janela de 17.660
    # tokens e o llama.cpp levantava "exceed context window of 8192". Pior, em
    # silêncio: o worker morria, collect devolvia "", e a janela era marcada como
    # concluída com ZERO átomos. NENHUMA janela pode passar do limite.
    turnos = [("Usuário", "palavra " * 500), ("Assistente", "ok")]
    janelas = janelar(turnos, limite=200)
    assert len(janelas) > 2
    for j in janelas:
        assert len(j) <= 200


def test_janelar_ao_partir_nao_perde_conteudo():
    texto = " ".join(f"tok{i}" for i in range(300))
    janelas = janelar([("Usuário", texto)], limite=150)
    juntas = " ".join(janelas)
    for i in range(300):                    # nada some no corte
        assert f"tok{i}" in juntas


def test_janelar_pedaco_partido_recarrega_o_papel():
    # Sem o papel em cada pedaço, o trecho 2..N chega ao LLM sem saber quem falou.
    janelas = janelar([("Assistente", "palavra " * 200)], limite=200)
    assert len(janelas) > 1
    for j in janelas:
        assert j.startswith("**Assistente:**")


def test_janelar_palavra_unica_gigante_nao_trava():
    # Sem espaço onde cortar (um base64, por exemplo): corta seco em vez de estourar.
    janelas = janelar([("Usuário", "x" * 1000)], limite=120)
    assert all(len(j) <= 120 for j in janelas)
    assert "".join(j.replace("**Usuário:** ", "") for j in janelas) == "x" * 1000


def test_janelar_preserva_todo_o_conteudo():
    turnos = [("Usuário", f"pergunta {i}") for i in range(20)]
    janelas = janelar(turnos, limite=60)
    juntas = "\n\n".join(janelas)
    for i in range(20):                     # nada se perde no janelamento
        assert f"pergunta {i}" in juntas


def test_janelar_vazio():
    assert janelar([]) == []


def test_ler_turnos_pula_json_que_nao_e_conversa(tmp_path):
    # Achado no acervo real: 'danielpvp22_mente_digital_6dadd2.json' é um SBOM SPDX
    # (gerado pelo dependency graph do GitHub) largado na pasta gemini/. Derrubava o
    # lote inteiro com AttributeError. Um arquivo estranho pula; não mata 5h de import.
    p = tmp_path / "sbom.json"
    p.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": []}), encoding="utf-8")
    assert ler_turnos(str(p)) == []


def test_ler_turnos_tolera_json_corrompido(tmp_path):
    p = tmp_path / "quebrado.json"
    p.write_text("{ isto não é json", encoding="utf-8")
    assert ler_turnos(str(p)) == []


def test_ler_turnos_ignora_mensagem_malformada(tmp_path):
    p = _json(tmp_path, "c.json", [
        "uma string solta no lugar de uma mensagem",
        {"role": "user", "contents": "contents deveria ser lista"},
        {"role": "user", "contents": [{"type": "text", "content": "esta é válida"}]},
    ])
    assert ler_turnos(p) == [("Usuário", "esta é válida")]


# --- entidades / garantir_assunto ------------------------------------------
# O título é INDEXADO junto com o corpo (split_markdown, strip_headers=False), e a
# ablação de formato mediu o assunto no título valendo 0.029 de distância — a maior
# peça do formato, o triplo da Malha. Quem garante essa peça é o garantir_assunto.
def test_entidades_ignora_a_palavra_generica_do_assunto():
    assert entidades("Pool de mineração de Zcash") == {"zcash"}          # não 'pool'
    assert entidades("Clima no Paraguai") == {"paraguai"}                # não 'clima'


def test_entidades_pega_all_caps_ate_na_primeira_posicao():
    # 'YOLO' abre a frase mas é inequívoco; 'Fine-tuning' abre e é genérico
    assert entidades("YOLO em Tarkov") == {"yolo", "tarkov"}
    assert entidades("Fine-tuning de modelos YOLO para Tarkov") == {"yolo", "tarkov"}


def test_entidades_nao_cai_no_title_case_do_nome_de_arquivo():
    # REGRESSÃO: `descobrir_assunto` cai no tema_do_arquivo quando o modelo falha, e o
    # nome vem Title-Cased. A 1ª versão chamava 'Pelo' e 'Localmente' de entidade.
    assert entidades("Acessar Ollama Localmente Pelo Celular") == set()
    # ALL-CAPS continua valendo mesmo em Title Case (é caixa deliberada, não convenção)
    assert entidades("Como Usar GPU No Windows") == {"gpu"}


def test_garantir_assunto_prefixa_quando_o_titulo_so_repete_a_palavra_generica():
    # REGRESSÃO: bastava UMA keyword em comum para desistir de prefixar, e a comum
    # costuma ser a genérica -> 'Conexão com a Pool' (pool de quê?) passava batido.
    out = garantir_assunto("## Conexão com a Pool\ncorpo.", "Pool de mineração de Zcash")
    assert out.splitlines()[0] == "## Pool de mineração de Zcash: Conexão com a Pool"

    out = garantir_assunto("## O Sweet Spot em fine-tuning\ncorpo.",
                           "Fine-tuning de modelos YOLO para Tarkov")
    assert out.splitlines()[0].startswith("## Fine-tuning de modelos YOLO para Tarkov: ")


def test_garantir_assunto_nao_mexe_no_titulo_que_ja_nomeia_a_entidade():
    original = "## Raridade de Neve no Paraguai\ncorpo."
    assert garantir_assunto(original, "Clima no Paraguai") == original
    original = "## Equihash no Zcash\ncorpo."
    assert garantir_assunto(original, "Pool de mineração de Zcash") == original


def test_garantir_assunto_sem_entidade_cai_na_regra_antiga():
    # Assunto todo genérico: não há entidade a exigir. Prefixar por prefixar é o risco
    # oposto — assunto ERRADO envenena a recuperação (foi o mal das notas _Pt<N>_).
    original = "## Economia mensal\ncorpo."
    assert garantir_assunto(original, "economia da máquina de lavar louça") == original


# --- limpar_vault: o backup FORA do vault -----------------------------------
def test_backup_do_limpar_vault_fica_fora_do_vault():
    # REGRESSÃO: o backup dentro de Importado_Gemini/ era re-indexado pelo sync (glob
    # recursivo **/*.md), anulando a limpeza — os 38 átomos de lixo voltaram ao Chroma.
    import importar_gemini  # garante o sys.path do scripts/
    import limpar_vault
    from mente_digital.config import settings
    vault = os.path.abspath(settings.caminho_obsidian)
    lixo = os.path.abspath(limpar_vault.LIXO)
    # o destino do lixo NÃO pode estar sob o caminho indexado
    assert not lixo.startswith(vault + os.sep)
    assert lixo != vault
