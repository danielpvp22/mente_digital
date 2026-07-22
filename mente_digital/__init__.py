"""Mente Digital — assistente Omni local (voz + texto).

Pacote raiz do aplicativo. Os módulos de domínio (agent, rag, llm, ws, etl, ...)
vivem aqui e se importam por caminho absoluto de pacote (``mente_digital.<mod>``).
O entrypoint é ``main.py`` na RAIZ do repo (``python main.py`` ou
``uvicorn main:app``); ele importa este pacote por caminho absoluto.
"""
