"""Detector de Contradição (#24) — parte pura/testável.

POR QUE existe: a base cresce sozinha (ETL + pesquisa proativa). Com o tempo, dois
átomos sobre o MESMO tema podem AFIRMAR COISAS OPOSTAS (um diz "X custa 500", outro
"X custa 800"; a base envelhece). Contradição vive entre átomos PRÓXIMOS mas não
idênticos — o vizinho semântico logo acima da banda de duplicata. No idle, o ETL
pergunta ao LLM se o par se contradiz; aqui ficam só as funções puras (prompt +
parse do veredito), para o teste não precisar de GPU.

O veredito do LLM é texto não-confiável: começa com SIM/NAO. Só "SIM" (com um
motivo curto) conta como contradição — qualquer outra coisa (NAO, prosa, vazio) é
tratada como "sem contradição", o lado seguro (não inventa conflito).
"""
from __future__ import annotations

from typing import Optional

from mente_digital import textutils


def parse_veredito(resposta: str) -> Optional[str]:
    """Extrai o motivo se o LLM disse SIM (contradição); None caso contrário. Puro.

    Aceita "SIM: motivo", "SIM - motivo", "SIM. motivo" e "SIM" solto. Um "NAO..."
    (ou "SIM" dentro de "assim"/"sim senhor" no meio) NÃO dispara — exige o veredito
    no INÍCIO da resposta normalizada."""
    if not resposta:
        return None
    bruto = resposta.strip()
    n = textutils.normaliza(bruto)
    if not n.startswith("sim"):
        return None
    # tira o "sim" inicial + separador (:/-/./espaço) e devolve o motivo original.
    resto = bruto[3:].lstrip(" :.-\t\n")
    return resto.strip() or "contradição detectada (sem detalhe)"
