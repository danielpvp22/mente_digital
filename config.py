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
    caminho_modelo_llama: str = str(DIR_MODELOS / "Qwen2.5-Coder-7B-Instruct-Uncensored.Q4_K_M.gguf")
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
    max_tokens_filler: int = 30
    max_tokens_query: int = 15
    max_tokens_sintese: int = 1600
    max_tokens_resumo: int = 1800

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
    rag_top_k: int = 3
    rag_score_max: float = 1.5          # distância máxima p/ um chunk ser exibível
    # PRINCIPAL BOTÃO DE CALIBRAÇÃO: distância abaixo da qual um match é "confiante"
    # o bastante para valer como Cache Hit MESMO sem casar palavra-chave. Ajuste
    # olhando o log "[LOCAL] melhor_dist=..." com os seus próprios dados.
    rag_score_confident: float = 0.8
    chunk_size: int = 1000
    chunk_overlap: int = 150
    chroma_batch: int = 2000
    web_max_results: int = 4
    web_prefetch_results: int = 3
    # Fallback de busca: tenta cada backend do ddgs em ordem até um dar resultado.
    web_backends: list[str] = ["auto", "html", "lite"]

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

    def ensure_dirs(self) -> None:
        """Cria as pastas necessárias. Chamado no startup, nunca no import."""
        os.makedirs(self.diretorio_banco_vetorial, exist_ok=True)
        os.makedirs(self.caminho_obsidian, exist_ok=True)
        os.makedirs(self.dir_conhecimento_novo, exist_ok=True)
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
