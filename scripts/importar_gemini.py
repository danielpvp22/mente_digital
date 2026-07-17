"""
Importa o histórico do Gemini (JSONs) como átomos Zettelkasten de verdade.

POR QUE EXISTE
--------------
O vault tem 7.268 notas no padrão `_Pt<N>_`, produto de um import mecânico que cortou
as conversas em fragmentos e derivou títulos sem contexto. O resultado:

    # Economia necessária
    **Origem:** [Import_Gemini_Otimizando-Munição-no-Tarkov-A.md]
    Precisamos economizar pelo menos **166,2**.      <- 166,2 de quê?

    # Gemini                                          <- título literalmente errado
    ...preços da AWS, instância g5.xlarge, Llama 3 (8B)...

Isso não é cosmético. `split_markdown` indexa o título junto com o corpo
(strip_headers=False), então o título ENTRA no vetor: medido, a nota do Tarkov acima
foi recuperada para uma pergunta sobre "a economia da máquina de lavar louça", junto
com uma sobre aluguel de trator — três domínios casados pela palavra "economia".

Não dá para consertar o título de um fragmento que perdeu o antecedente: a informação
não está lá. Mas a FONTE existe (64 JSONs, 9,1M de chars). Então regeneramos da fonte
em vez de remendar o derivado.

COMO EVITA REPETIR O ERRO
-------------------------
1. TEMA INJETADO: o nome do arquivo é o assunto da conversa e vai no prompt, com a
   regra de auto-contenção (prompts.prompt_sintese_import).
2. JANELA POR TURNO: corta em fronteira de turno, nunca no meio de uma frase.
3. FORMATO IMPOSTO: `agent.normalizar_atomo` garante '## ' + tags no Python — o A/B
   provou que nenhum modelo entrega o formato de forma confiável ("para que ele não
   alucine", nas palavras do dono).
4. Tags `#zettelkasten_atomico #memoria_legada` — é o passado do usuário, não
   curiosidade auto-colhida: não entra no ciclo de promoção do #conhecimento_novo.

Escreve numa pasta NOVA e não toca em nada existente. Retomável: mata e reinicia à
vontade que ele pula o que já fez.

    python scripts/importar_gemini.py --listar
    python scripts/importar_gemini.py --so "Calculando-Velocidade"   # prova de fogo
    python scripts/importar_gemini.py                                # tudo (~2,5h)
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time
from datetime import datetime
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import prompts  # noqa: E402
import textutils  # noqa: E402
from agent import _slug_titulo, dividir_atomos, normalizar_atomo  # noqa: E402
from config import settings  # noqa: E402
from llm import LlamaManager  # noqa: E402
from rag import EmbeddingProvider, strip_frontmatter  # noqa: E402
from telemetry import telemetry  # noqa: E402

DIR_JSON = os.path.join(settings.caminho_obsidian, "gemini")
DIR_SAIDA = os.path.join(settings.caminho_obsidian, "Importado_Gemini")
ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "importar_gemini.estado.json")

# Janela de conversa por passada. Cabe MUITO mais no n_ctx=8192, e a primeira versão
# usava 9000 — mas janela grande pede 10+ átomos numa passada só, e o modelo larga a
# regra de auto-contenção por volta do quinto (medido). Janela menor = menos átomos
# por passada = menos deriva. Custa mais passadas; o assunto (estágio 1) também fica
# mais preciso, porque um trecho curto não muda de tema no meio.
JANELA_CHARS = 4000
TAGS_IMPORT: Tuple[str, ...] = (prompts.TAG_ATOMO, "#memoria_legada")
# Similaridade de cosseno acima da qual dois átomos são "praticamente clones".
# Calibrado em 122k pares reais: mediana 0.350, p99 0.783 — os clones vivem em
# >=0.95. Conservador de propósito: funde cópia, não ideia parecida.
LIMIAR_CLONE = 0.95


def tema_do_arquivo(caminho: str) -> str:
    """'Otimizando-Munição-no-Tarkov-A.json' -> 'Otimizando Munição no Tarkov A'.

    O nome do arquivo é a única fonte confiável do ASSUNTO da conversa — e é
    justamente o que o import antigo jogou fora.
    """
    nome = os.path.splitext(os.path.basename(caminho))[0]
    return nome.replace("-", " ").replace("_", " ").strip()


def ler_turnos(caminho: str) -> List[Tuple[str, str]]:
    """Devolve [(papel, texto)] só do que é conhecimento.

    Descarta `thinking` (o rascunho interno do Gemini, 0,7M de chars: não é o que ele
    respondeu, e atomizar raciocínio abandonado geraria contradição na base) e os
    blocos não-textuais (imagem, widget, card de compra).
    """
    with open(caminho, encoding="utf-8") as f:
        dados = json.load(f)
    turnos: List[Tuple[str, str]] = []
    for msg in dados:
        papel = "Usuário" if msg.get("role") == "user" else "Assistente"
        partes = [
            str(c.get("content", "")).strip()
            for c in msg.get("contents", [])
            if c.get("type") == "text" and str(c.get("content", "")).strip()
        ]
        if partes:
            turnos.append((papel, "\n".join(partes)))
    return turnos


def janelar(turnos: List[Tuple[str, str]], limite: int = JANELA_CHARS) -> List[str]:
    """Agrupa turnos em janelas <= `limite`, cortando SÓ em fronteira de turno.

    Puro/testável. Um turno maior que o limite vira uma janela sozinho (é truncado
    pelo n_ctx lá na frente, mas nunca perde a fronteira pergunta/resposta — que é o
    que dá sentido ao trecho).
    """
    janelas: List[str] = []
    atual: List[str] = []
    tam = 0
    for papel, texto in turnos:
        bloco = f"**{papel}:** {texto}"
        if atual and tam + len(bloco) > limite:
            janelas.append("\n\n".join(atual))
            atual, tam = [], 0
        atual.append(bloco)
        tam += len(bloco)
    if atual:
        janelas.append("\n\n".join(atual))
    return janelas


class Deduplicador:
    """Funde átomos praticamente idênticos — uma ideia, uma nota.

    POR QUE É INDISPENSÁVEL AQUI: uma conversa longa discute a mesma ideia em vários
    trechos, e cada janela a re-atomiza. Medido numa parcial de 495 átomos: **37**
    tinham o título "Race Condition de Threads: Arquitetura de Interface", e 15
    arquivos eram byte-a-byte idênticos. Sem isto, o import entrega uma base redundante
    — e redundância no RAG é pior que inútil: o `rag_top_k=40` gasta o orçamento de
    contexto repetindo o mesmo fato e empurra para fora os átomos que faltavam.

    LIMIAR: medido sobre 122k pares reais, a mediana de similaridade é 0.350 e o p99
    é 0.783 — clones vivem em ≥0.95, bem separados. O default é conservador de
    propósito: funde só o que é quase cópia, e nunca duas ideias distintas que por
    acaso falam do mesmo assunto.

    Compara o texto INDEXADO (sem frontmatter): é exatamente o que vai virar vetor no
    Chroma, então dois átomos indistinguíveis aqui são indistinguíveis lá.
    """

    def __init__(self, emb, limiar: float) -> None:
        self._emb = emb
        self._limiar = limiar
        self._vecs = None          # np.ndarray (N, dim), já normalizado
        self.fundidos = 0

    def _add(self, vecs) -> None:
        import numpy as np
        self._vecs = vecs if self._vecs is None else np.vstack([self._vecs, vecs])

    def semear(self, textos: List[str]) -> None:
        """Carrega os átomos já em disco. Faz a dedup funcionar através do resume:
        sem isto, matar e reiniciar o lote reintroduziria as duplicatas."""
        if not textos:
            return
        import numpy as np
        vs = np.array(self._emb.embed_documents(textos), dtype="float32")
        vs /= np.linalg.norm(vs, axis=1, keepdims=True).clip(min=1e-9)
        self._add(vs)

    def filtrar(self, textos: List[str]) -> List[bool]:
        """Devolve, para cada texto, se ele deve ser MANTIDO. Compara contra tudo que
        já foi mantido — inclusive os anteriores desta mesma chamada (uma janela pode
        repetir a ideia dentro dela mesma)."""
        import numpy as np
        if not textos:
            return []
        vs = np.array(self._emb.embed_documents(textos), dtype="float32")
        vs /= np.linalg.norm(vs, axis=1, keepdims=True).clip(min=1e-9)
        manter: List[bool] = []
        for v in vs:
            clone = self._vecs is not None and len(self._vecs) and float((self._vecs @ v).max()) >= self._limiar
            manter.append(not clone)
            if clone:
                self.fundidos += 1
            else:
                self._add(v.reshape(1, -1))
        return manter


def garantir_assunto(bloco: str, assunto: str) -> str:
    """Se o título do átomo não menciona o assunto, PREFIXA o assunto. Puro/testável.

    A trava determinística, na mesma filosofia do `normalizar_atomo`: o LLM entrega a
    ideia, o Python garante a propriedade. Medido, o modelo obedece a regra de
    auto-contenção nos primeiros átomos e a larga por volta do quinto — então
    "## Pressão de Oferta" vira "## Economia do jogo NFT Bomb Crypto: Pressão de
    Oferta". Feio? Um pouco. Mas o título é INDEXADO junto com o corpo
    (split_markdown, strip_headers=False), então isto é literalmente o que faz o átomo
    ser recuperável pelo assunto certo — e o que impede que ele seja recuperado pelo
    assunto ERRADO, que é o bug que originou tudo isto (a nota do Tarkov casando com
    "economia da máquina de lavar louça" pela palavra 'economia').

    Não mexe quando o modelo já fez o trabalho: se houver qualquer keyword em comum
    entre título e assunto, o título fica intacto.
    """
    chaves_assunto = textutils.palavras_chave(assunto)
    if not chaves_assunto:
        return bloco
    linhas = bloco.strip().splitlines()
    for i, ln in enumerate(linhas):
        if not ln.lstrip().startswith("## "):
            continue
        titulo = ln.lstrip("#").strip()
        if textutils.tem_sobreposicao(chaves_assunto, textutils.palavras_chave(titulo)):
            return bloco                      # o modelo já nomeou o assunto
        linhas[i] = f"## {assunto}: {titulo}"
        return "\n".join(linhas)
    return bloco


def carregar_estado() -> dict:
    if os.path.exists(ESTADO):
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    return {"feitos": [], "atomos": 0}


def salvar_estado(estado: dict) -> None:
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


async def descobrir_assunto(llama, trecho: str, fallback: str) -> str:
    """ESTÁGIO 1: o assunto sai do TRECHO, não do nome do arquivo (a conversa muda de
    tema no meio). Cai no nome do arquivo se o modelo devolver lixo."""
    try:
        bruto = await llama.collect(
            prompts.prompt_assunto(trecho), max_tokens=24,
            system_prompt=prompts.SYS_ASSUNTO, temperature=0.0,
        )
    except Exception as exc:
        telemetry.warn("IMPORT", f"Falha ao detectar assunto: {exc}")
        return fallback
    assunto = " ".join(bruto.strip().strip('"\'.*').split())
    # Guarda contra o modelo que responde uma frase inteira em vez do assunto.
    if not assunto or len(assunto) > 70 or len(assunto.split()) > 10:
        return fallback
    return assunto


async def processar_janela(llama, dedup, tema: str, trecho: str, origem: str, seq: int) -> int:
    """Uma janela -> N arquivos de átomo. Devolve quantos salvou."""
    assunto = await descobrir_assunto(llama, trecho, tema)
    bruto = await llama.collect(
        prompts.prompt_sintese_import(assunto, trecho),
        max_tokens=settings.max_tokens_resumo,
        system_prompt=prompts.SYS_SINTESE_IMPORT,
    )
    bruto = bruto.strip()
    if not bruto:
        telemetry.warn("IMPORT", f"Síntese vazia (falha do LLM?) em '{assunto}' #{seq}.")
        return 0
    if bruto.upper().strip(".!\n ") == "NADA":
        return 0

    blocos = dividir_atomos(bruto)
    if not blocos:
        # Sem '##' NEM assinatura de átomo: o modelo devolveu prosa. Descartar é o
        # certo aqui (diferente do ETL ao vivo, onde o dump seria perdido): a fonte
        # continua no JSON e a janela pode ser reprocessada quando quisermos.
        telemetry.warn("IMPORT", f"Formato irrecuperável em '{assunto}' #{seq} — janela pulada.")
        return 0

    agora = datetime.now()
    # ESTÁGIO 3 (Python): a garantia. O estágio 2 acerta a maioria; este fecha o resto.
    atomos = [
        normalizar_atomo(garantir_assunto(b, assunto), origem, agora, tags=TAGS_IMPORT)
        for b in blocos
    ]
    atomos = [a for a in atomos if a.strip()]

    # ESTÁGIO 4 (Python): uma ideia, uma nota. Dedup sobre o texto INDEXADO — é o que
    # vira vetor no Chroma, então é ali que a redundância machuca.
    manter = dedup.filtrar([strip_frontmatter(a).strip() for a in atomos]) if dedup else [True] * len(atomos)

    salvos = 0
    for i, (atomo, ok) in enumerate(zip(atomos, manter)):
        if not ok:
            continue
        nome = f"Gemini_{_slug_titulo(atomo)}_{seq}_{i}.md"
        caminho = os.path.join(DIR_SAIDA, nome)
        try:
            with open(caminho, "w", encoding="utf-8") as f:
                f.write(atomo)
            salvos += 1
        except OSError as exc:
            telemetry.error("IMPORT", f"Falha ao salvar {nome}", exc)
    return salvos


async def importar(filtro: str | None, limite_janelas: int | None) -> None:
    arquivos = sorted(f for f in os.listdir(DIR_JSON) if f.endswith(".json"))
    if filtro:
        arquivos = [f for f in arquivos if filtro.lower() in f.lower()]
    if not arquivos:
        print(f"Nenhum JSON casa com '{filtro}' em {DIR_JSON}")
        return

    os.makedirs(DIR_SAIDA, exist_ok=True)
    estado = carregar_estado()
    feitos = set(estado["feitos"])

    llama = LlamaManager()
    await llama.load()
    if not llama.ready:
        print("ERRO: modelo não carregou (VRAM? o servidor está rodando?).")
        return

    # Dedup com o MESMO embedding do VectorStore: se dois átomos são clones aqui,
    # são indistinguíveis no Chroma também.
    provider = EmbeddingProvider()
    await asyncio.to_thread(provider.load)
    dedup = Deduplicador(provider.instance, LIMIAR_CLONE) if provider.instance else None
    if dedup is None:
        print("AVISO: sem embeddings — import seguirá SEM dedup.")
    else:
        # Semeia com o que já está em disco: sem isto, retomar o lote reintroduziria
        # duplicatas contra tudo que já foi importado.
        ja = [strip_frontmatter(open(p, encoding="utf-8").read()).strip()
              for p in sorted(glob.glob(os.path.join(DIR_SAIDA, "*.md")))]
        if ja:
            print(f"semeando dedup com {len(ja)} átomos já em disco...")
            await asyncio.to_thread(dedup.semear, ja)

    t0 = time.time()
    for arq in arquivos:
        caminho = os.path.join(DIR_JSON, arq)
        tema = tema_do_arquivo(caminho)
        turnos = ler_turnos(caminho)
        if not turnos:
            continue
        janelas = janelar(turnos)
        if limite_janelas:
            janelas = janelas[:limite_janelas]

        for seq, trecho in enumerate(janelas):
            chave = f"{arq}#{seq}"
            if chave in feitos:
                continue
            n = await processar_janela(llama, dedup, tema, trecho, arq, seq)
            estado["atomos"] += n
            feitos.add(chave)
            estado["feitos"] = sorted(feitos)
            salvar_estado(estado)          # a cada janela: matar o processo não perde nada
            print(f"  [{arq[:38]:38}] janela {seq+1}/{len(janelas)} -> {n} átomos "
                  f"(total {estado['atomos']}, {dedup.fundidos if dedup else 0} clones fundidos, "
                  f"{(time.time()-t0)/60:.0f} min)")

    print(f"\n{estado['atomos']} átomos em {DIR_SAIDA}")
    print("Eles NÃO estão no índice ainda: rode o servidor (o sync() indexa) quando quiser.")
    llama.shutdown()


def listar() -> None:
    arquivos = sorted(f for f in os.listdir(DIR_JSON) if f.endswith(".json"))
    total_j = 0
    for arq in arquivos:
        turnos = ler_turnos(os.path.join(DIR_JSON, arq))
        j = len(janelar(turnos))
        total_j += j
        print(f"  {j:4d} janelas  {len(turnos):4d} turnos  {arq}")
    print(f"\n{len(arquivos)} conversas, {total_j} janelas (~{total_j*9/60:.0f} min de GPU)")


def main() -> None:
    p = argparse.ArgumentParser(description="Importa o histórico do Gemini como átomos")
    p.add_argument("--listar", action="store_true", help="mostra o plano sem rodar nada")
    p.add_argument("--so", default=None, help="só as conversas cujo nome contém isto")
    p.add_argument("--max-janelas", type=int, default=None, help="teto de janelas por conversa (prova)")
    a = p.parse_args()
    if a.listar:
        listar()
    else:
        asyncio.run(importar(a.so, a.max_janelas))


if __name__ == "__main__":
    main()
