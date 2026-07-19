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

# Raiz do projeto = pasta deste arquivo. TODOS os caminhos default são derivados
# daqui (não de caminhos absolutos de uma máquina específica), então o projeto
# roda de qualquer diretório e em qualquer máquina, sem editar código. Cada campo
# ainda pode ser sobrescrito por .env / variável de ambiente (prefixo MENTE_).
BASE_DIR = Path(__file__).resolve().parent
# Modelos de IA (LLM .gguf, voz Piper) e cache do Whisper ficam versionados como
# pastas (com .gitkeep), mas os binários em si não vão pro git — ver .gitignore.
DIR_MODELOS = BASE_DIR / "modelos"
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
    caminho_modelo_llama: str = str(DIR_MODELOS / "Qwen2.5-7B-Instruct-Q4_K_M.gguf")
    caminho_voz_piper: str = str(DIR_MODELOS / "pt_BR-cadu-medium.onnx")
    # Cache onde o faster-whisper baixa os pesos do Whisper na 1ª execução.
    caminho_cache_whisper: str = str(DIR_WHISPER)
    # Vault Obsidian: default dentro do projeto (pode começar vazio); aponte para
    # o seu vault real via MENTE_CAMINHO_OBSIDIAN no .env.
    caminho_obsidian: str = str(BASE_DIR / "Cerebro_Digital")
    diretorio_banco_vetorial: str = str(BASE_DIR / "banco_vetorial_cerebro")
    arquivo_chat_dump: str = str(BASE_DIR / "chat_dump_bruto.md")
    db_telemetria: str = str(BASE_DIR / "telemetria_etl.db")
    subpasta_conhecimento_novo: str = "Conhecimento_Novo"

    # --- LLM (GPU) -------------------------------------------------------------
    n_gpu_layers: int = -1
    n_ctx: int = 8192
    temperatura_resposta: float = 0.2
    max_tokens_resposta: int = 800
    max_tokens_query: int = 15
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
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # "auto" = usa a GPU (cuda) se disponível, senão CPU. O embedding da query está
    # no caminho crítico de TODA pergunta, então a GPU baixa a latência por-pergunta
    # (e acelera a reindexação). Force com MENTE_EMBEDDING_DEVICE=cpu se precisar.
    embedding_device: str = "auto"

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
    web_max_results: int = 4
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
    web_fetch_pages: int = 3            # quantas URLs do resultado abrir de fato
    web_fetch_timeout: float = 6.0      # timeout por página (s) — não travar o TTFA
    web_fetch_max_chars: int = 20000    # teto de texto extraído por página (anti-lixo)
    web_chunk_size: int = 600           # tamanho do átomo efêmero (chars)
    web_chunk_overlap: int = 80
    web_rank_top_k: int = 12            # nº de trechos rankeados que entram no contexto
    # Orçamento de chars do contexto web montado (protege o n_ctx, como o do RAG local).
    web_context_char_budget: int = 6000

    # --- Ferramentas (function calling aditivo) --------------------------------
    max_tokens_router: int = 60      # decisão do roteador é curta (JSON de 1 linha)
    # Loop agêntico CAPADO: nº máximo de ferramentas encadeadas por mensagem.
    # Ferramentas "terminais" (calcular, hora, salvar) já saem no 1º passo.
    max_tool_steps: int = 3

    # --- VAD / Áudio -----------------------------------------------------------
    vad_rms_threshold: float = 0.005    # servidor: início de fala
    vad_silence_seconds: float = 1.2    # servidor: fim de fala
    vad_min_frames: int = 15            # ignora ruídos curtos
    tts_chunk_min_chars: int = 8        # frase mínima antes de sintetizar
    tts_chunk_max_chars: int = 180      # flush forçado em frases longas
    # Cache de voz (#1): nº de frases sintetizadas mantidas em RAM (LRU). Frase
    # recorrente (filler, confirmação, status) volta na hora, sem re-sintetizar.
    tts_cache_size: int = 256

    # --- Fase de idle (inatividade -> ETL + pesquisa proativa -> unload) --------
    # Segundos de silêncio (chat aberto, mas parado) até entrar em idle: consolidar
    # conhecimento e liberar a GPU. Diferente do vad_silence (fim de FALA, ~1s); este
    # é fim de INTERAÇÃO. Uma nova mensagem/fala rearma. Maior = idle mais preguiçoso
    # (menos reloads, VRAM presa por mais tempo); menor = libera a GPU mais cedo.
    idle_inatividade_seconds: float = 90.0
    # Descarregar o Qwen ao fim do idle, liberando VRAM p/ outros apps? A 1ª mensagem
    # seguinte paga o reload (~1-2s). Desligue se a máquina é dedicada ao assistente.
    idle_descarregar_modelo: bool = True
    # PESQUISA PROATIVA: no idle, buscar na web as maiores LACUNAS (perguntas que a RAM
    # E o banco não responderam), atomizar e inserir — assim a próxima vez já acha local.
    # Autônomo: consome web e cresce a base sozinho. Desligue para pausar.
    idle_pesquisa_proativa: bool = True
    # Quantas lacunas pesquisar por ciclo de idle (cada uma: 1 busca web + 1 síntese).
    idle_pesquisa_max: int = 3
    # Mínimo de keywords significativas para uma pergunta virar LACUNA pesquisável.
    # Sem isto, 'ok'/'sim' (falso-positivo do VAD/Whisper, 0 keywords) escalavam pra web
    # e a proativa pesquisava — medido: 8 átomos sobre a etimologia de "ok" no vault.
    lacuna_min_keywords: int = 2
    # Dedup do átomo novo contra o banco: distância de cosseno abaixo da qual o átomo é
    # considerado DUPLICADO e descartado. Conservador (0.08 ≈ quase idêntico) para não
    # podar átomo legitimamente distinto — impede duplicação sem matar cobertura.
    dedup_dist_max: float = 0.08

    # --- Palavra-mestre (fluxo isolado dos agentes) -----------------------------
    # Quando a mensagem COMEÇA por esta palavra, é tratada como COMANDO de agente
    # (fluxo determinístico, LLM só se necessário) — não como pergunta de conhecimento.
    # Sem ela, o pipeline de hoje não muda. Configurável por MENTE_PALAVRA_MESTRE.
    palavra_mestre: str = "mestre"
    palavra_mestre_habilitada: bool = True

    # --- Agentes / Scheduler (lembretes, alarmes, watchers, briefing) -----------
    # O SchedulerService é um loop de background que lê a tabela `agendamentos` e
    # dispara os vencidos. É a "responsabilidade contínua" dos agentes tipo-Alexa.
    scheduler_enabled: bool = True
    # Granularidade do loop: de quanto em quanto tempo checa a tabela. 20s dá precisão
    # de sub-minuto para lembretes sem custar quase nada (uma consulta SQLite indexada).
    scheduler_tick_seconds: float = 20.0
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
