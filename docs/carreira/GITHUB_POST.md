# GitHub — deixar o repositório profissional e divulgar

> "Fazer o post do projeto no GitHub" = duas coisas: (1) deixar a **página do repo**
> apresentável para quem cai nela (recrutador/dev), e (2) ter um **texto de anúncio**
> pronto para divulgar. Tudo abaixo é copiar-colar.

---

## 1) Campo "About" do repositório (a descrição no topo direito)

Vá em **repo → engrenagem "About"** e cole:

**Description (uma linha, ~120 caracteres):**
```
Assistente de IA por voz, 100% local — pipeline de dados/RAG de ponta a ponta em Python (ETL, embeddings, vetorial), com 624 testes em CI.
```

**Website:** deixe em branco ou aponte para seu LinkedIn.

**Topics (tags) — clique em "Add topics" e cole estas:**
```
python · data-engineering · etl · rag · llm · vector-database · chromadb ·
fastapi · data-pipeline · dataops · embeddings · local-first · voice-assistant ·
whisper · sqlite · machine-learning
```
> **Por quê:** os topics indexam o repo na busca do GitHub e sinalizam suas
> competências. `data-engineering`, `etl`, `data-pipeline`, `dataops` são os que
> importam para a vaga — não deixe de fora.

---

## 2) Social preview (a imagem que aparece quando o link é compartilhado)

Em **Settings → General → Social preview**, suba uma imagem 1280×640. Sugestão: um
card simples (Canva) com:
- Título grande: **Mente Digital**
- Subtítulo: *Pipeline de dados para IA, 100% local · Python · ETL · RAG*
- Rodapé: github.com/danielpvp22/mente_digital

> Sem isso, o link compartilhado no LinkedIn/WhatsApp fica sem imagem — com, parece
> muito mais profissional.

---

## 3) Fixar o repositório no seu perfil do GitHub

No seu perfil (github.com/danielpvp22) → **Customize your pins** → marque
`mente_digital`. Assim ele aparece em destaque para qualquer recrutador que abrir seu
perfil.

---

## 4) Criar um Release (dá um ar de "produto", não de "rascunho")

Em **Releases → Draft a new release**, tag `v2.0.0`, título e corpo:

**Título:** `v2.0.0 — Mente Digital (pacote modularizado)`

**Corpo (cole):**
```
Primeira release pública do Mente Digital: assistente de IA por voz 100% local,
com um pipeline de dados/RAG de ponta a ponta.

Destaques desta versão
- Pipeline de ETL incremental: ingestão → transformação → carga (relacional + vetorial)
- Recuperação semântica com embeddings e ranqueamento validado por A/B
- Agentes proativos (scheduler persistente) e comandos de voz determinísticos
- DataOps: 624 testes sem GPU/rede rodando em CI, migrações idempotentes
- Otimização orientada por métrica (percentis de latência p50/p95)

Como rodar e a arquitetura completa estão no README.
```

---

## 5) Texto de anúncio para divulgar (Reddit / comunidades / Twitter-X)

**Versão curta (X/Twitter, Telegram, WhatsApp):**
```
Abri o código do Mente Digital: um assistente de IA por voz que roda 100% local —
sem nuvem, sem API paga. Por baixo é um pipeline de dados/RAG completo em Python,
com 624 testes em CI. github.com/danielpvp22/mente_digital
```

**Versão para comunidades de dev/dados (Reddit r/dataengineering, Discords):**
```
Título: Construí um pipeline de dados para IA (RAG) 100% local — código aberto

Compartilhando um projeto autoral em Python. A parte que mais me deu trabalho (e
mais aprendi) não foi a IA, foi a engenharia de dados: ingestão incremental de
múltiplas fontes, transformação/dedup/proveniência, carga em base relacional e
vetorial, e uma cultura de DataOps de verdade — 624 testes rodando em CI sem
depender de GPU nem rede, migrações idempotentes, e cada decisão de arquitetura
validada por A/B (o ranqueamento de recuperação dobrou; a taxa de erro do modelo caiu
de 33% para 8%).

Feedback sobre a arquitetura é muito bem-vindo. Repo: github.com/danielpvp22/mente_digital
```

---

## 6) (Opcional) GitHub Discussions
Ative **Settings → Features → Discussions** e crie um post de "Show and tell" com o
texto acima. Sinaliza um projeto vivo e receptivo a colaboração.

---

## Checklist GitHub
- [ ] Preencher **About** + **Topics** (2 min, alto impacto)
- [ ] Subir **Social preview**
- [ ] **Fixar** o repo no perfil
- [ ] Publicar **Release v2.0.0**
- [ ] Divulgar com um dos textos de anúncio
