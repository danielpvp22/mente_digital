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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MENTE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Caminhos (defaults do seu ambiente Windows) ---------------------------
    caminho_modelo_llama: str = r"D:\projetos\modelos\Qwen2.5-Coder-7B-Instruct-Uncensored.Q4_K_M.gguf"
    caminho_voz_piper: str = r"D:\projetos\modelos\pt_BR-cadu-medium.onnx"
    caminho_obsidian: str = r"C:\Users\User\Desktop\projetos\memoria_vetorial\Cerebro_Digital"
    diretorio_banco_vetorial: str = "./banco_vetorial_cerebro"
    arquivo_chat_dump: str = "chat_dump_bruto.md"
    db_telemetria: str = "telemetria_etl.db"
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

    # --- STT / Embeddings ------------------------------------------------------
    whisper_model: str = "small"
    whisper_device: str = "cpu"
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
