"""O `.env.example` promete botões — este teste cobra a promessa.

⚠ POR QUE ELE EXISTE. O pydantic está com `extra="ignore"` (config.py), então uma chave
`MENTE_*` que não corresponde a campo nenhum é engolida CALADA: quem a copia para o `.env`
acredita ter ligado a função e não ligou nada. Não é hipótese — foi medido em 2026-08-02
com `MENTE_PESQUISA_AGENDADA_INTERVALO_SEGUNDOS`, que viveu meses no `.env` do dono lendo
`0` (desligado) enquanto o campo real terminava em `_SECONDS`. O aviso escrito no próprio
arquivo não impede a próxima: o `.env.example` mistura campos em português
(`..._VALIDADE_MINUTOS`) e em inglês (`..._INTERVALO_SECONDS`), e adivinhar o idioma de
cada um é exatamente o erro que o silêncio do pydantic esconde.

Aqui a documentação passa a FALHAR quando mente, em vez de decepcionar em produção.
"""
from __future__ import annotations

import re
from pathlib import Path

from mente_digital.config import Settings

RAIZ = Path(__file__).resolve().parents[1]
EXEMPLO = RAIZ / ".env.example"

# Casa tanto a linha ativa quanto a comentada — o arquivo documenta a maioria dos botões
# COMENTADOS (é um exemplo, não uma configuração), e um nome errado atrás do `#` engana
# igual: quem copia a linha tira o `#` e leva o erro junto.
_CHAVE = re.compile(r"^\s*#?\s*(MENTE_[A-Z0-9_]+)\s*=", re.MULTILINE)


def _chaves_documentadas() -> list[str]:
    return _CHAVE.findall(EXEMPLO.read_text(encoding="utf-8"))


def test_exemplo_tem_chaves():
    """Guarda contra o teste passar à toa: regex quebrada acharia zero chaves e todos os
    asserts abaixo ficariam verdes sem conferir nada."""
    assert len(_chaves_documentadas()) >= 15


def test_toda_chave_do_exemplo_existe_em_settings():
    """O `.env.example` só pode citar botão que o pydantic de fato lê."""
    campos = set(Settings.model_fields)
    orfas = [k for k in _chaves_documentadas()
             if k.removeprefix("MENTE_").lower() not in campos]
    assert not orfas, (
        f"Chaves no .env.example sem campo correspondente em Settings: {orfas}. "
        "Com extra='ignore' o pydantic as ignora em SILÊNCIO — quem copiar a linha vai "
        "acreditar que ligou a função. Confira a grafia (PT vs EN) em config.py."
    )


def test_validade_do_codigo_de_pareamento_esta_documentada():
    """O botão que se ajusta ao convidar alguém tem de estar no arquivo que o dono abre.

    Estava só em `config.py`, entre ~1.500 linhas de campos, e a pergunta que o originou
    ("o código não pode expirar por 2 h, tem como?") não se responde lendo aquilo.
    """
    texto = EXEMPLO.read_text(encoding="utf-8")
    assert "MENTE_APARELHOS_CODIGO_VALIDADE_MINUTOS" in texto
    # As duas pegadinhas que fazem o ajuste falhar em silêncio precisam estar ditas: sem
    # restart o valor novo não vale, e retroativo significa que há o que desfazer depois.
    assert "REINICIAR" in texto
    assert "RETROATIVO" in texto
