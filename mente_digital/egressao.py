"""Guarda de Egressão Web (#6): mascara PII do usuário ANTES da query sair para a
rede (DuckDuckGo). 100% puro e testável — sem dependência, sem estado, sem IO.

POR QUE existe: a ÚNICA saída de rede do sistema é o WebSearcher. Um assistente
"100% local" que vaza o CPF/e-mail/cartão do dono no termo de busca quebra a
própria promessa — o DDG (e quem estiver no meio) veria o dado. O caminho quente
é `WebSearcher._ddg`, por onde TODA busca passa (search e prefetch); é lá que a
máscara é aplicada.

FILOSOFIA anti-falso-positivo: mascarar cegamente arruinaria buscas legítimas
("qual o DDD 41?"). Então cada detector é CONSERVADOR — prefere deixar passar um
caso duvidoso a estragar a busca — e o que casa vira um TOKEN semântico
(`[email]`, `[cpf]`, ...) em vez de sumir, para a query seguir fazendo sentido
("meu cpf [cpf] caiu no serasa?"). Toda máscara é LOGADA pelo chamador — é uma
defesa de privacidade observável, não silenciosa.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# --- E-mail: alta confiança, sempre mascara --------------------------------
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# --- CPF/CNPJ FORMATADOS: a pontuação é o sinal de confiança ---------------
# Só casamos as formas COM pontuação canônica (xxx.xxx.xxx-xx). Uma corrida de 11
# dígitos crus é ambígua demais (poderia ser telefone) — deixamos para o detector
# de telefone/cartão, que têm suas próprias travas.
_CPF_RE = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
_CNPJ_RE = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")

# --- Cartão de crédito: 13–19 dígitos (com separadores) que passem no Luhn --
# O Luhn é a trava anti-falso-positivo: um número de série qualquer quase nunca
# passa, um cartão sempre passa. Sem ele, "produto 4111 1111 1111 1111" e um SKU
# longo seriam indistinguíveis.
_CARTAO_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# --- Telefone BR: exige um MARCADOR explícito de telefone ------------------
# Um par de 8 dígitos ("1234 5678") é indistinguível de um telefone — mascarar
# isso arruinaria buscas legítimas (SKU, código, ano+valor). Então, fiéis à
# filosofia conservadora, SÓ mascaramos quando há sinal inequívoco de telefone:
# DDD entre parênteses "(41)" OU código de país "+55". Telefone escrito solto
# (sem parênteses nem +55) passa — é o preço aceito para não gerar falso-positivo.
_TELEFONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:\+55[\s-]?\(?\d{2}\)?|\(\d{2}\))"   # +55[ DD] OU (DD) — o marcador
    r"[\s-]?9?\d{4}[\s-]?\d{4}"              # 9 opcional + 4 + 4
    r"(?!\d)"
)


def _luhn_ok(digitos: str) -> bool:
    """Valida o dígito verificador de Luhn (base de cartões de crédito)."""
    if len(digitos) < 13:
        return False
    total = 0
    for i, ch in enumerate(reversed(digitos)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mascarar_cartao(texto: str, achados: set) -> str:
    def _sub(m: re.Match) -> str:
        so_digitos = re.sub(r"[ -]", "", m.group(0))
        if _luhn_ok(so_digitos):
            achados.add("cartao")
            return "[cartao]"
        return m.group(0)  # não passou no Luhn -> não é cartão, preserva

    return _CARTAO_RE.sub(_sub, texto)


def _mascarar(texto: str, regex: re.Pattern, token: str, tipo: str, achados: set) -> str:
    def _sub(_m: re.Match) -> str:
        achados.add(tipo)
        return token

    return regex.sub(_sub, texto)


def mascarar_pii(texto: str) -> Tuple[str, List[str]]:
    """Devolve (texto_mascarado, tipos_encontrados). Ordem IMPORTA: e-mail e os
    documentos formatados primeiro; cartão (Luhn) antes de telefone, senão um
    cartão viraria vários "telefones". `tipos_encontrados` é ordenado para o log/
    teste serem determinísticos."""
    if not texto:
        return texto, []
    achados: set = set()
    texto = _mascarar(texto, _EMAIL_RE, "[email]", "email", achados)
    texto = _mascarar(texto, _CNPJ_RE, "[cnpj]", "cnpj", achados)
    texto = _mascarar(texto, _CPF_RE, "[cpf]", "cpf", achados)
    texto = _mascarar_cartao(texto, achados)
    texto = _mascarar(texto, _TELEFONE_RE, "[telefone]", "telefone", achados)
    return texto, sorted(achados)
