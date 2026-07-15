# modelos/

Pasta local dos modelos de IA. Os binários **não vão para o git** (são grandes) —
só a estrutura de pastas é versionada. Coloque os arquivos aqui manualmente após o
clone.

```
modelos/
├── Qwen2.5-Coder-7B-Instruct-Uncensored.Q4_K_M.gguf   # LLM (~4.7 GB)
├── pt_BR-cadu-medium.onnx                              # voz TTS (Piper)
├── pt_BR-cadu-medium.onnx.json                         # config da voz (junto do .onnx)
└── whisper/                                            # cache do STT (baixa sozinho)
```

## O que baixar

- **LLM** — `Qwen2.5-Coder-7B-Instruct-Uncensored.Q4_K_M.gguf`.
- **Voz Piper** — `pt_BR-cadu` (medium), do repositório oficial
  [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) em
  `pt/pt_BR/cadu/medium/`. Baixe o `.onnx` **e** o `.onnx.json`.
- **Whisper (STT)** — não precisa baixar à mão: o `faster-whisper` baixa os pesos
  para `modelos/whisper/` na 1ª execução (controlado por `caminho_cache_whisper`).

## Apontar para outro lugar

Os caminhos default são relativos a esta pasta. Para usar modelos que já estão em
outro diretório, sobrescreva no `.env` (sem tocar no código):

```
MENTE_CAMINHO_MODELO_LLAMA=D:\outro\caminho\modelo.gguf
MENTE_CAMINHO_VOZ_PIPER=D:\outro\caminho\voz.onnx
MENTE_CAMINHO_CACHE_WHISPER=D:\outro\caminho\whisper
```
