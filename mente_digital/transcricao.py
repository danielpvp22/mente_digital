"""
Gravação COMPLETA do turno: o que o app fez por dentro e o que o usuário VIU.

Pedido do dono em 2026-07-29, depois do primeiro teste guiado por roteiro. O log
de telemetria conta o comportamento (rota, distância, figuras entregues) mas não
guarda uma linha do que apareceu na tela; o SQLite guarda a resposta em texto mas
não as IMAGENS nem onde elas caíram. Nenhuma das duas fontes respondia às três
perguntas que ele realmente tinha: *qual* imagem apareceu, se ela tinha a ver com
a pergunta, e por que a mesma sequência de imagens se repetia entre perguntas
diferentes.

A gravação mora no `safe_send` porque ele é a ÚNICA porta de saída para o front —
todo token, status e áudio passa por ali. Gravando nesse ponto, o registro não é
uma reconstrução do que o servidor *acha* que mandou: é o que ele mandou.

O áudio entra só como TAMANHO. O base64 de um turno passa de um megabyte e o
valor de diagnóstico é zero — o que importa é que houve áudio e quanto.
"""
from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# `![[Figuras/livro/livro_p0122_f1.webp]]` — o mesmo embed que o vault usa.
_EMBED_RE = re.compile(r"!\[\[([^\[\]]+?)\]\]")


def imagens_em(texto: str) -> List[Dict[str, Any]]:
    """As imagens que o usuário viu, em ORDEM e com a posição em chars.

    É a régua do inline sem precisar instrumentar o emissor: a posição sai do
    próprio texto final. `char` é onde a imagem começa; comparado com o tamanho
    total do texto, diz se ela caiu junto do trecho que a menciona ou no rodapé.

    Puro/testável — não toca disco nem estado.
    """
    return [
        {"arquivo": Path(m.group(1)).stem, "char": m.start()}
        for m in _EMBED_RE.finditer(texto)
    ]


def montar_registro(pergunta: str, conversa_id: Optional[str],
                    eventos: List[Dict[str, Any]], quando: str) -> Dict[str, Any]:
    """O registro do turno a partir dos eventos crus. Puro/testável.

    `resposta` é a concatenação dos tokens VISÍVEIS, ou seja, o texto exatamente
    como ficou na tela — embeds de imagem incluídos, no lugar em que apareceram.
    """
    visivel = "".join(e.get("texto", "") for e in eventos if e.get("tipo") == "token")
    return {
        "quando": quando,
        "conversa_id": conversa_id,
        "pergunta": pergunta,
        "resposta": visivel,
        "chars": len(visivel),
        "imagens": imagens_em(visivel),
        "eventos": eventos,
    }


class GravadorTurno:
    """Acumula um turno e o escreve como UMA linha JSON no fim.

    Fail-soft por princípio: gravação é instrumentação e não pode ter poder de
    derrubar um turno. Qualquer erro de disco é engolido aqui — o `telemetry`
    segue sendo o canal de erro do resto do app.
    """

    def __init__(self, caminho: str | Path) -> None:
        self._caminho = Path(caminho)
        self._pergunta: Optional[str] = None
        self._conversa_id: Optional[str] = None
        self._eventos: List[Dict[str, Any]] = []

    @property
    def aberto(self) -> bool:
        return self._pergunta is not None

    def abrir(self, pergunta: str, conversa_id: Optional[str] = None) -> None:
        self._pergunta = pergunta
        self._conversa_id = conversa_id
        self._eventos = []

    def ver(self, data: Dict[str, Any]) -> None:
        """Registra um envio ao front. Chamado de dentro do `safe_send`."""
        if self._pergunta is None:
            return                      # push proativo fora de turno: não é resposta
        tipo = data.get("tipo")
        if tipo == "audio":
            # só o TAMANHO: o base64 de um turno passa de 1 MB e não diagnostica nada.
            self._eventos.append({"tipo": "audio", "bytes": len(data.get("base64") or "")})
        elif tipo is not None:
            evento: Dict[str, Any] = {"tipo": tipo}
            texto = data.get("texto")
            if texto is not None:
                evento["texto"] = texto
            self._eventos.append(evento)

    def fechar(self, quando: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Escreve o turno e devolve o registro (None se não havia turno aberto)."""
        if self._pergunta is None:
            return None
        registro = montar_registro(
            self._pergunta, self._conversa_id, self._eventos,
            quando or datetime.now().isoformat(timespec="seconds"),
        )
        self._pergunta = None
        self._eventos = []
        with contextlib.suppress(OSError, ValueError, TypeError):
            self._caminho.parent.mkdir(parents=True, exist_ok=True)
            with self._caminho.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
        return registro
