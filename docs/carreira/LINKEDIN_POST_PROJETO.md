# Post do LinkedIn — Mente Digital

> Três versões prontas. Escolha uma. Poste em **texto puro** (LinkedIn não formata
> markdown) — os emojis e quebras de linha abaixo já são o formato final para colar.
> Melhor horário: terça a quinta, 8h–10h ou 12h–13h. Responda todos os comentários na
> 1ª hora (o alcance depende disso).

---

## VERSÃO A — narrativa técnica (recomendada para posicionar como Eng. de Dados)

```
Passei os últimos meses construindo um projeto que, no fundo, é uma aula de
Engenharia de Dados disfarçada de assistente de voz. 🧠

Chama-se Mente Digital. Por fora, é um assistente que responde por voz a partir das
minhas próprias notas. Por dentro, é o problema que todo time de dados conhece:
como transformar dado bruto e disperso em um dataset confiável, pronto para uma
camada de IA consumir?

O que construí, em linguagem de engenharia de dados:

🔹 Pipeline de ETL incremental de ponta a ponta — ingestão de múltiplas fontes,
   transformação, deduplicação e carga, no modelo bruto → limpo → pronto
   (o mesmo racional de bronze/silver/gold).
🔹 Ingestão incremental por watermark (só reprocesso o que mudou) — o mesmo
   princípio de CDC/merge incremental.
🔹 Duas engines de armazenamento: relacional (SQLite) para fatos e vetorial
   (ChromaDB) para busca semântica.
🔹 DataOps levado a sério: 624 testes automatizados rodando em CI, migrações de
   schema idempotentes e configuração 100% versionada.
🔹 Decisões por dado, não por achismo: A/B próprios que dobraram o ranqueamento da
   recuperação (MRR@10 0,20 → 0,375) e derrubaram a taxa de erro do modelo de 33% p/ 8%.
🔹 Otimização sob restrição real (orçamento de 10 GB de memória): profiling por
   estágio com percentis p50/p95 para saber onde otimizar antes de otimizar.

A maior lição: "usar IA" é fácil; o difícil — e o valioso — é a engenharia de dados
que alimenta a IA. É onde o dado vira confiável.

Código aberto: github.com/danielpvp22/mente_digital

#EngenhariaDeDados #DataEngineering #Python #ETL #DataOps #RAG #DadosParaIA
```

---

## VERSÃO B — curta e direta (mais alcance, menos técnica)

```
Construí um assistente de IA que roda 100% na minha máquina — sem nuvem, sem API paga.

Mas o que mais aprendi não foi sobre IA. Foi sobre Engenharia de Dados. 👇

Porque, no fundo, o projeto é um pipeline: pegar dado bruto de várias fontes,
limpar, modelar em camadas, garantir qualidade e entregar um dataset confiável para
o modelo consumir. Bronze → Silver → Gold, na prática.

Levei a sério a parte de engenharia:
✅ 624 testes automatizados em CI
✅ ETL incremental (só processa o que mudou)
✅ Decisões validadas por A/B com métrica (ranqueamento 2x melhor)
✅ Otimização por profiling (p50/p95), não por chute

"Usar IA" é a parte fácil. A engenharia de dados que alimenta a IA é a parte que
importa — e é o que eu faço.

Repositório aberto no primeiro comentário. 👇

#EngenhariaDeDados #DataEngineering #Python #ETL #IA
```
> (Na Versão B, cole o link `github.com/danielpvp22/mente_digital` no **primeiro
> comentário** — links no corpo do post reduzem o alcance; no comentário, não.)

---

## VERSÃO C — storytelling pessoal (conexão + engajamento)

```
"Por que você construiria um assistente de IA do zero se já existe o ChatGPT?"

Me fizeram essa pergunta e a resposta é: eu não queria o assistente. Eu queria o
problema de engenharia de dados por trás dele.

Chamei de Mente Digital. E ele me obrigou a resolver, de verdade, tudo que um
Engenheiro de Dados enfrenta:

→ Ingerir dado de fontes bagunçadas e transformá-lo em algo confiável.
→ Modelar em camadas (bruto → limpo → pronto para consumo).
→ Garantir qualidade: deduplicação, proveniência, testes.
→ Otimizar custo e performance com recurso limitado.
→ E provar que cada decisão foi a certa — com métrica, não com opinião.

O resultado tem 624 testes automatizados, roda em CI, e cada escolha de arquitetura
foi validada por um experimento A/B versionado.

No fim, aprendi que a IA é só o último passo. O trabalho de verdade — o que separa um
protótipo de um sistema — é a engenharia de dados.

Deixei tudo aberto: github.com/danielpvp22/mente_digital

Curioso para saber: na sua experiência, qual a parte mais subestimada de um pipeline
de dados? 👇

#EngenhariaDeDados #DataEngineering #Python #DataOps #Carreira
```

---

## Dicas de publicação
- **Não** cole o link do GitHub no meio do texto na Versão B/C — ponha no 1º comentário.
- Marque 2–3 pessoas relevantes (ex-colegas, mentores) só se fizer sentido — não force.
- Reaproveite o texto: uma semana depois, poste **um print de um trecho do README** (o
  diagrama Mermaid da arquitetura fica ótimo) como carrossel/imagem — conteúdo visual
  rende mais.
- Fixe o post em **Em destaque** no perfil depois.
