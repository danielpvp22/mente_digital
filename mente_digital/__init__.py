"""Mente Digital — assistente Omni local (voz + texto).

Pacote raiz do aplicativo. Os módulos de domínio (agent, rag, llm, ws, etl, ...)
vivem aqui e se importam por caminho absoluto de pacote (``mente_digital.<mod>``).
O entrypoint é ``mente_digital.main`` (``python -m mente_digital.main`` ou
``uvicorn mente_digital.main:app``).
"""
