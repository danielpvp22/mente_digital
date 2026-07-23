"""
Configuração central do Mente Digital.

Tudo que era constante espalhada no arquivo monolítico agora vive aqui, como
Pydantic Settings. Os defaults preservam o seu ambiente atual (Windows, RTX 3080),
mas qualquer campo pode ser sobrescrito por variável de ambiente (prefixo MENTE_)
ou por um arquivo .env — sem tocar no código.

Ex.:  MENTE_N_CTX=4096  MENTE_TEMPERATURA_RESPOSTA=0.1  python -m mente_digital
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Raiz do projeto = a pasta que CONTÉM o pacote mente_digital/. Este arquivo mora em
# mente_digital/config.py, então subimos DOIS níveis: .parent = mente_digital/,
# .parent.parent = raiz do repo. TODOS os caminhos default derivam daqui — de
# __file__, não de caminhos absolutos de uma máquina específica — então o projeto
# roda de qualquer diretório e em qualquer máquina, sem editar código. Cada campo
# ainda pode ser sobrescrito por .env / variável de ambiente (prefixo MENTE_).
BASE_DIR = Path(__file__).resolve().parent.parent
# Dados de runtime (vault, índice vetorial, SQLite, dump, modelos) vivem TODOS sob
# dados/ — a raiz fica só com código/config. Tudo gitignored (ver .gitignore).
DIR_DADOS = BASE_DIR / "dados"
# Modelos de IA (LLM .gguf, voz Piper) e cache do Whisper: pastas versionadas
# (com .gitkeep), mas os binários em si não vão pro git — ver .gitignore.
DIR_MODELOS = DIR_DADOS / "modelos"
DIR_WHISPER = DIR_MODELOS / "whisper"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MENTE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Caminhos (relativos à raiz do projeto — ver BASE_DIR acima) -----------
    # Coloque os modelos em ./modelos/ (ou aponte para outro lugar via .env).
    caminho_modelo_llama: str = str(DIR_MODELOS / "Qwen3-8B-Q4_K_M.gguf")
    caminho_voz_piper: str = str(DIR_MODELOS / "pt_BR-cadu-medium.onnx")
    # XTTS-v2 (engine de voz GPU alternativo): DIRETÓRIO com config.json + model.pth +
    # vocab.json + speakers_xtts.pth (não é um arquivo único como o Piper). Baixe o
    # modelo "coqui/XTTS-v2" para cá. Só é usado quando MENTE_TTS_ENGINE=xtts.
    caminho_modelo_xtts: str = str(DIR_MODELOS / "xtts_v2")
    # Cache onde o faster-whisper baixa os pesos do Whisper na 1ª execução.
    caminho_cache_whisper: str = str(DIR_WHISPER)
    # Vault Obsidian: default dentro do projeto (pode começar vazio); aponte para
    # o seu vault real via MENTE_CAMINHO_OBSIDIAN no .env.
    caminho_obsidian: str = str(DIR_DADOS / "Cerebro_Digital")
    diretorio_banco_vetorial: str = str(DIR_DADOS / "banco_vetorial_cerebro")
    arquivo_chat_dump: str = str(DIR_DADOS / "chat_dump_bruto.md")
    db_telemetria: str = str(DIR_DADOS / "telemetria_etl.db")
    subpasta_conhecimento_novo: str = "Conhecimento_Novo"

    # --- LLM (GPU) -------------------------------------------------------------
    n_gpu_layers: int = -1
    n_ctx: int = 8192
    temperatura_resposta: float = 0.2
    max_tokens_resposta: int = 800
    max_tokens_query: int = 15
    # Gate léxico do extrator (painel 2026-07, fase a): pergunta AUTO-CONTIDA (sem
    # referência ao turno anterior) não paga a chamada LLM do QueryOptimizer — usa
    # limpar_query direto (que já era o fallback). Remove ~0,3-0,9s do TTFT da
    # maioria das perguntas. Desligue se o aterramento léxico degradar (acompanhe
    # o [LOCAL] relevante= nos logs).
    optimizer_gate: bool = True
    # FASE (b) do extrator (consultoria TTFT #9): quando a pergunta REFERENCIA o turno
    # anterior (o gate acima NÃO poupa o LLM), a recuperação vetorial parte em PARALELO
    # com o decode do extrator — o embedding usa a pergunta CRUA (ver _texto_busca),
    # que não depende dos termos; só o aterramento léxico espera o LLM. Esconde o custo
    # da busca embaixo de uma chamada que já existia. Sem efeito nos turnos que o gate
    # (a) já poupa, nem com HyDE ligado (HyDE também chama o LLM — a GPU é serializada,
    # não haveria paralelismo real).
    optimizer_overlap: bool = True
    max_tokens_sintese: int = 1600
    max_tokens_resumo: int = 1800
    # Governador de verbosidade (#7): pergunta factual curta (≤ N palavras, sem pista de
    # "explica") ganha uma resposta de UMA frase, com teto de tokens menor — menos GPU,
    # menos latência de fala. Pedido de explicação usa max_tokens_resposta cheio.
    max_tokens_resposta_curto: int = 90
    verbosidade_curto_max_palavras: int = 8
    # Síntese sob Demanda (#23): "o que eu sei sobre X". Fluxo map-reduce SEPARADO —
    # recupera muitos átomos e os resume em LOTES que cabem no n_ctx, depois combina.
    sintese_top_k: int = 60             # átomos recuperados (largo — é uma varredura do tema)
    # HUBS PRIMEIRO na síntese (G7, Onda 3): o map-reduce fatia os átomos em lotes; se o
    # tema é grande, os últimos lotes podem nem influenciar tanto o reduce. Reordenar os
    # átomos por CENTRALIDADE na malha (o "backbone" do tema — átomos cujos conceitos raros
    # reaparecem nos vizinhos do próprio conjunto) faz o núcleo do tema entrar nos PRIMEIROS
    # lotes. Sem malha construída, mantém a ordem vetorial. Desligue com off.
    sintese_hubs_primeiro: bool = True
    sintese_lote_chars: int = 6000      # orçamento de chars por lote (map) — protege o n_ctx
    max_tokens_sintese_tema: int = 400  # teto de cada resumo parcial (map)

    # --- Tuning llama.cpp (§7 do estudo de perf) -------------------------------
    # Flash attention: kernel de atenção fundido. Ganho DUPLO num card apertado —
    # prefill mais rápido (melhora TTFT com contexto RAG longo) E menos VRAM de
    # KV-cache. O default do llama-cpp-python é False; aqui ligamos por padrão.
    flash_attn: bool = True
    # Lote de prefill. n_ubatch controla o paralelismo ao "engolir" o prompt —
    # subir ajuda o TTFT de prompts RAG longos, mas custa VRAM no buffer de compute.
    # Mantemos o default do llama.cpp (512); é um botão, não um valor mágico.
    n_batch: int = 512
    n_ubatch: int = 512
    # KV-cache quantizado: "f16" (default seguro, sem perda) | "q8_0" | "q4_0".
    # q8_0 corta ~metade da VRAM de KV com perda de qualidade ínfima -> libera
    # espaço para embeddings/Whisper. EXIGE flash_attn=True (o cache V quantizado
    # só funciona com flash attention no llama.cpp). Ver _build_llama_kwargs.
    kv_cache_type: str = "f16"
    # --- Qwen3: raciocínio e a tag <think> --------------------------------------
    # O Qwen3 ABRE toda resposta com um bloco <think>…</think>. "/no_think" no system
    # prompt desliga o raciocínio (o bloco sai VAZIO), mas a TAG continua saindo — e sem
    # removê-la ela vaza para o SentenceChunker e o TTS "fala" a marcação. Os dois botões
    # andam juntos e são NO-OP em modelos que não usam <think> (Qwen2.5, Llama…), por isso
    # são flags e não default. Ligue os dois ao apontar o LLM para um Qwen3.
    # LIGADOS por default porque o modelo default É um Qwen3. Ao apontar o LLM para um
    # modelo SEM <think> (Qwen2.5, Llama…), desligue os dois — o strip vira no-op sozinho,
    # mas o "/no_think" viraria texto solto no system prompt.
    llm_no_think: bool = True       # prefixa "/no_think" no system prompt
    llm_strip_think: bool = True    # remove o bloco <think>…</think> do início do stream
    # PREÂMBULO COMUM (consultoria TTFT #10 — INVESTIGAÇÃO, off até o bench provar):
    # prefixa TODO system prompt com prompts.PREAMBULO_COMUM (ver llm.montar_system).
    # Prefixo byte-idêntico entre extrator/roteador/resposta => o llama.cpp reaproveita
    # o KV do maior prefixo comum entre chamadas consecutivas (prefill economizado).
    # Critério de kill da consultoria: só cravar True se um A/B com o modelo REAL
    # (eval/bench_ttfa.py --real, comparando os dois estados) medir >=50ms/turno RAG.
    prompt_preambulo_comum: bool = False

    # --- Speculative decoding (§5) — prompt-lookup ------------------------------
    # DESLIGADO por default após benchmark no RTX 3080 (2026-07): o
    # LlamaPromptLookupDecoding do llama-cpp-python 0.3.34 (a) fica MAIS lento em
    # prompt curto (93 vs 121 tok/s, overhead de lookup sem aceitação) e (b)
    # CRASHA em contexto longo — "could not broadcast array ... shape mismatch" —
    # justo no caso de uso principal (RAG). Mantido como flag experimental: religue
    # (MENTE_SPECULATIVE_ENABLED=true) só após subir o llama-cpp-python p/ uma
    # versão que corrija o bug de shape no draft com contexto grande.
    speculative_enabled: bool = False
    speculative_num_pred_tokens: int = 10   # tamanho do n-grama proposto por passo

    # --- STT / Embeddings ------------------------------------------------------
    # Backend: faster-whisper (CTranslate2) — mesmos pesos do Whisper, bem mais
    # rápido. Para MÁXIMA qualidade de transcrição, suba o modelo:
    #   MENTE_WHISPER_MODEL=large-v3  (e MENTE_WHISPER_DEVICE=cuda se tiver VRAM).
    whisper_model: str = "small"
    whisper_device: str = "cpu"     # "auto"/"cuda"/"cpu" — cuda: large-v3 usa ~3GB VRAM
    # "auto" = float16 na GPU, int8 na CPU (int8 é rápido e preciso o bastante).
    whisper_compute_type: str = "auto"
    # STT no caminho crítico (painel 2026-07): o CTranslate2 fica em ~4 threads por
    # default num Ryzen de 16 núcleos, e beam 5 é luxo para comando de voz curto —
    # greedy (beam 1) corta ~1,5-2x o decode com WER praticamente igual em fala
    # curta. Juntos tipicamente cortam o stt_ms pela metade. Se a transcrição de
    # fala longa/ruidosa degradar, volte MENTE_WHISPER_BEAM_SIZE=5 no .env (valide
    # comparando o stt_ms e as transcrições no log antes/depois).
    whisper_cpu_threads: int = 8
    whisper_beam_size: int = 1
    # SPIKE Whisper->GPU (consultoria TTFT #4): o STT na CPU custa ~0,8x a duração da
    # fala — o maior item individual do TTFA de voz. Na GPU int8 o turbo cai a fração
    # disso, MAS a VRAM opera com ~1,3GB livres (a 3080 também segura o desktop), então
    # o load em cuda pode falhar por OOM/fragmentação. True = cai para CPU/int8 (o
    # comportamento estável de produção) em vez de deixar o STT morto — o spike liga
    # MENTE_WHISPER_DEVICE=cuda + MENTE_WHISPER_COMPUTE_TYPE=int8 sabendo que o pior
    # caso é voltar ao status quo, logado. Ver também scripts/bench_stt_threads.py
    # (o plano B barato na CPU: threads/afinidade de CCD no 7950X3D).
    whisper_gpu_fallback_cpu: bool = True
    # GATE DE CONFIANÇA DO STT (#37 parte 1): fala CURTA que o Whisper crava de um ruído
    # ("oração"/"atenção"/"reparação" — palavra válida, baixa confiança) era respondida a
    # sério. Com o gate LIGADO, uma transcrição de <= N palavras cujo avg_logprob médio
    # fica abaixo do limiar é DESCARTADA (o app não responde lixo). DEFAULT OFF: o limiar é
    # sensível ao mic/ambiente e deve ser calibrado por VOZ (ligue e observe o log
    # [WHISPER] "confiança=..."; avg_logprob ~0 = ótimo, mais negativo = pior; ~-1.0 é um
    # ponto de partida). Só morde fala curta — enunciado longo com 1 palavra incerta passa.
    whisper_descartar_incerto: bool = False
    whisper_confianca_min_logprob: float = -1.0
    whisper_incerto_max_palavras: int = 2
    # BACKCHANNEL (teste real 2507): ignora "ok"/"aham"/"tchau"/"valeu" — acuse-recebido
    # que virava átomo de memória ("Entendido, registrei") e ativava o pipeline à toa.
    # Ignora em silêncio (nada dito, nada gravado). Lista em otimizador._BACKCHANNEL.
    ignorar_backchannel: bool = True
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # "auto" = usa a GPU (cuda) se disponível, senão CPU. O embedding da query está
    # no caminho crítico de TODA pergunta, então a GPU baixa a latência por-pergunta
    # (e acelera a reindexação). Force com MENTE_EMBEDDING_DEVICE=cpu se precisar.
    embedding_device: str = "auto"
    # PREFIXOS DE INSTRUÇÃO (família e5): modelos como intfloat/multilingual-e5-* foram
    # treinados com "query: " nas perguntas e "passage: " nos documentos — sem isso
    # perdem boa parte da qualidade. VAZIOS por padrão (o MiniLM atual não usa prefixo);
    # ao trocar para um e5, ligue no .env: MENTE_EMBEDDING_QUERY_PREFIX="query: " e
    # MENTE_EMBEDDING_PASSAGE_PREFIX="passage: ". Aplicados no EmbeddingProvider (rag.py),
    # cobrindo TODO caminho (Chroma index/busca, malha, RAG efêmero web) de uma vez.
    # ATENÇÃO: trocar o modelo/prefixo EXIGE reindexar o vault (apagar banco_vetorial_*)
    # E recalibrar os thresholds do gate — a escala de distância muda com o modelo.
    embedding_query_prefix: str = ""
    embedding_passage_prefix: str = ""

    # --- RAG / Busca -----------------------------------------------------------
    # Nº de candidatos recuperados do vetor. A base é ZETTELKASTEN ATÔMICA — cada
    # nota tem UMA ideia. Para montar uma resposta de verdade é preciso colher DEZENAS
    # de átomos (o usuário estimou 10~30), senão o assistente "esquece" o que já foi
    # anotado. Por isso o leque é largo. Custo: mais prefill (TTFT) — limitado pelo
    # orçamento de caracteres abaixo, não só pela contagem.
    rag_top_k: int = 40
    # Teto de chunks/átomos que entram no contexto do LLM. Com nota atômica, 4 era
    # quase nada. Subimos para reunir muitos átomos por resposta; o corte REAL costuma
    # ser o rag_context_char_budget (protege o n_ctx), este é só o limite de contagem.
    rag_max_chunks: int = 30
    # Orçamento de caracteres do contexto local montado. Guarda o n_ctx (8192): mesmo
    # colhendo 30 átomos, paramos de empilhar ao bater este teto (átomos são pequenos,
    # mas alguns imports são grandes). ~12k chars ≈ 3k tokens, sobra folga p/ resposta.
    rag_context_char_budget: int = 12000
    rag_score_max: float = 1.5          # distância máxima p/ um chunk ser exibível
    # PRINCIPAL BOTÃO DE CALIBRAÇÃO: distância abaixo da qual um match é "confiante"
    # o bastante para valer como Cache Hit MESMO sem casar palavra-chave. Ajuste
    # olhando o log "[LOCAL] melhor_dist=..." com os seus próprios dados.
    rag_score_confident: float = 0.8
    # ATERRAMENTO PONDERADO POR IDF (G3, Onda 2/Graphify): o aterramento léxico é um OR
    # booleano — bastava a nota conter UMA keyword da pergunta. Uma keyword comum (que
    # escapou do STOP, ex.: "base", "sistema") aterrava a nota ERRADA (a mesma falha que a
    # Malha evita de propósito, ver rag.MalhaIndex). Agora só uma keyword RARA
    # (idf_palavra >= este mínimo) conta como evidência; se TODAS forem hub, não há
    # aterramento léxico (a nota ainda pode entrar por confiança semântica). O IDF é sobre
    # o CORPUS de átomos (log(N/df)), construído junto com a Malha. Calibre como o
    # rag_score_confident: 0 desliga; MAIOR = mais rígido (mais web); menor = mais frouxo.
    # Default afinado para o vault real (~3k átomos); revise em vault pequeno (idf é menor).
    aterramento_idf_min: float = 1.5
    # ROTEAR DEFINICIONAL PARA A WEB (Part A, Onda 3): perguntas de conhecimento GERAL
    # ("o que é X", "quem foi Y", "me explica Z") vão DIRETO pra web, pulando o local —
    # como o talvez_tempo_real já faz. Conserta o sintoma "pergunta geral puxa nota
    # pessoal" (o Tarkov: "o que é RAG" devolvia a nota-piada do usuário). O IDF (acima)
    # NÃO resolve esse caso (keyword rara genuína), então a correção é de ROTA. Pergunta
    # PESSOAL ("meu projeto", "o que eu anotei") é excluída e segue local (ver
    # tools.pergunta_definicional). Desligue com MENTE_ROTEAR_DEFINICIONAL_WEB=false.
    rotear_definicional_web: bool = True
    # LEVER B — força mínima do vault para confiar no local numa pergunta DEFINICIONAL.
    # Em vez de mandar TODA definição pra web (Part A puro), o app consulta o vault e só
    # escala pra web se ele for FRACO: menos de N átomos DISTINTOS casando o tema. A base
    # é Zettelkasten atômica (1 ideia/nota), então um tema que você REALMENTE estudou vira
    # MUITOS átomos (você estimou 10~30), enquanto uma menção-piada/incidental vira 1~2 —
    # o Tarkov ("o que é RAG" → 1 nota-piada) cai abaixo do mínimo e vai pra web, mas um
    # tema bem coberto responde LOCAL (e sem pagar web). Como calibrar (só este número):
    #   1  = confia em QUALQUER match (B desligado na prática — o Tarkov volta a passar);
    #   2  = exige 2+ átomos (filtra menção única, tolerante);
    #   3  = exige 3+ (default: "tema desenvolvido", separa estudo de menção solta);
    #   5  = exige 5+ (rígido, vai mais pra web);
    #   alto (ex.: 999) = quase toda definição vai pra web (≈ Part A puro).
    # Só age quando rotear_definicional_web=True E a pergunta é definicional (não pessoal).
    definicional_min_atomos: int = 3
    # DEDUP NEAR-DUPLICATE DO CONTEXTO (G6, Onda 3): a busca dedupa átomos por texto
    # EXATO, mas o ETL pode ter atomizado o MESMO fato de fontes diferentes (web +
    # conversa) com palavras quase iguais — os dois entram no contexto e gastam prefill
    # (TTFT maior). Aqui, ao montar os candidatos, um átomo cujo conjunto de tokens é
    # >= este limiar de Jaccard vs. um já escolhido é descartado (velocidade pura, sem
    # embedding). Conservador de propósito (0.9 ≈ quase idêntico) para não podar átomo
    # legitimamente distinto. 0 ou 1.0 desliga o near-dup (mantém só o dedup exato).
    rag_dedup_near_jaccard: float = 0.9
    # Diagnóstico: MENTE_RAG_DEBUG=true loga cada chunk recuperado (dist/fonte/trecho)
    # para você VER o que a busca pega. Off por padrão (senão polui o log de prod).
    rag_debug: bool = False
    # EARLY-STOP DA CASCATA (#3): quando LIGADO, a cascata de resposta PARA na primeira
    # fonte que responde com confiança (RAM > Banco > Web) — se a RAM já respondeu, nem
    # roda a passada do Banco; se o Banco respondeu, não vai à web. Troca a fusão
    # multi-fonte (cada fonte contribuía um parágrafo) por MENOS passes de inferência na
    # GPU serializada = menos latência no próximo turno. Desligue (MENTE_EARLY_STOP_CASCATA
    # =false) para voltar à fusão completa RAM+Banco. A web já era "só se nada respondeu".
    early_stop_cascata: bool = True
    # HyDE (Hypothetical Document Embeddings): antes de buscar no vetor, o LLM gera
    # uma PASSAGEM hipotética no estilo das notas e ELA é embeddada — casa melhor com
    # os parágrafos do banco do que a query crua (o modelo de embedding é simétrico).
    # Custa uma chamada extra ao LLM no caminho crítico da busca LOCAL (~300-900ms),
    # por isso é um BOTÃO: ligue com MENTE_RAG_HYDE=true e meça no log "[HYDE]".
    # Com off, a base já embeddar a pergunta natural inteira (grátis) em vez da query
    # de 5 palavras — que casa mal com passagens longas.
    rag_hyde: bool = False
    max_tokens_hyde: int = 160          # tamanho da passagem hipotética (curta)
    # MALHA: expansão por conceito compartilhado (ver rag.MalhaIndex). Depois de a busca
    # vetorial escolher os átomos, traz a VIZINHANÇA deles — os átomos que o LLM marcou
    # com os mesmos [[conceitos]] na ingestão. Não custa LLM (parsing + lookup em RAM);
    # custa PREFILL (mais átomos no contexto = TTFT maior), por isso é um botão.
    #
    # DESLIGADA por padrão porque a medição NÃO a defendeu. Nas 74 perguntas reais:
    # contexto de 5.049 -> 7.476 chars de mediana (+48% de prefill, direto no TTFT) e,
    # na inspeção, os vizinhos vêm do assunto certo e da pergunta errada (fine-tuning
    # de YOLO numa pergunta sobre TensorRT). A ablação de formato explica o porquê: a
    # linha da Malha vale 0.010 de distância (vs 0.029 do assunto no título) — ela quase
    # não carrega sinal. Ligue com MENTE_MALHA_EXPANDIR=true para experimentar; o
    # candidato a consertá-la é filtrar o vizinho por similaridade com a PERGUNTA
    # (rag.rankear_por_similaridade, já existe), exigindo conceito raro E proximidade.
    malha_expandir: bool = False
    # Teto de vizinhos. Eles entram DEPOIS dos matches reais e disputam o que sobra do
    # rag_context_char_budget — na prática o orçamento corta antes deste número.
    malha_max_vizinhos: int = 8
    # Corte de hub por IDF. Medido na base real (3.004 átomos, 4.512 conceitos):
    # [[Python]] em 101 átomos -> idf 3.4; [[DuckDB]] em 34 -> 4.5; conceito em 3 -> 6.9.
    # Compartilhar um conceito raro é evidência de vizinhança; compartilhar [[IA]] não é.
    # Subir = expansão mais conservadora (só conceito muito específico conecta).
    malha_idf_min: float = 4.0
    # G5′ (Onda 3): FILTRO DE PROXIMIDADE do vizinho da malha à PERGUNTA. A medição que
    # desligou a expansão mostrou o vizinho vindo "do assunto certo, da pergunta errada"
    # (fine-tuning de YOLO numa pergunta sobre TensorRT). Agora, além do conceito raro
    # (malha_idf_min), o vizinho só entra se a similaridade de cosseno do seu texto com a
    # PERGUNTA for >= este mínimo (usa o embedding já carregado, rankear_por_similaridade).
    # Assim a expansão exige conceito raro E proximidade — o conserto que torna
    # malha_expandir=true viável (meça o TTFT/qualidade ao religar). 0 desliga o filtro
    # (volta a aceitar todo vizinho). Sem embeddings (testes), o filtro é pulado.
    malha_sim_min: float = 0.5
    chunk_size: int = 1000
    chunk_overlap: int = 150
    chroma_batch: int = 2000
    # Resultados do DDG. 6 (era 4) porque parte dos sites BLOQUEIA o fetch (403 anti-bot,
    # 500, cert quebrado) — pedir mais candidatos garante que sobrem fontes boas mesmo
    # quando 2-3 caem. É o teto de URLs que o deep-fetch pode abrir (ver web_fetch_pages).
    web_max_results: int = 6
    web_prefetch_results: int = 3
    # Fallback de busca: tenta cada backend do ddgs em ordem até um dar resultado.
    web_backends: list[str] = ["auto", "html", "lite"]

    # --- Web deep-fetch + RAG efêmero -----------------------------------------
    # PROBLEMA que isto resolve: o ddgs.text() devolve só SNIPPETS (título + 1-2
    # frases). Para perguntas específicas/numéricas ("quanto o TensorRT acelera o
    # YOLO"), o número está DENTRO do artigo, nunca no snippet — então o LLM, fiel ao
    # anti-alucinação, respondia "Não tenho informações suficientes" mesmo com a web
    # tendo "respondido". A correção: baixar o CORPO das top-N páginas, extrair o
    # texto principal (trafilatura), atomizar e RANKEAR esses trechos contra a
    # pergunta com o embedding JÁ carregado, e passar só os melhores ao LLM (RAG
    # efêmero, nada é indexado). Desligue com MENTE_WEB_FETCH_ENABLED=false para
    # voltar ao comportamento antigo (só snippets).
    web_fetch_enabled: bool = True
    web_fetch_pages: int = 6            # LEGADO: supersebido por web_fetch_pool (o race dispara até pool)
    web_fetch_timeout: float = 6.0      # timeout do client httpx (s) — não travar o TTFA
    web_fetch_max_chars: int = 20000    # teto de texto extraído por página (anti-lixo)
    # Fallback de SSL: se a página falhar por VERIFICAÇÃO de certificado (cert vencido,
    # cadeia inválida, hostname mismatch — comum em blogs BR), re-baixa SEM verificar o
    # cert. Só nesse erro (nunca em 403/500/timeout). É conteúdo público lido p/ RAG, mas
    # como é downgrade de segurança fica aqui como botão. False = comportamento estrito.
    web_fetch_ssl_fallback: bool = True
    web_chunk_size: int = 600           # tamanho do átomo efêmero (chars)
    web_chunk_overlap: int = 80
    web_rank_top_k: int = 12            # nº de trechos rankeados que entram no contexto
    # Orçamento de chars do contexto web montado (protege o n_ctx, como o do RAG local).
    web_context_char_budget: int = 6000
    # --- RACE-FIRST-K no deep-fetch (aceleração web) ---------------------------
    # PROBLEMA: o deep-fetch antigo esperava TODAS as top-N páginas (asyncio.gather) —
    # a mais lenta ou morta segurava o turno inteiro (medido: 3-12s; turno 1 = 10,4s
    # p/ 6 páginas). CORREÇÃO: dispara ATÉ `web_fetch_pool` fetches CONCORRENTES e
    # aceita os PRIMEIROS `web_fetch_aceitar` que voltarem com corpo ÚTIL (texto não
    # vazio após trafilatura), CANCELANDO o resto — não espera as lentas/mortas. O
    # ranking (rankear_por_similaridade) roda só sobre os K aceitos, igual a antes; se
    # menos que K vierem, usa o que veio; se NADA, cai nos snippets. Para o pool ter o
    # que abrir, o DDG passa a pedir >= pool candidatos quando o fetch está ligado.
    web_fetch_pool: int = 12            # nº MÁX de fetches web concorrentes disparados
    web_fetch_aceitar: int = 3          # aceita os K 1ºs com corpo útil e cancela o resto
    # Timeout POR PÁGINA no race (asyncio.wait_for): um site travado vira None (não
    # contribui) em vez de segurar a coleta. Rede de segurança — como só esperamos os K
    # mais rápidos, ele raramente dispara. Mais curto que o web_fetch_timeout do client.
    web_fetch_timeout_s: float = 4.0
    # --- Filler contínuo da web (aceleração da PERCEPÇÃO de latência) -----------
    # O deep-fetch leva 3-12s; um filler fixo de ~3s (uma ponte só) deixava silêncio. Em
    # vez de UMA ponte e esperar, o _responder_web fala a 1ª ponte na hora e, ENQUANTO a
    # busca não volta, emite pontes curtas adicionais a cada `filler_intervalo_s`, até a
    # busca terminar ou bater `filler_max_pontes`. O filler dura ~o tempo real do fetch,
    # não um valor fixo. Barge-in corta (o pipeline é task cancelável). 0 = só a 1ª ponte.
    filler_max_pontes: int = 3          # teto de pontes ADICIONAIS além da 1ª
    filler_intervalo_s: float = 2.5     # intervalo entre pontes enquanto a busca roda
    # CARÊNCIA: desde o race-first-K a web ficou RÁPIDA (~3s, às vezes <1,5s). A 1ª ponte
    # falada na hora passou a ATROPELAR a resposta quando a busca já volta quase junto —
    # o dono ouvia "vou buscar..." e logo em cima a resposta real. Antes de falar QUALQUER
    # ponte, o _responder_web dá esta carência de silêncio à busca; se ela terminar dentro
    # da janela, PULA o filler inteiro e vai direto à resposta. 0 = sem carência (comportamento
    # antigo: 1ª ponte sempre imediata). Curto de propósito: acima disso o silêncio incomoda.
    filler_carencia_s: float = 1.5      # head-start de silêncio dado à busca antes da 1ª ponte
    # --- Guarda de Egressão (#6): anti-PII na query web ------------------------
    # A ÚNICA saída de rede é o WebSearcher. Antes de a query ir ao DDG, mascara
    # PII do dono (e-mail, CPF/CNPJ, cartão via Luhn, telefone) por um token
    # semântico ([email], [cpf], ...). Conservador de propósito p/ não estragar
    # busca legítima; toda máscara é logada. Desligue com MENTE_EGRESSAO_GUARDA=false.
    egressao_guarda: bool = True
    # --- Auto-recuperação de Índice (#33) --------------------------------------
    # Se o ChromaDB abrir corrompido (HNSW truncado, sqlite quebrado), o RAG local
    # morria em silêncio até um restart + apagar o banco na mão. Como o vault é a
    # FONTE DE VERDADE, a recuperação move o índice corrompido para o lado e
    # reconstrói do vault — SEM tocar o mtime dos .md (nada de reindex-ruim em
    # massa na origem). Best-effort, uma vez por processo. Desligue com false.
    indice_auto_recuperar: bool = True
    # --- Fila Offline / Disjuntor da web (#31) ---------------------------------
    # Circuit breaker anti-shadowban: N falhas de rede seguidas ABREM a busca web
    # por um cooldown; enquanto aberto, a query nem toca o DDG (não apanha ban) e
    # vai para uma fila em RAM, drenada (best-effort, p/ o cache) quando reabre.
    web_disjuntor_habilitado: bool = True
    web_disjuntor_limite_falhas: int = 3     # falhas seguidas p/ abrir
    web_disjuntor_cooldown_seg: float = 900  # ~15 min de descanso do DDG
    web_pendentes_max: int = 20              # teto da fila offline (em RAM)
    # --- Modo Econômico (#30): botão-mestre da feature --------------------------
    # Se false, o meta-comando "modo econômico" fica inerte (o gate nunca é
    # bypassado). Default on = feature disponível, mas SEMPRE opt-in por sessão.
    modo_economico_habilitada: bool = True
    # --- Anti-injeção no conteúdo web (#26) ------------------------------------
    # O deep-fetch passa corpo de página (dado NÃO-confiável) ao LLM. Trechos com
    # imperativos de override ("ignore as instruções anteriores") são DROPADOS antes
    # de entrar no contexto; os limpos seguem. Desligue com MENTE_ANTIINJECAO_WEB=false.
    antiinjecao_web: bool = True
    # --- Detector de Contradição (#24, no idle) --------------------------------
    # Após atomizar no idle, cada átomo novo é comparado com seu VIZINHO semântico
    # "relacionado mas distinto" (dist na banda [dedup_dist_max, contradicao_dist_max))
    # — é aí que mora a contradição. Um veredito LLM (preemptível) por par, capado por
    # ciclo. "mestre, alguma contradição?" reporta as achadas. Desligue com false.
    contradicao_detectar: bool = True
    contradicao_dist_max: float = 0.6        # teto da banda de "mesmo tema" (cosseno)
    contradicao_max_por_ciclo: int = 3       # tetos de chamadas LLM por atomização
    max_tokens_contradicao: int = 60         # veredito é curto (SIM/NAO + motivo)
    # --- Governador de VRAM (#28) + orçamento de tokens de fundo (#29) ----------
    # #28: o scheduler amostra o uso de VRAM a cada tick e AVISA se detectar
    # crescimento sustentado (vazamento). Só observa. #29: calibra quantos tokens
    # o trabalho de FUNDO (ETL, contradição) pode gerar pela fração de VRAM livre —
    # pouca folga -> gera menos, para não competir com a inferência interativa.
    vram_monitor_habilitado: bool = True
    vram_leak_amostras: int = 12             # janela do detector de vazamento
    vram_leak_slack_bytes: int = 400_000_000  # ~400 MB de crescimento p/ alertar
    vram_orcamento_base_tokens: int = 512    # teto de tokens de fundo com VRAM folgada
    vram_orcamento_min_tokens: int = 128     # piso de tokens de fundo com VRAM apertada
    vram_frac_min: float = 0.08              # abaixo disso, aperto (usa o piso)
    vram_frac_ok: float = 0.35               # acima disso, folga (usa a base)
    # --- Diapasão (#36): perfil de COMO o dono prefere conversar ---------------
    # No idle, destila da conversa uma diretriz de ESTILO (curto/detalhado, exemplos,
    # tom técnico) e a injeta na instrução de resposta — adapta o COMO, não imita o
    # tom, não muda o conteúdo. "mestre, como você me vê?" reporta. Desligue com false.
    diapasao_habilitado: bool = True
    max_tokens_perfil: int = 80              # o perfil é 1-2 frases
    # --- Fio da Conversa (#35) -------------------------------------------------
    # "mestre, onde paramos?" resgata o assunto de uma conversa anterior. Só conta
    # como fio uma conversa com >= fio_min_turnos trocas (assunto real, não um 'oi').
    fio_min_turnos: int = 2
    # --- Navegação por Voz (#14) -----------------------------------------------
    # "mestre, nova conversa" / "mostra o histórico" / "abre a conversa sobre X"
    # operam a UI: o backend manda {tipo:"navegar"} e o front executa. Desligue com false.
    navegacao_voz_habilitada: bool = True

    # --- Ferramentas (function calling aditivo) --------------------------------
    max_tokens_router: int = 60      # decisão do roteador é curta (JSON de 1 linha)
    # Loop agêntico CAPADO: nº máximo de ferramentas encadeadas por mensagem.
    # Ferramentas "terminais" (calcular, hora, salvar) já saem no 1º passo.
    max_tool_steps: int = 3

    # --- VAD / Áudio -----------------------------------------------------------
    vad_rms_threshold: float = 0.005    # servidor: início de fala
    vad_silence_seconds: float = 1.2    # servidor: fim de fala (teto/fallback)
    vad_min_frames: int = 15            # ignora ruídos curtos
    # BARGE-IN DO SERVIDOR (teste real 2507): cortar a resposta quando o dono fala POR
    # CIMA dela. O barge-in do front nem sempre disparava; este é a rede de segurança no
    # servidor. GUARD ANTI-ECO: como não há cancelamento de eco no servidor, o próprio TTS
    # captado pelo mic NÃO pode auto-cortar — por isso o limiar é ALTO (4x o VAD normal: a
    # voz direta no mic é bem mais forte que o eco pelo alto-falante) E precisa ser
    # SUSTENTADO por `barge_min_frames` consecutivos (um pico curto de eco não corta). Só
    # vale enquanto há resposta em voo. Se auto-cortar por eco: suba o threshold/frames;
    # se demorar a obedecer: baixe. Desligue com MENTE_BARGE_IN_SERVIDOR=false.
    barge_in_servidor: bool = True
    barge_rms_threshold: float = 0.02
    barge_min_frames: int = 8
    # ENDPOINTING ADAPTATIVO (consultoria TTFT #3): fala CURTA (comando, pergunta seca)
    # encerra com `vad_silence_curta_seconds` de silêncio em vez do teto cheio — os 1,2s
    # fixos eram pagos por TODO turno de voz e são maiores que o próprio TTFT do RAG.
    # Fala mais longa que `vad_fala_curta_seconds` mantém a margem cheia (pausa de
    # respiração no meio de um raciocínio não pode decapitar a frase). O ws loga
    # "retomada Nms após endpoint curto" quando a fala recomeça logo depois de um corte
    # curto — essa taxa de corte-precoce é o que decide se estes defaults apertam ou
    # relaxam (regra da consultoria: medir antes de cravar). Off = sempre o teto.
    vad_adaptativo: bool = True
    vad_silence_curta_seconds: float = 0.7
    vad_fala_curta_seconds: float = 3.0
    tts_chunk_min_chars: int = 8        # frase mínima antes de sintetizar
    tts_chunk_max_chars: int = 180      # flush forçado em frases longas
    # 1º CHUNK AGRESSIVO (consultoria TTFT #8): o TTFA espera a PRIMEIRA frase fechar —
    # a 180 chars e ~120 tok/s isso é >1s de decode antes do 1º áudio. Só no 1º chunk
    # da resposta, vírgula/;/: também fecham frase (o Piper pausa em vírgula: soa como
    # respiração, não corte) e o flush forçado usa esta janela menor. Do 2º chunk em
    # diante valem as regras de sempre. Nunca ALARGA a janela (é min() com o max acima).
    # 0 desliga (comportamento histórico). Vírgula decimal ("3,5") não fecha — a
    # fronteira exige espaço após a pontuação.
    tts_chunk_primeiro_max_chars: int = 60
    # Cache de voz (#1): nº de frases sintetizadas mantidas em RAM (LRU). Frase
    # recorrente (filler, confirmação, status) volta na hora, sem re-sintetizar.
    tts_cache_size: int = 256
    # Prosódia do Piper (naturalidade da fala): repassados ao SynthesisConfig da
    # síntese. None = default treinado da própria voz (comportamento histórico).
    # noise_scale = variação de entonação (mais alto = menos monótono; alto demais =
    # artefatos); noise_w_scale = variação de duração dos fonemas (ritmo);
    # length_scale = velocidade (>1.0 mais lento). Calibre de ouvido comparando os
    # WAVs de eval/amostras_tts.py.
    tts_noise_scale: float | None = None
    tts_noise_w_scale: float | None = None
    tts_length_scale: float | None = None

    # --- Engine de TTS: Piper (CPU) x XTTS-v2 (GPU) ----------------------------
    # "piper" (default) = VITS na CPU, baixa latência, voz do histórico. "xtts" =
    # XTTS-v2 (coqui-tts) na GPU: voz muito mais natural/clonável, mas PESA VRAM e
    # DISPUTA a GPU serializada do LLM (na 3080 compartilhada há contenção/risco de
    # OOM — daí o fp16 abaixo). Só troque com o modelo baixado e coqui-tts instalado.
    tts_engine: str = "piper"
    # Device do XTTS (auto/cuda/cuda:1/cpu), resolvido por rag.resolve_device. Use
    # "cuda:1" se um dia segmentar (LLM numa GPU, XTTS noutra). CPU é inviável (lento demais).
    tts_xtts_device: str = "cuda"
    # fp16 = MIXED PRECISION via torch.autocast (NÃO pesos half: o XTTS quebra com
    # model.half() — "expected scalar type Float but found Half" no layer_norm do GPT).
    # Ganho de compute/velocidade; os PESOS seguem em fp32, então NÃO reduz a VRAM pela
    # metade (o XTTS usa ~2-4GB de qualquer forma). Desligue p/ inferência fp32 pura.
    tts_xtts_fp16: bool = True
    # Voz: locutor EMBUTIDO do XTTS por nome (padrão). Para CLONAR, aponte
    # tts_xtts_speaker_wav para uma amostra .wav limpa de 6-30s (tem precedência).
    tts_xtts_speaker: str = "Ana Florence"
    tts_xtts_speaker_wav: str = ""
    tts_xtts_language: str = "pt"
    # inference_stream: nº de tokens GPT por chunk de áudio (menor = 1º som interno mais
    # cedo, mais overhead). Os chunks são juntados num WAV por frase.
    tts_xtts_stream_chunk_size: int = 20
    # enable_text_splitting: OFF — exige spacy[ja] (pesado) e o SentenceChunker do pipeline já
    # entrega frase a frase. O corte de segurança é feito EM CASA (_dividir_para_xtts) abaixo.
    tts_xtts_enable_text_splitting: bool = False
    # Teto de chars por frase mandada ao GPT-2 interno do XTTS. Frase acima disto é fatiada
    # (em fronteira de sentença/palavra) ANTES de gerar — senão os audio-tokens passam de
    # gpt_max_audio_tokens (~605/14s) e o índice de posição estoura (device-side assert que
    # corrompe o contexto CUDA e derruba o LLM junto). 0 desliga o corte.
    tts_xtts_max_chars_frase: int = 200
    # Cache LRU (reusa tts_cache_size): OFF porque o XTTS amostra (não-determinístico) —
    # cachear congelaria uma "tomada" aleatória. Ligue só se aceitar isso p/ frases fixas.
    tts_xtts_cache_enabled: bool = False
    # Amostragem (None = default treinado do XTTS): temperatura (~0.65), etc.
    tts_xtts_temperature: float | None = None
    tts_xtts_repetition_penalty: float | None = None
    tts_xtts_top_k: int | None = None
    tts_xtts_top_p: float | None = None
    # DeepSpeed: acelera ~2-3x o GPT, mas exige CUDA toolkit + MSVC (build frágil no
    # Windows). OFF por padrão; só ligue com um deepspeed compatível instalado.
    tts_xtts_use_deepspeed: bool = False

    # --- Fase de idle (inatividade -> ETL + pesquisa proativa -> unload) --------
    # Segundos de silêncio (chat aberto, mas parado) até entrar em idle: consolidar
    # conhecimento e liberar a GPU. Diferente do vad_silence (fim de FALA, ~1s); este
    # é fim de INTERAÇÃO. Uma nova mensagem/fala rearma. Maior = idle mais preguiçoso
    # (menos reloads, VRAM presa por mais tempo); menor = libera a GPU mais cedo.
    idle_inatividade_seconds: float = 90.0
    # Carência ANTES do idle de conhecimento pousar na GPU ao encerrar a conversa
    # (debounce). O ETL não roda no instante em que o chat fecha — espera este tanto SEM
    # nova atividade. Abrir outra conversa ou mandar um turno neste intervalo ADIA o idle
    # (os itens ficam bufferados, nada se perde), então dá pra encadear conversas sem a
    # GPU a 100% do ETL disputando o STT/decode do próximo turno. 0 = comportamento antigo
    # (idle imediato). Não afeta o idle por inatividade (esse já espera 90s parado).
    idle_grace_seconds: float = 20.0
    # Descarregar o Qwen ao fim do idle, liberando VRAM p/ outros apps? A 1ª mensagem
    # seguinte paga o reload (~1-2s). Desligue se a máquina é dedicada ao assistente.
    idle_descarregar_modelo: bool = True
    # PESQUISA PROATIVA: no idle, buscar na web as maiores LACUNAS (perguntas que a RAM
    # E o banco não responderam), atomizar e inserir — assim a próxima vez já acha local.
    # Autônomo: consome web e cresce a base sozinho. Desligue para pausar.
    idle_pesquisa_proativa: bool = True
    # Quantas lacunas pesquisar por ciclo de idle (cada uma: 1 busca web + 1 síntese).
    idle_pesquisa_max: int = 3
    # RE-PESQUISA DE TEMAS QUENTES (#4): além das LACUNAS (o que FALTOU), o idle também
    # re-pesquisa os temas que o usuário MAIS REUSA do vault (o estágio Banco respondeu de
    # fato) — sinal de interesse recorrente. Ao contrário da lacuna, aqui o banco JÁ cobre
    # o tema; o valor é a NOVIDADE que a web traz (o dedup por átomo descarta o já-sabido),
    # então a base "amadurece" nos temas favoritos, não só nos buracos. Desligue p/ pausar.
    idle_pesquisa_temas: bool = True
    # Quantos temas quentes re-pesquisar por ciclo (extra, DEPOIS das lacunas). Baixo de
    # propósito: buraco genuíno (lacuna) tem prioridade sobre refrescar o que já se sabe.
    idle_temas_max: int = 2
    # Mínimo de REUSOS (Banco respondeu) para um tema virar "quente" e valer re-pesquisa.
    # Separa o perguntado uma vez do interesse recorrente de verdade (evita re-pesquisar
    # curiosidade passageira). Maior = só favoritos consolidados.
    idle_temas_min_reuso: int = 3
    # Cooldown: não re-pesquisa o mesmo tema quente antes de N dias (evita spam de web e
    # de átomos sobre o favorito). ~quinzenal traz novidade sem reprocessar à toa.
    idle_temas_cooldown_dias: int = 14
    # Mínimo de keywords significativas para uma pergunta virar LACUNA pesquisável.
    # Sem isto, 'ok'/'sim' (falso-positivo do VAD/Whisper, 0 keywords) escalavam pra web
    # e a proativa pesquisava — medido: 8 átomos sobre a etimologia de "ok" no vault.
    lacuna_min_keywords: int = 2
    # Dedup do átomo novo contra o banco: distância de cosseno abaixo da qual o átomo é
    # considerado DUPLICADO e descartado. A ESCALA É DO EMBEDDING EM USO (consultoria
    # TTFT #2): o 0.08 histórico era "quase idêntico" no MiniLM, mas o e5-base comprime
    # as distâncias — o dry-run do painel (scripts/dedup_semantico.py, 12.947 chunks)
    # mediu p50 do vizinho cross-fonte = 0.045, então 0.08 marcaria 75% da base como
    # duplicata e o `_ja_no_banco` (etl.py) DESCARTAVA átomo novo legítimo em silêncio.
    # 0.01 é o "quase idêntico" da escala e5 (<0.01 = 57 de 12.947 fontes). Ao trocar
    # de embedding, recalibre isto JUNTO com o rag_score_confident — mesmo esquecimento,
    # mesma causa (a escala muda com o modelo).
    dedup_dist_max: float = 0.01

    # --- Palavra-mestre (fluxo isolado dos agentes) -----------------------------
    # Quando a mensagem COMEÇA por esta palavra, é tratada como COMANDO de agente
    # (fluxo determinístico, LLM só se necessário) — não como pergunta de conhecimento.
    # Sem ela, o pipeline de hoje não muda. Configurável por MENTE_PALAVRA_MESTRE.
    palavra_mestre: str = "mestre"
    palavra_mestre_habilitada: bool = True
    # F3 — WAKE-WORD "mestre" (modo tipo Alexa): quando LIGADO, a sessão live começa
    # DORMENTE e só processa fala DEPOIS de a palavra-mestre acordá-la; após
    # `mestre_sleep_seconds` de silêncio, dorme de novo (só "mestre" reacorda). Assim a
    # voz de outra pessoa por perto NÃO dispara nada. DESLIGADO (default) = sempre ativa,
    # como hoje. Gate no servidor: só vale com o live conectado (mic já streamando).
    # Ligue com MENTE_MESTRE_WAKE=true.
    mestre_wake: bool = False
    mestre_sleep_seconds: float = 15.0

    # --- Agentes / Scheduler (lembretes, alarmes, watchers, briefing) -----------
    # O SchedulerService é um loop de background que lê a tabela `agendamentos` e
    # dispara os vencidos. É a "responsabilidade contínua" dos agentes tipo-Alexa.
    scheduler_enabled: bool = True
    # Granularidade do loop: de quanto em quanto tempo checa a tabela. 20s dá precisão
    # de sub-minuto para lembretes sem custar quase nada (uma consulta SQLite indexada).
    scheduler_tick_seconds: float = 20.0
    # PESQUISA PROATIVA AGENDADA (#1 evolução, "cresce a noite toda"): gatilho POR TEMPO
    # que faz o scheduler rodar EtlProcessor.pesquisa_proativa() + pesquisa_temas_quentes()
    # mesmo SEM sessão aberta. O run_idle só dispara ao fim de uma conversa (oportunista);
    # sem isto a base não cresce enquanto o app fica ocioso. Intervalo entre passadas, em
    # segundos. 0 = DESLIGADO (default seguro; o idle por fim-de-sessão segue como o único
    # gatilho). Dispara em background (não atrasa o loop de alarmes) e nunca concorre com
    # sessão viva. Ex.: MENTE_PESQUISA_AGENDADA_INTERVALO_SEGUNDOS=7200 (a cada 2h).
    pesquisa_agendada_intervalo_seconds: int = 0
    # ACK de aplicação do push proativo (painel 2026-07): o disparo só é CONCLUÍDO
    # quando o cliente confirma que exibiu ({"tipo":"ack_proativo"}); sem ack neste
    # prazo, volta à reentrega normal (pendente_entrega). O send "com sucesso" num
    # TCP meio-aberto bufferiza e mente — por isso a confirmação é do CLIENTE.
    proativo_ack_timeout_seconds: float = 30.0
    # Watcher "me avise quando": intervalo padrão entre checagens da condição na web.
    # Cada checagem gasta 1 busca web + 1 inferência (preemptível, cede à conversa).
    watcher_intervalo_seconds: int = 1800     # 30 min
    # Teto de vida de um watcher: expira sozinho depois disso (não fica checando a web
    # para sempre por uma condição que talvez nunca ocorra).
    watcher_expira_horas: int = 168           # 7 dias
    # Briefing diário: horário padrão (HH:MM) quando o usuário não especifica.
    briefing_hora_padrao: str = "07:00"
    # Pasta das listas do "Agente de Listas" (compras/tarefas), dentro do vault.
    subpasta_listas: str = "Listas"
    # LEITOR DE AGENDA .ics (#40): 100% LOCAL. Aponte para uma pasta (dentro do vault por
    # default) com .ics exportados do seu calendário (Google/Outlook). O app lê os
    # compromissos de HOJE para "mestre, o que tenho hoje" e para o briefing. Nada vai à nuvem.
    subpasta_agenda: str = "Agenda"
    # COFRE DE CONFIRMAÇÃO (#25): ações destrutivas E não-desfazíveis (hoje só
    # cancelar_lembrete — o undo #8 não recria um lembrete) exigem um "mestre, confirma"
    # antes de rodar. As ações que o undo cobre (add/remove) NÃO são gateadas — confirmar
    # o desfazível seria a "confirmação redundante" (#15) que se quer evitar. Desligue com
    # MENTE_CONFIRMACAO_HABILITADA=false para executar direto.
    confirmacao_habilitada: bool = True
    # ATALHO DE INTENÇÃO FREQUENTE (#2): quantas vezes a MESMA intenção-mestre (forma
    # normalizada) precisa se repetir para o app OFERECER um atalho nomeado. A oferta
    # acontece UMA vez por intenção. 0/negativo desliga a sugestão (só conta).
    atalho_sugestao_min: int = 3
    # DESCOBRIDOR DE CONEXÕES (G8, Onda 3): "mestre, alguma conexão nova?" acha PONTES no
    # vault — notas que ligam dois conceitos ESTABELECIDOS (cada um em >= conexao_df_min
    # átomos) que quase nunca co-ocorrem (<= conexao_coocorrencia_max átomos juntos). É o
    # "descobridor de conexões" (#22) por DEMANDA, sem push. Roda sobre a malha de conceitos.
    # Menor df_min = temas menos consolidados entram; maior coocorrencia_max = pontes menos
    # "surpreendentes" (temas que já se cruzam mais). limite = quantas pontes a fala traz.
    conexao_df_min: int = 3
    conexao_coocorrencia_max: int = 1
    conexao_limite: int = 3

    # --- SRS: repetição espaçada (#43) -----------------------------------------
    # "mestre, revisa isso" cria um card da última troca; "mestre, revisão" puxa os cards
    # vencidos (SOB DEMANDA, sem push). Leitner: acerto avança a caixa (intervalo maior),
    # erro volta à primeira. Intervalos em DIAS por caixa (0 a N). Persistente no SQLite.
    srs_intervalos_dias: list[int] = [1, 3, 7, 16, 35]
    srs_max_por_sessao: int = 10   # teto de cards por sessão de revisão (não cansar)

    # --- Pomodoro (#19) --------------------------------------------------------
    # Ciclo foco/pausa anunciado por voz (via SchedulerService, tipo 'pomodoro'): quando o
    # foco acaba avisa a pausa; quando a pausa acaba, volta ao foco. Cicla até "para o
    # pomodoro". Minutos por fase.
    pomodoro_foco_min: int = 25
    pomodoro_pausa_min: int = 5

    # --- Limites de memória (evitam crescimento sem fim na RAM) -----------------
    max_chat_history: int = 50
    max_session_knowledge: int = 12
    max_etl_queue: int = 64
    max_web_cache: int = 128

    # --- Servidor --------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    # TLS opcional (painel 2026-07): com AMBOS os caminhos, o servidor sobe em HTTPS/
    # WSS. Destrava a VOZ no celular pela LAN — getUserMedia (microfone) exige
    # "secure context" fora de localhost, então sem HTTPS a voz remota nem liga — e
    # cifra transcrições/trechos do vault em trânsito. Gere o par com
    # scripts/gerar_cert.py (mkcert = CA local, sem aviso no browser). Vazio = HTTP
    # (comportamento atual). O front já deriva ws/wss de location.protocol sozinho.
    ssl_cert: str = ""
    ssl_key: str = ""
    # Acesso (painel 2026-07, #7): VAZIO = só loopback usa as rotas /api e o WS (a
    # LAN não alcança nada — nem leitura do vault, nem o POST que vira átomo).
    # Defina um segredo (ex.: 48 hex aleatórios) para liberar outros aparelhos;
    # no aparelho, abra a página uma vez com ?token=SEGREDO (o front guarda).
    access_token: str = ""

    # --- Instrumentação de latência (trace por turno + saneamento de outliers) --
    # O dono pediu "coletar quanto tempo cada ação demorou (inferência ou espera),
    # para testes futuros". Estes botões são ADITIVOS e best-effort: NÃO mudam nenhum
    # comportamento de runtime (ordem, device, engine), só ligam a coleta e limpam a
    # estatística de artefatos de relógio-de-parede.
    # TRACE por turno: grava UMA linha JSONL por resposta (a timeline de marcos +
    # todos os tempos por estágio) em trace_dir/AAAAMMDD.jsonl (rotacionado por dia).
    # OFF por padrão — ligue com MENTE_TRACE_ENABLED=true numa sessão de medição; o
    # dump roda em thread e um erro nele nunca afeta a resposta.
    trace_enabled: bool = False
    trace_dir: str = str(DIR_DADOS / "traces")
    # Tetos de sanidade dos percentis/médias: uma resposta com TTFT ou tok/s acima
    # destes é ARTEFATO (relógio de parede poluído por idle/reload frio — ex.: o id272
    # com ttft=346800ms/tok_s=2142 envenenava p95/AVG) e é EXCLUÍDA da agregação. A
    # linha crua PERMANECE no banco (nada é apagado); só não entra na estatística.
    # Calibre se um caso legítimo lento passar a cair fora.
    latencia_ttft_teto_ms: float = 120000.0
    latencia_tok_s_teto: float = 300.0

    # --- Derivados -------------------------------------------------------------
    @property
    def dir_conhecimento_novo(self) -> Path:
        return Path(self.caminho_obsidian) / self.subpasta_conhecimento_novo

    @property
    def dir_listas(self) -> Path:
        return Path(self.caminho_obsidian) / self.subpasta_listas

    @property
    def dir_agenda(self) -> Path:
        return Path(self.caminho_obsidian) / self.subpasta_agenda

    @property
    def arquivo_inbox(self) -> Path:
        # Captura Rápida (GTD): tudo que o usuário "anota rápido" cai aqui, cru, com
        # carimbo de tempo. Fica no vault (indexado, pesquisável); o ritual de revisão
        # é trabalho do idle (destilar a inbox em átomos), não do momento da captura.
        return Path(self.caminho_obsidian) / "Inbox_Captura.md"

    def ensure_dirs(self) -> None:
        """Cria as pastas necessárias. Chamado no startup, nunca no import."""
        # dados/ primeiro: é o pai do SQLite e do chat_dump (arquivos soltos que o
        # Database/etl abrem com open() sem criar a pasta). As demais pastas abaixo
        # já ficam sob dados/, mas garantimos a raiz de dados explicitamente.
        os.makedirs(DIR_DADOS, exist_ok=True)
        os.makedirs(self.diretorio_banco_vetorial, exist_ok=True)
        os.makedirs(self.caminho_obsidian, exist_ok=True)
        os.makedirs(self.dir_conhecimento_novo, exist_ok=True)
        os.makedirs(self.dir_listas, exist_ok=True)
        os.makedirs(self.dir_agenda, exist_ok=True)
        # Pastas dos modelos: garantem que o local de download do Whisper e o
        # destino esperado do LLM/voz existam mesmo num clone recém-feito.
        os.makedirs(DIR_MODELOS, exist_ok=True)
        os.makedirs(self.caminho_cache_whisper, exist_ok=True)


settings = Settings()


# ==========================================================================
# DICIONÁRIO FONÉTICO (INGLÊS -> PT-BR) — usado pelo TTS (Piper)
# ==========================================================================
DICIONARIO_FONETICO: dict[str, str] = {
    "software": "sóft-uér",
    "hardware": "rárd-uér",
    "duckduckgo": "dãquidãqui gou",
    "fastapi": "fést ei pi ai",
    "python": "páiton",
    "llm": "éle éle ême",
    "rag": "rágui",
    "chromadb": "crôma di bi",
    "whisper": "uísper",
    "obsidian": "obsídian",
    "insight": "insáit",
    "download": "daunlôud",
    "update": "apidêit",
    "web": "uébi",
    "bug": "bãgui",
    "backend": "béqui éndi",
    "frontend": "frónti éndi",
    "streaming": "istrímin",
    "pipeline": "paipi láini",
}
