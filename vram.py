"""Governador de VRAM (#28) + orçamento de tokens de fundo (#29).

POR QUE existe: o alvo tem 10 GB e tudo coabita (pesos + KV-cache + embeddings +
eventualmente Whisper). Dois riscos:
 - #28 VAZAMENTO: se o uso de VRAM só cresce ao longo do tempo (um cache que não
   libera, um tensor retido), o processo caminha para o OOM. Um monitor amostra o
   uso e AVISA quando detecta crescimento sustentado — só observa, não age.
 - #29 ORÇAMENTO DINÂMICO: quando sobra pouca VRAM, o trabalho de FUNDO (ETL,
   contradição, watcher) deve gerar MENOS tokens para não competir pela memória
   com a inferência interativa. O orçamento é calibrado pela fração livre.

A leitura da VRAM (torch.cuda) é o único ponto de IO; o resto — detector de
vazamento e cálculo do orçamento — é puro/testável, alimentado por amostras.
"""
from __future__ import annotations

import collections
from typing import Deque, Optional


def ler_uso() -> Optional[dict]:
    """(free, total, usado, livre_frac) da GPU 0, ou None sem CUDA. torch já é dep
    (sentence-transformers). Import lazy: `import vram` funciona sem torch/GPU."""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        livre, total = torch.cuda.mem_get_info()  # bytes
    except Exception:
        return None
    if not total:
        return None
    return {
        "livre": int(livre),
        "total": int(total),
        "usado": int(total - livre),
        "livre_frac": livre / total,
    }


class MonitorVram:
    """Detector de VAZAMENTO (#28): alimentado com o uso (bytes) a cada amostra.
    Sinaliza quando a janela recente está CHEIA, cresceu além de `slack_bytes` e
    ainda está no pico (não foi um pico-e-volta). Puro."""

    def __init__(self, amostras: int, slack_bytes: int) -> None:
        self._n = max(3, amostras)
        self._slack = slack_bytes
        self._janela: Deque[int] = collections.deque(maxlen=self._n)

    def registrar(self, usado_bytes: int) -> bool:
        """True quando um vazamento é suspeito. Ao disparar, LIMPA a janela para não
        avisar a cada amostra (precisa de outra janela cheia para re-disparar)."""
        self._janela.append(int(usado_bytes))
        if len(self._janela) < self._n:
            return False
        amostras = list(self._janela)
        cresceu = amostras[-1] - amostras[0] > self._slack
        no_pico = amostras[-1] == max(amostras)   # não recuperou memória no fim
        if cresceu and no_pico:
            self._janela.clear()
            return True
        return False

    def reset(self) -> None:
        """Zera a janela quando o PATAMAR muda por causa CONHECIDA (unload/religar do
        LLM no ciclo de idle). Sem isto a janela compara o vale pós-unload com o pico
        pós-reload e acusa vazamento falso (medido 2026-07-21: aviso aos 7,33 GB logo
        após religar o modelo). Quem detecta o flip é o scheduler."""
        self._janela.clear()


def orcamento_tokens(
    livre_frac: Optional[float],
    base: int,
    minimo: int,
    frac_min: float,
    frac_ok: float,
) -> int:
    """Orçamento de tokens de FUNDO calibrado pela fração de VRAM livre (#29). Puro.

    livre_frac >= frac_ok  -> `base` (folga total).
    livre_frac <= frac_min -> `minimo` (aperto).
    entre os dois          -> interpolação linear.
    livre_frac None (sem CUDA/leitura) -> `base` (não penaliza quem não mede)."""
    if livre_frac is None or frac_ok <= frac_min:
        return base
    if livre_frac >= frac_ok:
        return base
    if livre_frac <= frac_min:
        return minimo
    t = (livre_frac - frac_min) / (frac_ok - frac_min)
    return int(round(minimo + (base - minimo) * t))
