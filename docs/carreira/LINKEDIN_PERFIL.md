# LinkedIn — reformulação do perfil (o quê e onde)

> Guia seção por seção, na ordem em que aparecem no perfil. Cada bloco tem o texto
> **pronto para colar** e uma nota de *por quê*. Substitua os `«...»`.
> Objetivo: ranquear para recrutadores que buscam "Engenheiro de Dados" e contar uma
> história coerente com a vaga da XP (medallion, dados para IA, DataOps).

---

## 1) Foto e banner
- **Foto:** rosto claro, fundo neutro, expressão profissional. (Perfis com foto recebem muito mais visualizações.)
- **Banner (a faixa atrás):** use uma imagem simples com um mini-pitch. Sugestão de texto para colocar no banner (Canva tem modelos "LinkedIn banner data engineer"):
  > *"Engenharia de Dados · Pipelines confiáveis · Python · SQL · Cloud"*

## 2) Headline (o título abaixo do nome) — **o campo mais importante**

O LinkedIn indexa fortemente esse campo. Não deixe só "Engenheiro de Dados".
Escolha **uma** das opções (todas cabem no limite de 220 caracteres):

**Opção A — direta e com keywords (recomendada):**
```
Engenheiro de Dados Pleno | Python · SQL Avançado · Spark/Databricks · ETL & DataOps | Pipelines confiáveis para Analytics e IA
```

**Opção B — orientada a valor:**
```
Engenheiro de Dados | Construo pipelines e datasets confiáveis (Bronze/Silver/Gold) para times de Analytics e IA | Python · SQL · Databricks · Airflow
```

**Opção C — enxuta:**
```
Engenheiro de Dados Pleno · Python | SQL | Spark | Databricks | Airflow | ETL · DataOps · Qualidade de Dados
```

> **Por quê:** recrutador filtra por palavra-chave. Ter "Databricks", "Spark",
> "Airflow", "ETL", "DataOps" no headline faz você aparecer em buscas onde hoje
> provavelmente não aparece. Se ainda não domina Databricks/Spark, use a Opção A/C
> mas remova o que não sabe defender — ou mantenha e trate como "estudando" no Sobre.

## 3) Seção "Sobre" (About) — sua narrativa

Cole e ajuste. Estrutura: quem você é → o que faz de melhor → prova → o que busca.

```
Engenheiro de Dados focado em transformar dados brutos e dispersos em datasets
confiáveis, bem modelados e prontos para consumo por times de Analytics e de IA.

Trabalho o ciclo completo: ingestão de múltiplas fontes, modelagem em camadas
(bronze/silver/gold), transformação e controle de qualidade, e entrega de bases
consistentes — sempre com cultura de DataOps: versionamento, testes automatizados e
decisões baseadas em métrica, não em achismo.

O que trago:
• SQL avançado e modelagem de dados (relacional e não-relacional)
• Python para engenharia de dados e automação de pipelines
• ETL/ELT incremental, deduplicação, proveniência e otimização de performance/custo
• «Spark/Databricks/Delta, Airflow/ADF — cite o que já usou; o resto, "aprofundando"»

Recentemente construí o Mente Digital, um projeto autoral que faz a engenharia de
dados que alimenta um modelo de IA (pipeline RAG de ponta a ponta): ingestão →
limpeza → modelagem em camadas → indexação → entrega. Levei a sério a parte de
engenharia: 624 testes automatizados rodando em CI, migrações de schema idempotentes
e otimização orientada por percentis de latência. Código aberto:
github.com/danielpvp22/mente_digital

Aberto a oportunidades de Engenheiro de Dados (Pleno) — São Paulo / presencial
flexível ou remoto.
```

> **Por quê:** o "Sobre" é lido por humanos E indexado. As primeiras 3 linhas
> aparecem antes do "ver mais" — por isso o pitch mais forte vem no topo. Os bullets
> repetem as keywords da vaga (bom para busca) e o parágrafo do projeto dá prova
> concreta, que é o que diferencia você de um perfil genérico.

## 4) Experiência — Grupo SC

Reescreva a entrada do Grupo SC com **bullets de impacto** (não descrição de tarefas).
Use os mesmos bullets do CV (arquivo `CV_XP_Engenheiro_Dados_Pleno.md`, seção
Experiência) — eles já estão na linguagem da vaga. Regras:

- Comece cada linha com **verbo de ação**: Construí, Modelei, Otimizei, Implementei.
- Sempre que possível, **número**: volume de dados, % de redução de tempo/custo, nº de fontes.
- Inclua as palavras: **pipeline, ETL, modelagem, SQL, Python, data quality, performance**.
- No campo "Competências" da própria vaga (o LinkedIn deixa marcar skills por experiência), marque SQL, Python, ETL, etc. — isso reforça o ranking.

Modelo de descrição do cargo (cole e ajuste):
```
Responsável por pipelines de dados, modelagem e entrega de datasets confiáveis para
consumo analítico e de negócio.

• Construí e mantive pipelines de ETL/ELT em «ferramenta», processando «volume» de
  múltiplas fontes com entrega confiável para «times».
• Modelei camadas de dados «staging/DW ou bronze/silver/gold», padronizando fontes
  heterogêneas em datasets reutilizáveis.
• Otimizei «queries/jobs», reduzindo «tempo/custo» em «X%» via «técnica».
• Implementei controles de data quality e boas práticas de DataOps (Git, testes, CI).
• Dei suporte a «analistas/cientistas de dados» com dados consistentes e documentados.
```

## 5) Seção "Projetos" (adicione se ainda não tiver)

Adicione o Mente Digital como **Projeto** e como item **Em destaque (Featured)** no
topo do perfil:

- **Título:** Mente Digital — pipeline de dados para IA (RAG) 100% local
- **Descrição:**
```
Sistema autoral de engenharia de dados que prepara datasets para consumo por um
modelo de IA. Pipeline de ETL incremental (medallion: bruto → limpo → pronto),
deduplicação, proveniência/linhagem, carga em base relacional (SQLite) e vetorial
(ChromaDB). DataOps: 624 testes em CI, migrações idempotentes, decisões por A/B
(ranqueamento 2×, erro 33%→8%), profiling p50/p95. Python. Código aberto.
```
- **Link:** github.com/danielpvp22/mente_digital

> **Featured:** fixe o link do repositório e o post do projeto (ver
> `LINKEDIN_POST_PROJETO.md`) na seção "Em destaque". É a primeira coisa que o
> recrutador vê ao rolar.

## 6) Competências (Skills) — reordene as 3 do topo

O LinkedIn destaca 3 skills fixadas. Fixe as que a vaga pede: **SQL**, **Python**,
**Apache Spark** (ou **ETL**). Adicione a lista completa:
`SQL · Python · Apache Spark · Databricks · Delta Lake · Apache Airflow · Azure Data
Factory · ETL · Modelagem de Dados · Data Warehouse · PySpark · Data Quality ·
DataOps · Git · Pipelines de Dados`

> Só adicione as que sabe defender. Peça **endossos** a ex-colegas do Grupo SC nas 3–4
> principais — sobe o ranking de busca.

## 7) "Open to Work"
Ative **Open to work** (visível só para recrutadores, se preferir discrição) com
cargos: *Engenheiro de Dados, Data Engineer, Analytics Engineer*; localidade *São
Paulo e Remoto*. Isso te coloca no filtro que recrutadores da XP usam.

## 8) URL personalizada e idioma
- Sua URL já está boa (`daniel-f-mma-gmp`). Se quiser, personalize para algo como
  `daniel-fernandes-dados` (Configurações → editar URL pública) — mais legível num CV.
- Considere preencher também a versão em **inglês** do perfil (LinkedIn permite perfil
  multilíngue) com as mesmas keywords em inglês (Data Engineer, ETL, Data Pipelines) —
  amplia o alcance.

---

## Checklist rápido do que fazer hoje
- [ ] Trocar o **Headline** (Opção A) — maior impacto, 2 minutos
- [ ] Reescrever o **Sobre** com o texto acima
- [ ] Reescrever os bullets da **experiência Grupo SC** com números
- [ ] Adicionar **Mente Digital** em Projetos + Em destaque
- [ ] Fixar **SQL / Python / Spark** no topo das Skills e pedir endossos
- [ ] Ativar **Open to Work**
- [ ] Publicar o **post do projeto** (arquivo separado) e fixá-lo em Em destaque
