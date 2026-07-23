# Currículo — Engenheiro de Dados Pleno (XP Inc.)

> **Como usar:** este é o conteúdo pronto para copiar. Substitua tudo entre `«...»`
> pelas suas informações reais. Mantenha 1–2 páginas. Recomendo montar a versão final
> no **Google Docs** ou **Canva** (modelo limpo, uma coluna, sem foto, fonte legível)
> e exportar em **PDF nomeado `Daniel_Fernandes_Engenheiro_de_Dados.pdf`**.
> Dica ATS: o sistema da XP (Greenhouse) lê texto — nada de tabelas/ícones que
> quebrem a leitura. Uma coluna, títulos simples, palavras-chave da vaga no texto.

---

## DANIEL FERNANDES
### Engenheiro de Dados Pleno

📍 «Cidade / SP» · Disponível para modelo presencial flexível (São Paulo)
📧 «seu-email» · 📱 «seu-telefone»
🔗 linkedin.com/in/daniel-f-mma-gmp · 💻 github.com/danielpvp22

---

### RESUMO PROFISSIONAL

Engenheiro de Dados com «X» anos de experiência em construção de pipelines de
dados, modelagem e preparação de datasets confiáveis para consumo analítico e por
modelos de IA. Sólido em **SQL avançado**, **Python** e **arquitetura de dados
(relacional e não-relacional)**, com prática em **ETL incremental, controle de
qualidade e otimização de performance/custo**. Cultura forte de **DataOps**:
versionamento, testes automatizados e decisões orientadas por métrica. Buscando
atuar na construção de camadas analíticas (medallion) e no fornecimento de dados
consistentes para times de dados e IA.

> Ajuste os anos e, se você já usou Spark/Databricks/Airflow no Grupo SC, troque a
> frase para citar isso explicitamente — é o que mais pontua nesta vaga.

---

### COMPETÊNCIAS TÉCNICAS

- **Linguagens:** Python (engenharia de dados), SQL avançado, «Scala/Java se aplicável»
- **Big Data / Processamento:** «Spark / PySpark», «Databricks / Delta Lake», processamento em lote e streaming
- **Orquestração:** «Airflow», «Azure Data Factory (ADF)», agendamento e pipelines resilientes
- **Bancos de dados:** «PostgreSQL / SQL Server / Oracle», modelagem dimensional e relacional; NoSQL/vetorial (ChromaDB)
- **Cloud:** «Azure / AWS / GCP — cite as que usou»
- **DataOps & Qualidade:** Git, CI, testes automatizados, migrações de schema idempotentes, data quality, observabilidade/telemetria
- **Modelagem:** arquitetura medallion (Bronze/Silver/Gold), ingestão incremental (CDC/watermark), deduplicação, proveniência/linhagem
- **Ferramentas de dados para IA:** embeddings, pipelines de RAG, indexação vetorial

> **Regra de ouro:** só liste o que você sabe defender em entrevista. Onde estiver
> `«...»`, coloque a sua realidade. Se ainda não tem Spark/Databricks de produção,
> veja a seção "Formação Contínua" abaixo — declare como "em progresso", não invente.

---

### EXPERIÊNCIA PROFISSIONAL

**Grupo SC — Engenheiro de Dados Pleno**
«mês/ano de início» – «mês/ano de fim ou Atual» · «Cidade/UF ou Remoto»

> Escreva 4–6 bullets no formato **verbo de ação + o que fez + resultado/impacto
> mensurável**. Abaixo, bullets-modelo já na linguagem da vaga XP — **substitua os
> `«...»` pelos seus números e ferramentas reais**:

- Construí e mantive pipelines de ETL/ELT em «ferramenta» processando «volume, ex.: X GB/milhões de registros por dia» de múltiplas fontes, entregando dados confiáveis para «times de analytics/BI/ciência de dados».
- Modelei camadas analíticas «Bronze/Silver/Gold ou staging/DW» em «SQL/Spark», padronizando «X» fontes heterogêneas em datasets conformados e reutilizáveis.
- Otimizei performance e custo de «queries/jobs», reduzindo «tempo de execução/custo» em «X%» através de «particionamento / reescrita de SQL / cache / ajuste de cluster».
- Implementei controles de **data quality** e testes automatizados que reduziram «incidentes/retrabalho» em «X%».
- Apliquei boas práticas de **DataOps**: versionamento em Git, «CI/CD», documentação e migrações de schema versionadas.
- Dei suporte a «cientistas de dados / analistas» disponibilizando datasets consistentes e documentados para «modelos/relatórios».

**«Empresa anterior, se houver» — «Cargo»**
«período»
- «bullet»
- «bullet»

---

### PROJETO EM DESTAQUE

**Mente Digital — Pipeline de dados para IA (RAG) 100% local**
Projeto autoral · Python · [github.com/danielpvp22/mente_digital](https://github.com/danielpvp22/mente_digital)

Sistema que **transforma dados brutos em datasets prontos para consumo por um modelo
de IA** — a mesma disciplina de engenharia de dados que sustenta um time de IA:

- **Pipeline de ETL incremental** de ponta a ponta: ingestão (bruto → limpo →
  pronto, no modelo **medallion**), transformação com deduplicação e proveniência, e
  carga em duas engines — **relacional (SQLite)** e **vetorial (ChromaDB)**.
- **Ingestão incremental por watermark** (`mtime` como change-feed), o mesmo racional
  de CDC/`MERGE` incremental em Delta Lake.
- **Decisões orientadas por dados:** harnesses de A/B versionados elevaram o
  ranqueamento de recuperação em **~2× (MRR@10 0,20 → 0,375)** e reduziram a taxa de
  erro do modelo de **33% → 8%**.
- **DataOps de verdade:** **624 testes automatizados sem dependência de GPU/rede**
  rodando em **CI** a cada mudança; migrações de schema idempotentes; configuração
  100% versionada por variável de ambiente (128 parâmetros).
- **Otimização de custo/performance sob restrição real** (orçamento de 10 GB): profiling
  por estágio com **percentis p50/p95** para decidir onde otimizar antes de otimizar.

> Detalhamento técnico completo no README do repositório.

---

### FORMAÇÃO

**«Curso — ex.: Bacharelado em Ciência da Computação / Análise e Desenvolvimento de Sistemas»**
«Instituição» · «ano de conclusão ou previsão»

---

### FORMAÇÃO CONTÍNUA / CERTIFICAÇÕES

> Esta seção é onde você fecha o gap com a stack da XP de forma honesta e proativa.
> Sugestões de alto valor para esta vaga (cite as que estiver fazendo/tiver):

- «Databricks — Data Engineer Associate (em progresso)»
- «Formação PySpark / Delta Lake — em progresso»
- «Azure Data Fundamentals (DP-900) / Azure Data Engineer (DP-203)»
- «Apache Airflow — curso/prática»

---

### IDIOMAS

- Português — nativo
- Inglês — «nível: básico / intermediário / avançado» (leitura técnica «nível»)
