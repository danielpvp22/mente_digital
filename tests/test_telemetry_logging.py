"""Robustez do logging (evolução #2): o telemetry NUNCA pode derrubar o processo
por causa de encoding.

Bug real visto ao vivo: um átomo do vault com char fora do Latin-1 (ex.: vietnamita)
chegou ao log; o `sys.stdout` do Windows é cp1252 e o `write` estourou
`UnicodeEncodeError`, matando a busca local no meio do turno. Logging é
instrumentação — jamais deve ter poder de quebrar o pipeline.
"""
from __future__ import annotations

import io

from mente_digital.telemetry import TelemetryStream


class _StreamCp1252:
    """Fake de stdout do Windows: só aceita o que couber em cp1252 (como o real)."""

    encoding = "cp1252"

    def __init__(self) -> None:
        self.buffer_txt = ""

    def write(self, text: str) -> int:
        # Reproduz o TextIOWrapper cp1252: encode estoura em char fora do codec.
        # O que passa é registrado (para provar que sanitizou, não engoliu).
        text.encode("cp1252")  # levanta UnicodeEncodeError se não couber
        self.buffer_txt += text
        return len(text)

    def flush(self) -> None:  # pragma: no cover - trivial
        pass


def test_track_nao_levanta_com_char_fora_do_cp1252(monkeypatch):
    # Arrange: stdout que rejeita char não-cp1252 e uma mensagem vietnamita.
    stream = _StreamCp1252()
    monkeypatch.setattr("mente_digital.telemetry.sys.stdout", stream)
    tel = TelemetryStream()
    mensagem = "átomo do vault: Tiếng Việt — Hà Nội"  # 'ế' não existe em cp1252

    # Act + Assert: não pode propagar exceção.
    tel.track("LOCAL_DBG", mensagem)

    # E algo foi de fato escrito (sanitizado), não silenciosamente engolido.
    assert stream.buffer_txt != ""
    assert "LOCAL_DBG" in stream.buffer_txt


def test_error_com_traceback_nao_levanta_com_char_fora_do_cp1252(monkeypatch):
    # Arrange: stderr cp1252 e uma exceção cuja mensagem tem char não-Latin-1.
    stderr = _StreamCp1252()
    monkeypatch.setattr("mente_digital.telemetry.sys.stderr", stderr)
    # stdout também precisa aguentar (track roda primeiro dentro do error).
    monkeypatch.setattr("mente_digital.telemetry.sys.stdout", _StreamCp1252())
    tel = TelemetryStream()
    exc = ValueError("falhou no átomo Tiếng Việt")

    # Act + Assert: o dump de traceback não pode derrubar nada.
    tel.error("RAG", "erro ao indexar", exc)

    assert stderr.buffer_txt != ""


def test_safe_write_preserva_texto_ascii():
    # Um stream utf-8 normal recebe o texto intacto (a sanitização só age no erro).
    stream = io.StringIO()
    TelemetryStream._safe_write(stream, "linha normal\n")
    assert stream.getvalue() == "linha normal\n"
