"""
Ciclo de vida do conhecimento auto-colhido:

- Átomos nascem com a tag #conhecimento_novo (curiosidade não consolidada).
- Quando um átomo é de fato USADO numa resposta local, o pipeline REMOVE a tag
  (promoção) — Agent._consolidar_fontes + textutils.remover_tag.
- A busca local reporta as FONTES dos chunks que entraram no contexto (LocalResult.fontes).
- O histórico de conversa é atomizado (EtlProcessor.summarize_dump) em vez de resumo
  estruturado, e nasce como #conhecimento_novo.

Tudo com fakes (sem GPU/rede/Chroma).
"""
import os
from datetime import datetime

from mente_digital import prompts
from mente_digital import textutils
from mente_digital.agent import Agent, EtlProcessor, dividir_atomos, normalizar_atomo, normalizar_malha
from mente_digital.config import Settings, settings
from mente_digital.rag import NENHUM, VectorStore, strip_frontmatter
from mente_digital.state import AppContext

from conftest import FakeDoc, FakeLlama, FakeStore, FakeTts


# --- dividir_atomos (puro: 1 arquivo por átomo) ------------------------------
def test_dividir_atomos_separa_por_cabecalho():
    texto = (
        "## Um\ncorpo um\n#zettelkasten_atomico #conhecimento_novo\n\n"
        "## Dois\ncorpo dois\n#zettelkasten_atomico #conhecimento_novo\n"
    )
    blocos = dividir_atomos(texto)
    assert len(blocos) == 2
    assert blocos[0].startswith("## Um") and blocos[1].startswith("## Dois")


def test_dividir_atomos_ignora_preambulo():
    blocos = dividir_atomos("Aqui vão as notas:\n## Único\ncorpo\n")
    assert len(blocos) == 1
    assert blocos[0].startswith("## Único")   # preâmbulo antes do 1º '##' descartado


def test_dividir_atomos_sem_cabecalho_vazio():
    assert dividir_atomos("texto solto sem heading nenhum") == []


def test_dividir_atomos_sem_hash_mas_com_assinatura_de_atomo():
    # Modo de falha REAL do Qwen2.5-7B-Instruct (medido no A/B): título + corpo +
    # tags, sem o '## '. Antes virava [] -> _salvar_atomos colava tudo num arquivo.
    texto = (
        "Previsão do Tempo\nLisboa amanhã: 28°C.\n#zettelkasten_atomico #conhecimento_novo\n\n"
        "Uso de TensorRT\nOtimiza inferência YOLO.\n#zettelkasten_atomico #conhecimento_novo\n"
    )
    blocos = dividir_atomos(texto)
    assert len(blocos) == 2
    assert "Previsão do Tempo" in blocos[0] and "TensorRT" in blocos[1]


def test_dividir_atomos_prosa_solta_nao_vira_atomo():
    # O fallback é conservador: sem tags nem Malha Neural, não há átomo — dois
    # parágrafos de prosa continuam devolvendo [] (o fallback de _salvar_atomos assume).
    assert dividir_atomos("um parágrafo qualquer\n\noutro parágrafo qualquer") == []


# --- normalizar_atomo (o LLM dá a ideia, o Python dá a estrutura) -------------
_AGORA = datetime(2026, 7, 17, 10, 30)


def test_normalizar_impoe_tags_quando_o_modelo_esquece():
    # Modo de falha do Qwen2.5-Coder: emite '##' e some com as tags -> sem TAG_NOVO,
    # _consolidar_fontes nunca promove (o bug dos 169/177 no vault).
    out = normalizar_atomo("## TensorRT\nAcelera o YOLO.", "Sintese", _AGORA)
    assert prompts.TAG_NOVO in out and prompts.TAG_ATOMO in out
    assert "## TensorRT" in out


def test_normalizar_impoe_hash_quando_o_modelo_esquece():
    # Modo de falha do Qwen2.5-Instruct: tags certas, sem '## '.
    bruto = "Previsão do Tempo\nLisboa amanhã: 28°C.\n#zettelkasten_atomico #conhecimento_novo"
    out = normalizar_atomo(bruto, "Conversa", _AGORA)
    assert "## Previsão do Tempo" in out
    assert dividir_atomos(out)          # agora é fatiável


def test_normalizar_preserva_tags_inventadas_e_a_malha():
    out = normalizar_atomo(
        "## Clima\nSol.\n**Malha Neural:** [[Lisboa]]\n#tempo #meteorologia", "Conversa", _AGORA
    )
    assert "#tempo" in out and "#meteorologia" in out     # úteis no Obsidian, preservadas
    assert "**Malha Neural:** [[Lisboa]]" in out
    assert prompts.TAG_NOVO in out                        # e as canônicas garantidas


def test_normalizar_nao_duplica_tag_pendurada_no_fim_da_linha():
    # Visto no import real do Gemini: o modelo escreve a tag na MESMA linha da Malha
    # Neural. Sem colher dali, a canônica era acrescentada de novo — 12 de 19 átomos
    # saíam com #zettelkasten_atomico duas vezes.
    bruto = "## Pressão de Oferta\nA oferta é alta.\n**Malha Neural:** [[Oferta]] #zettelkasten_atomico"
    out = normalizar_atomo(bruto, "Gemini", _AGORA)
    assert out.count(prompts.TAG_ATOMO) == 1
    assert "**Malha Neural:** [[Oferta]]" in out       # a Malha sobrevive, sem a tag colada


def test_normalizar_nao_confunde_cabecalho_com_tag():
    # A regra de tag-no-fim exige espaço antes do '#': '## Título' nunca pode ser comido.
    out = normalizar_atomo("## Título com # no meio\ncorpo", "Sintese", _AGORA)
    assert "## Título com # no meio" in out


# --- normalizar_malha: o Obsidian só resolve [[duplo]] -----------------------
def test_malha_colchete_simples_vira_wikilinks():
    # O caso REAL reportado: 44 de 84 átomos do import saíram assim. '[FastAPI, ...]'
    # não é link no Obsidian — é sintaxe markdown sem URL, renderiza como texto.
    bruto = "[FastAPI, Faster-Whisper, Silero VAD (Opcional, mas recomendado), Ollama Python Library, Piper TTS ou Edge-TTS]"
    out = normalizar_malha(bruto)
    assert out == ("[[FastAPI]] [[Faster-Whisper]] [[Silero VAD]] "
                   "[[Ollama Python Library]] [[Piper TTS]] [[Edge-TTS]]")


def test_malha_parentese_nao_vira_link_lixo():
    # A vírgula DENTRO do parêntese quebraria 'Silero VAD (Opcional, mas recomendado)'
    # em '[[Silero VAD (Opcional]]' e '[[mas recomendado)]]'.
    out = normalizar_malha("[Silero VAD (Opcional, mas recomendado)]")
    assert out == "[[Silero VAD]]"


def test_malha_aninhamento_torto_do_modelo():
    # O caso REAL, achado em 45 de 378 átomos: o modelo abre duplo e fecha simples.
    # O regex de '[[...]]' não casava, o descasque externo deixava tudo como UM
    # conceito, e o resultado saía re-embrulhado e igualmente quebrado.
    bruto = "[[Córtex Auditivo] [Solicitações HTTP] [Carregamento de pesos]]"
    assert normalizar_malha(bruto) == "[[Córtex Auditivo]] [[Solicitações HTTP]] [[Carregamento de pesos]]"


def test_malha_aninhamento_torto_com_dois_conceitos():
    assert normalizar_malha("[[Vídeos processados] [Pulando vídeos]]") == \
        "[[Vídeos processados]] [[Pulando vídeos]]"


def test_malha_ja_correta_e_preservada():
    out = normalizar_malha("[[Estimativa de valor]] [[Cálculo de liquidez]]")
    assert out == "[[Estimativa de valor]] [[Cálculo de liquidez]]"


def test_malha_sem_colchete_nenhum():
    assert normalizar_malha("TensorRT, YOLO") == "[[TensorRT]] [[YOLO]]"


def test_malha_dedup_e_vazio():
    assert normalizar_malha("[A, a, A]") == "[[A]]"
    assert normalizar_malha("") == ""
    assert normalizar_malha("[]") == ""


def test_malha_frase_longa_nao_vira_link():
    # Conceito é um NOME. Se o modelo escreveu uma frase, é ruído, não link.
    frase = "este conceito se relaciona com muitas outras ideias do vault e mereceria uma nota inteira"
    assert normalizar_malha(f"[{frase}]") == ""


def test_normalizar_atomo_conserta_a_malha_no_lugar():
    # A integração: quem impõe a sintaxe é o normalizar_atomo, não o prompt.
    bruto = "## Backend em Python\nO servidor será assíncrono.\n**Malha Neural:** [FastAPI, Piper TTS]"
    out = normalizar_atomo(bruto, "Gemini", _AGORA)
    assert "**Malha Neural:** [[FastAPI]] [[Piper TTS]]" in out
    assert "[FastAPI," not in out


def test_normalizar_e_idempotente():
    once = normalizar_atomo("## A\ncorpo", "Sintese", _AGORA)
    assert normalizar_atomo(once, "Sintese", _AGORA) == once   # re-normalizar não duplica nada


def test_normalizar_nivel_de_cabecalho_vira_h2():
    assert "## Titulo" in normalizar_atomo("# Titulo\ncorpo", "Sintese", _AGORA)
    assert "### " not in normalizar_atomo("### Titulo\ncorpo", "Sintese", _AGORA)


def test_normalizar_texto_quebrado_ainda_deriva_titulo():
    # Sem título algum: melhor um derivado que um átomo sem cabeçalho.
    out = normalizar_atomo("a lavadora moderna consome de 8 a 15 litros por ciclo.", "Sintese", _AGORA)
    assert out.count("## ") == 1
    assert dividir_atomos(out)


def test_normalizar_proveniencia_nao_polui_o_indice():
    # A proveniência vai em frontmatter DE PROPÓSITO: strip_frontmatter a remove antes
    # do chunking, então 'colhido_em'/'origem' nunca entram no embedding nem no
    # aterramento léxico (o vault já sofre com boilerplate em 97,6% das notas).
    out = normalizar_atomo("## A\ncorpo", "Sintese", _AGORA)
    assert out.startswith("---\n")
    assert "colhido_em: 2026-07-17" in out
    indexado = strip_frontmatter(out)
    assert "colhido_em" not in indexado and "origem" not in indexado
    assert "## A" in indexado and "corpo" in indexado


# --- textutils.remover_tag (puro) --------------------------------------------
def test_remover_tag_inline_mantem_a_outra():
    txt = "ideia atômica\n#zettelkasten_atomico #conhecimento_novo\n"
    out = textutils.remover_tag(txt, "#conhecimento_novo")
    assert "#conhecimento_novo" not in out
    assert "#zettelkasten_atomico" in out
    assert "  \n" not in out          # não deixou espaço pendurado antes do \n


def test_remover_tag_nao_pega_prefixo_de_outra_tag():
    txt = "x #conhecimento_novo_extra y"
    # \b garante palavra inteira: não deve remover parte de outra tag
    assert textutils.remover_tag(txt, "#conhecimento_novo") == txt


def test_remover_tag_ausente_e_noop():
    txt = "só #zettelkasten_atomico aqui"
    assert textutils.remover_tag(txt, "#conhecimento_novo") == txt


# --- LocalResult.fontes ------------------------------------------------------
def _store_com(resultados):
    vs = VectorStore(embeddings=None)
    vs._store = FakeStore(resultados)
    return vs


async def test_search_reporta_fontes_dos_usados():
    doc = FakeDoc("TensorRT acelera o YOLO", {"confidence": 0.6, "source": "/vault/Conhecimento_Novo/x.md"})
    vs = _store_com([(doc, 0.3)])
    res = await vs.search("tensorrt yolo")
    assert res.relevante is True
    assert res.fontes == ["/vault/Conhecimento_Novo/x.md"]


async def test_search_sem_fonte_nos_metadados_nao_quebra():
    doc = FakeDoc("algo confiante", {"confidence": 1.0})   # sem 'source'
    vs = _store_com([(doc, 0.3)])
    res = await vs.search("algo")
    assert res.fontes == []


async def test_search_irrelevante_sem_fontes():
    doc = FakeDoc("bolo de cenoura", {"source": "/vault/n.md"})
    vs = _store_com([(doc, 1.2)])                            # nem keyword nem confiante
    res = await vs.search("tensorrt")
    assert res.texto == NENHUM
    assert res.fontes == []


# --- Agent._consolidar_fontes (promoção) -------------------------------------
def _agent():
    ctx = AppContext(settings=settings)
    ctx.llama = FakeLlama([])
    ctx.tts = FakeTts()
    return Agent(ctx)


async def test_consolidar_remove_tag_do_arquivo_usado(tmp_path):
    nota = tmp_path / "Sintese_x.md"
    nota.write_text(
        "## TensorRT acelera YOLO\nAcelera ~36%.\n#zettelkasten_atomico #conhecimento_novo\n",
        encoding="utf-8",
    )
    await _agent()._consolidar_fontes([str(nota)])
    conteudo = nota.read_text(encoding="utf-8")
    assert "#conhecimento_novo" not in conteudo        # promovido
    assert "#zettelkasten_atomico" in conteudo         # continua átomo


async def test_consolidar_sem_tag_nao_reescreve(tmp_path):
    nota = tmp_path / "manual.md"
    original = "## Nota do usuário\nConteúdo.\n#zettelkasten_atomico\n"
    nota.write_text(original, encoding="utf-8")
    mtime0 = nota.stat().st_mtime
    await _agent()._consolidar_fontes([str(nota)])
    assert nota.read_text(encoding="utf-8") == original  # idempotente, sem tag pra tirar
    assert nota.stat().st_mtime == mtime0                # nem tocou no arquivo


async def test_consolidar_arquivo_inexistente_nao_explode():
    # best-effort: fonte apagada entre a resposta e a promoção não pode derrubar nada
    await _agent()._consolidar_fontes(["/caminho/que/nao/existe.md"])


# --- EtlProcessor.summarize_dump (atomização da conversa) --------------------
class FakeVectorStore:
    def __init__(self):
        self.syncs = 0

    async def sync(self):
        self.syncs += 1


def _etl_com(resposta_llm, tmp_path, monkeypatch):
    """EtlProcessor com dump temporário e LLM falso devolvendo `resposta_llm`."""
    st = Settings(_env_file=None)
    st.caminho_obsidian = str(tmp_path)
    dump = tmp_path / "chat_dump.md"
    st.arquivo_chat_dump = str(dump)            # summarize_dump lê ESTE caminho
    os.makedirs(st.dir_conhecimento_novo, exist_ok=True)
    monkeypatch.setattr("mente_digital.etl.settings", st)     # summarize_dump lê o dump por aqui
    monkeypatch.setattr("mente_digital.etl.db.log_etl", lambda *a, **k: None)  # hermético: sem SQLite real
    ctx = AppContext(settings=st)
    ctx.llama = FakeLlama([resposta_llm])
    ctx.vectorstore = FakeVectorStore()
    return EtlProcessor(ctx), dump, st


async def test_conversa_vira_atomos_com_tag_novo(tmp_path, monkeypatch):
    atomo = "## TensorRT\nAcelera inferência YOLO.\n#zettelkasten_atomico #conhecimento_novo\n"
    etl, dump, st = _etl_com(atomo, tmp_path, monkeypatch)
    dump.write_text("## [t]\n**Usuário:** o que é tensorrt\n**Mente Digital:** acelera inferência.\n", encoding="utf-8")

    await etl.summarize_dump()

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Conversa_")]
    assert len(gerados) == 1
    conteudo = (st.dir_conhecimento_novo / gerados[0]).read_text(encoding="utf-8")
    assert prompts.TAG_NOVO in conteudo
    assert dump.read_text(encoding="utf-8") == ""      # dump limpo após salvar


async def test_conversa_small_talk_nao_cria_nota_mas_limpa_dump(tmp_path, monkeypatch):
    etl, dump, st = _etl_com("NADA", tmp_path, monkeypatch)
    dump.write_text("## [t]\n**Usuário:** oi tudo bem?\n**Mente Digital:** tudo!\n" * 3, encoding="utf-8")

    await etl.summarize_dump()

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Conversa_")]
    assert gerados == []                                # nada a reter
    assert dump.read_text(encoding="utf-8") == ""       # mas limpou o dump


async def test_conversa_multi_atomo_gera_um_arquivo_por_atomo(tmp_path, monkeypatch):
    dois = (
        "## TensorRT\nAcelera YOLO.\n#zettelkasten_atomico #conhecimento_novo\n\n"
        "## YOLO\nDetector em tempo real.\n#zettelkasten_atomico #conhecimento_novo\n"
    )
    etl, dump, st = _etl_com(dois, tmp_path, monkeypatch)
    dump.write_text(
        "## [t]\n**Usuário:** o que é tensorrt e yolo?\n"
        "**Mente Digital:** tensorrt acelera o yolo, um detector em tempo real.\n",
        encoding="utf-8",
    )

    await etl.summarize_dump()

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Conversa_")]
    assert len(gerados) == 2                             # UM arquivo por átomo (Zettelkasten puro)
    assert dump.read_text(encoding="utf-8") == ""        # dump limpo após reter


async def test_process_queue_gera_um_arquivo_por_atomo(tmp_path, monkeypatch):
    dois = (
        "## A\nideia a\n#zettelkasten_atomico #conhecimento_novo\n\n"
        "## B\nideia b\n#zettelkasten_atomico #conhecimento_novo\n"
    )
    etl, _dump, st = _etl_com(dois, tmp_path, monkeypatch)

    await etl.process_queue([("tema qualquer", "dados brutos da web")])

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Sintese_")]
    assert len(gerados) == 2
    assert etl.ctx.vectorstore.syncs == 1               # sincroniza uma vez ao fim
    for f in gerados:                                    # cada átomo nasce como curiosidade
        assert prompts.TAG_NOVO in (st.dir_conhecimento_novo / f).read_text(encoding="utf-8")


async def test_atomo_sem_cabecalho_salvo_como_um_fallback(tmp_path, monkeypatch):
    # LLM quebrou o formato (sem '##'): não pode perder o conhecimento em silêncio.
    etl, _dump, st = _etl_com("conhecimento solto sem heading", tmp_path, monkeypatch)

    await etl.process_queue([("tema", "dados")])

    gerados = [f for f in os.listdir(st.dir_conhecimento_novo) if f.startswith("Sintese_")]
    assert len(gerados) == 1                             # fallback: 1 átomo com o texto inteiro


# --- portão NADA/vazio no normalizar_atomo ---------------------------------
def test_normalizar_rejeita_bloco_nada():
    # O sentinela "nada a extrair" vazava por bloco (o check do importador/ETL só pega
    # a saída inteira). Um bloco NADA não pode virar átomo com título.
    from datetime import datetime
    from mente_digital import agent
    assert agent.normalizar_atomo("## Preço de GPUs na AWS\nNADA", "x.json", datetime(2026, 7, 17)) == ""
    assert agent.normalizar_atomo("## Assunto\nnada.", "x.json", datetime(2026, 7, 17)) == ""


def test_normalizar_rejeita_atomo_so_com_malha():
    # Linha de Malha sem corpo = átomo oco (9 na base). Sem fato, não é átomo.
    from datetime import datetime
    from mente_digital import agent
    out = agent.normalizar_atomo(
        "## Reflexos\n**Malha Neural:** [[FPS]] [[APM]]", "x.json", datetime(2026, 7, 17))
    assert out == ""


def test_normalizar_mantem_atomo_com_fato_e_malha():
    # Não pode ser zeloso demais: átomo real com Malha continua passando.
    from datetime import datetime
    from mente_digital import agent
    out = agent.normalizar_atomo(
        "## Stratum\nO Stratum conecta o minerador à pool.\n**Malha Neural:** [[Zcash]]",
        "x.json", datetime(2026, 7, 17))
    assert "Stratum conecta" in out
    assert out.strip() != ""


# --- Rótulo improvisado da Malha (medido 2026-07-25 na ingestão do Amabis) -----
def test_rotulo_improvisado_da_malha_e_canonizado():
    """33% dos 2.038 átomos do livro trocaram '**Malha Neural:**' por um rótulo
    inventado; a linha caía como corpo e o átomo ficava fora do grafo."""
    bloco = ("## Divisão binária em euglenóides\n"
             "Euglenóides e diatomáceas se reproduzem por divisão binária.\n"
             "**Divisão binária:** [[Reprodução assexuada]]\n"
             "#zettelkasten_atomico")
    out = normalizar_atomo(bloco, "Livro 'X'", datetime(2026, 7, 25))
    assert "**Malha Neural:** [[Reprodução assexuada]]" in out
    assert "**Divisão binária:**" not in out


# --- corpo envolto em '<...>' (artefato do decoder, medido 2026-07-28) --------
def test_corpo_envolto_em_angular_e_desembrulhado():
    """2.919 linhas de corpo da base saíram como '<A planta precisa de luz>'.
    O '<' não é conteúdo: é marca de citação que o modelo inventa ao copiar o
    trecho da fonte, e ela entra no embedding e na fala."""
    out = normalizar_atomo(
        "## Luz mínima para cultivo\n<O cultivo exige cinco a seis horas de sol ao dia.>",
        "Livro 'X'", datetime(2026, 7, 28))
    assert "O cultivo exige cinco a seis horas de sol ao dia." in out
    assert "<O cultivo" not in out


def test_angular_com_pontuacao_depois_do_fecha_preserva_a_pontuacao():
    """Variante real (101 linhas): '<texto>.' — o ponto vem DEPOIS do '>'."""
    out = normalizar_atomo(
        "## Mulching conserva umidade\n<Uma camada espessa de mulha conserva umidade no solo>.",
        "Livro 'X'", datetime(2026, 7, 28))
    assert "conserva umidade no solo." in out
    assert ">" not in out.split("## ")[1]


def test_atomo_truncado_perde_o_angular_mesmo_sem_o_fecha():
    """78 átomos ficaram truncados no meio da frase (o lote estourou o orçamento
    de saída) e o '>' nunca veio. O texto que sobrou ainda vale; o '<' não."""
    out = normalizar_atomo(
        "## Ciclo de vida do barbeiro\n<O barbeiro adquire",
        "Livro 'X'", datetime(2026, 7, 28))
    assert "O barbeiro adquire" in out
    assert "<O barbeiro" not in out


def test_tag_tipo_html_sozinha_NAO_e_desembrulhada():
    """A guarda: '<think>' e afins são UMA palavra entre angulares — desembrulhar
    viraria a palavra em conteúdo. Só desembrulha o que tem cara de FRASE."""
    out = normalizar_atomo(
        "## Vazamento de raciocínio\n<think>\nO modelo deixou a marca no corpo.",
        "Livro 'X'", datetime(2026, 7, 28))
    assert "<think>" in out


def test_prosa_com_negrito_NAO_vira_malha():
    """A guarda que impede o conserto de comer conteúdo: só canoniza a linha que
    TEM colchete. Um destaque em negrito no meio da ideia continua sendo prosa."""
    bloco = ("## Fotossíntese depende de luz\n"
             "**Importante:** o ciclo de Calvin não roda sem ATP do fotossistema.\n"
             "**Malha Neural:** [[Ciclo de Calvin]]\n"
             "#zettelkasten_atomico")
    out = normalizar_atomo(bloco, "Livro 'X'", datetime(2026, 7, 25))
    assert "**Importante:** o ciclo de Calvin não roda sem ATP" in out
    assert out.count("**Malha Neural:**") == 1
