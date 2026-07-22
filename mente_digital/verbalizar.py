"""
Verbalização de números PT-BR para fala (pré-Piper).

PORQUÊ: o Piper foneiza via espeak-ng, que ADIVINHA a leitura de números — e em PT-BR
erra os casos mais comuns do assistente:
  - decimal: "3,5" → a vírgula vira PAUSA ("três… cinco") em vez de "três vírgula cinco";
  - hora com "h": "22h42" → o "h" fica literal ("vinte e dois agá quarenta e dois");
  - símbolos ($ % ° º): o filtro _ALLOWED do TtsService os DESCARTA depois, então
    "R$ 5,50" viraria "erre cinco…", "80°C" viraria "oitenta cê".

`verbalizar` soletra TUDO em palavras ANTES do Piper — tira a adivinhação do espeak.
Módulo PURO/testável (mesmo espírito de agenda.py): nenhuma dependência de GPU/rede.
Cardinais vêm da num2words (lang="pt"); a estrutura (ordem, plurais, "e", horas,
decimal dígito-a-dígito) é imposta aqui, como em atomos.py o Python impõe a forma.

Ordem das transformações importa: cada passo que soletra CONSOME seus dígitos (viram
palavras), então passos posteriores não os re-tocam. Passos que só trocam o SÍMBOLO
(%/°/traço) deixam o número para o passo de inteiro/decimal/milhar no fim.
"""
from __future__ import annotations

import re

try:
    from num2words import num2words

    _HAS_N2W = True
except Exception:  # pragma: no cover - degrada sem a lib (não quebra o TTS)
    _HAS_N2W = False


def _card(n: int) -> str:
    """Cardinal PT-BR ("catorze"). Sem num2words, degrada para o dígito cru."""
    if not _HAS_N2W:
        return str(n)
    return num2words(n, lang="pt")


def _ordinal(n: int, feminino: bool) -> str:
    """Ordinal PT-BR ("primeiro"/"primeira"). Feminino troca o "o" final de cada
    palavra por "a" ("vigésimo primeiro" → "vigésima primeira")."""
    if not _HAS_N2W:
        return str(n)
    palavra = num2words(n, lang="pt", to="ordinal")
    if feminino:
        palavra = " ".join(
            t[:-1] + "a" if t.endswith("o") else t for t in palavra.split()
        )
    return palavra


def _hora_word(h: int) -> str:
    """A HORA é feminina em PT-BR falado: "uma hora", "duas e trinta"."""
    return {1: "uma", 2: "duas"}.get(h, _card(h))


# ---------------------------------------------------------------------------
# Cada função abaixo é o `repl` de um re.sub. Recebem o Match e devolvem a fala.
# ---------------------------------------------------------------------------
def _moeda(m: "re.Match[str]") -> str:
    reais = int(m.group(1).replace(".", ""))
    cent = int(m.group(2)) if m.group(2) else 0
    partes = []
    if reais > 0:
        partes.append(f"{_card(reais)} {'real' if reais == 1 else 'reais'}")
    if cent > 0:
        partes.append(f"{_card(cent)} {'centavo' if cent == 1 else 'centavos'}")
    if not partes:
        return "zero reais"
    return " e ".join(partes)


def _hora_hm(m: "re.Match[str]") -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    if mi == 0:
        return f"{_hora_word(h)} {'hora' if h == 1 else 'horas'}"
    return f"{_hora_word(h)} e {_card(mi)}"


def _hora_h(m: "re.Match[str]") -> str:
    h = int(m.group(1))
    return f"{_hora_word(h)} {'hora' if h == 1 else 'horas'}"


def _decimal(m: "re.Match[str]") -> str:
    inteiro = int(m.group(1).replace(".", ""))
    # Parte decimal DÍGITO A DÍGITO (preserva zeros: "5,50" → "cinco vírgula cinco
    # zero"; e casa com a leitura da própria num2words: "3,14" → "um quatro").
    frac = " ".join(_card(int(d)) for d in m.group(2))
    return f"{_card(inteiro)} vírgula {frac}"


def _milhar(m: "re.Match[str]") -> str:
    return _card(int(m.group(0).replace(".", "")))


def _inteiro(m: "re.Match[str]") -> str:
    return _card(int(m.group(0)))


# Regexes compilados uma vez. `_HM` cobre "14:30" e "22h42"; `_H` só "8h".
_MOEDA = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{1,2}))?")
_HM = re.compile(r"\b(\d{1,2})[:hH](\d{2})\b")
_H = re.compile(r"\b(\d{1,2})[hH]\b")
_ORD_M = re.compile(r"\b(\d+)\s*º")
_ORD_F = re.compile(r"\b(\d+)\s*ª")
_PCT = re.compile(r"(\d)\s*%")
_GRAUS_C = re.compile(r"°\s*C\b")
_GRAUS_F = re.compile(r"°\s*F\b")
_GRAUS = re.compile(r"°")
_INTERVALO = re.compile(r"(\d)\s*[–—]\s*(\d)")
_DECIMAL = re.compile(r"(\d{1,3}(?:\.\d{3})*|\d+),(\d+)")
_MILHAR = re.compile(r"\b\d{1,3}(?:\.\d{3})+\b")
_INTEIRO = re.compile(r"\d+")


def verbalizar(texto: str) -> str:
    """Reescreve todos os números/símbolos de `texto` como PALAVRAS faláveis.

    Puro: sem estado, sem IO. Idempotência prática — rodar de novo sobre a saída (já
    sem dígitos) não muda nada."""
    if not texto:
        return texto

    # 1) Moeda primeiro: consome "R$ 1.234,56" inteiro (incl. milhar e centavos).
    texto = _MOEDA.sub(_moeda, texto)
    # 2) Horas: "HH:MM"/"HHhMM" antes do "Hh" solto (senão "22h42" viraria "22 horas").
    texto = _HM.sub(_hora_hm, texto)
    texto = _H.sub(_hora_h, texto)
    # 3) Ordinais (consomem o dígito com leitura própria, ≠ cardinal).
    texto = _ORD_M.sub(lambda m: _ordinal(int(m.group(1)), feminino=False), texto)
    texto = _ORD_F.sub(lambda m: _ordinal(int(m.group(1)), feminino=True), texto)
    # 4) Símbolos que só viram sufixo — deixam o NÚMERO para os passos 6-8.
    texto = _PCT.sub(r"\1 por cento", texto)
    texto = _GRAUS_C.sub(" graus célsius", texto)
    texto = _GRAUS_F.sub(" graus fahrenheit", texto)
    texto = _GRAUS.sub(" graus", texto)
    texto = _INTERVALO.sub(r"\1 a \2", texto)
    # 5) Decimal (tem vírgula) antes do milhar (só pontos) e do inteiro cru.
    texto = _DECIMAL.sub(_decimal, texto)
    texto = _MILHAR.sub(_milhar, texto)
    texto = _INTEIRO.sub(_inteiro, texto)

    return re.sub(r"\s{2,}", " ", texto).strip()
