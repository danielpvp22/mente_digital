"""
Fakes e fixtures compartilhados.

Objetivo: testar a LÓGICA do pipeline sem GPU, sem modelos e sem rede. Os fakes
abaixo substituem o LLM (stream de tokens controlado), o TTS (no-op) e o vector
store (resultados fixos), então cada teste exercita só a decisão do código.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

# ISOLAMENTO DO SQLITE REAL (consultoria TTFT #5, endurecido com o resgate do
# worktree awesome-swirles): o `telemetry.db` global nasce no IMPORT apontando para
# MENTE_DB_TELEMETRIA — redirecionar AQUI, antes de qualquer import do projeto, é o
# único jeito de a suíte não gravar lacunas/latência/log_etl no telemetria_etl.db de
# produção (medido: 3 lacunas-fixture no topo da fila da pesquisa proativa).
# Atribuição DURA, não setdefault: uma MENTE_DB_TELEMETRIA herdada do shell
# apontaria a suíte de volta ao banco real — o redirect tem que vencer sempre.
if "mente_digital.config" in sys.modules:
    raise RuntimeError(
        "conftest: 'mente_digital.config' foi importado antes do redirect de "
        "MENTE_DB_TELEMETRIA — o db global nasceria no telemetria_etl.db real. "
        "Mantenha este bloco acima de qualquer import do projeto."
    )
_TMP_DB_DIR = tempfile.mkdtemp(prefix="mente_pytest_")
os.environ["MENTE_DB_TELEMETRIA"] = os.path.join(_TMP_DB_DIR, "telemetria_teste.db")
atexit.register(shutil.rmtree, _TMP_DB_DIR, ignore_errors=True)  # não vazar tmp por run

from typing import List, Optional, Tuple

import pytest

from mente_digital.config import Settings
from mente_digital.config import settings as _settings

# Defaults LIMPOS do código (ignora o .env) — a suíte de lógica assume estes valores.
_DEFAULTS_LIMPOS = Settings(_env_file=None)

# Tabelas no banco ISOLADO do redirect acima: sem isto os writes best-effort dos
# testes (save_lacuna, save_latency...) falhariam em silêncio por falta de schema —
# e os testes que LEEM o que gravaram passariam por motivo errado.
from mente_digital.telemetry import db as _db_teste  # noqa: E402  (precisa vir DEPOIS do redirect)

_db_teste.init()


@pytest.fixture(autouse=True)
def _isola_do_env(monkeypatch):
    """Isola a suíte do .env de PRODUÇÃO.

    Os testes de gate/malha assumem os DEFAULTS do código (ver
    test_defaults_de_calibracao_intactos), mas o `settings` global lê o `.env` no
    import — então um .env real com e5 (rag_score_confident=0.16, prefixos
    query:/passage:) quebrava o gate (distâncias de escala antiga) e a malha. Aqui os
    campos .env-sensíveis voltam ao default por teste; quem precisa de outro valor
    sobrescreve depois (o monkeypatch do próprio teste roda DEPOIS deste, então vence)."""
    for campo in ("rag_score_confident", "embedding_query_prefix", "embedding_passage_prefix",
                  "llm_no_think", "llm_strip_think"):
        monkeypatch.setattr(_settings, campo, getattr(_DEFAULTS_LIMPOS, campo), raising=False)
    # Backup diário (ops-backup-01): default LIGADO em produção, mas um tick de
    # scheduler dentro da suíte zipar o vault REAL seria efeito colateral em disco.
    # Off aqui; o módulo backup é testado direto (test_backup.py) com tmp_path.
    monkeypatch.setattr(_settings, "backup_habilitado", False, raising=False)
    yield


# ==========================================================================
# LLM falso — emite uma sequência de tokens pré-definida
# ==========================================================================
class FakeLlama:
    """Substitui LlamaManager: `stream` devolve os tokens dados, na ordem.

    Também imita o contrato de PREEMPÇÃO do LlamaManager real: `preempt()` marca os
    streams `preemptible=True` em curso, que então levantam InferenciaPreemptada —
    como o decode de verdade faria ao ceder a GPU para uma pergunta do usuário.
    """

    def __init__(self, tokens: List[str]) -> None:
        self.tokens = tokens
        self.preempcoes = 0
        self._preemptar_proximo = False

    def preempt(self) -> int:
        self.preempcoes += 1
        return 0

    def armar_preempcao(self) -> None:
        """A PRÓXIMA stream preemptible será cortada (simula o usuário perguntando
        no meio de uma síntese de ETL)."""
        self._preemptar_proximo = True

    async def stream(self, prompt: str, **kwargs):
        from mente_digital.llm import InferenciaPreemptada

        if kwargs.get("preemptible") and self._preemptar_proximo:
            self._preemptar_proximo = False
            if self.tokens:
                yield self.tokens[0]          # alcançou a decodificar um pedaço...
            raise InferenciaPreemptada("preempção simulada")   # ...e foi cortada
        for tok in self.tokens:
            yield tok

    async def collect(self, prompt: str, **kwargs) -> str:
        return "".join([tok async for tok in self.stream(prompt, **kwargs)])


# ==========================================================================
# TTS falso — não sintetiza nada (retorna None => o _falar pula o áudio)
# ==========================================================================
class FakeTts:
    def __init__(self) -> None:
        self.chamadas: List[str] = []

    async def synth_base64(self, texto: str) -> Optional[str]:
        self.chamadas.append(texto)
        return None

    def cancel(self) -> None:
        # Contrato do TTS (barge-in): existe em ambos os engines reais; no-op no fake.
        pass


# ==========================================================================
# Documento / store falsos para a busca local (sem ChromaDB)
# ==========================================================================
class FakeDoc:
    def __init__(self, content: str, metadata: Optional[dict] = None) -> None:
        self.page_content = content
        self.metadata = metadata or {}


class FakeStore:
    """Devolve pares (doc, score) fixos, imitando ChromaDB."""

    def __init__(self, results: List[Tuple[FakeDoc, float]]) -> None:
        self._results = results

    def similarity_search_with_score(self, termos: str, k: int):
        return self._results[:k]


# ==========================================================================
# Coletor de mensagens enviadas (o callback `send` do pipeline)
# ==========================================================================
def make_send():
    """Devolve (send, enviados): `send` é o callback async, `enviados` a lista."""
    enviados: List[dict] = []

    async def send(data: dict) -> bool:
        enviados.append(data)
        return True

    return send, enviados


def textos_de_tokens(enviados: List[dict]) -> str:
    """Concatena o texto de todas as mensagens do tipo 'token'."""
    return "".join(m["texto"] for m in enviados if m.get("tipo") == "token")
