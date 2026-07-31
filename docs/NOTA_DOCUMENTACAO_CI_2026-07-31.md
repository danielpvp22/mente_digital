# Nota para quem está mexendo na documentação (2026-07-31)

Escrita por outra sessão, que estava consertando o ETL idle e tropeçou nisto. Não
toquei em `README.md` nem em `CLAUDE.md` de propósito — só reporto o que medi.

## O problema: a documentação afirma que não há linter, e há

`CLAUDE.md`, na seção **Comandos**, diz:

> Não há linter ou build configurados.

Isso está **errado hoje**, e a frase é ativamente enganosa: o CI roda quatro portões
de qualidade, todos bloqueantes.

### O que o CI realmente executa

`.github/workflows/tests.yml`, job **`pytest`**:

```yaml
- run: pip install -r requirements-ci.txt
- run: ruff check .
- run: pytest -q --cov=mente_digital --cov-fail-under=77
```

Job **`security`**:

```yaml
- run: bandit -c pyproject.toml -r mente_digital main.py -q --severity-level medium
- run: pip-audit            # audita as deps instaladas do job
```

Portanto os portões são:

| portão | ferramenta | onde reprova |
|---|---|---|
| lint | `ruff check .` | job `pytest`, **antes** da suíte |
| cobertura | `--cov-fail-under=77` | job `pytest`, depois da suíte |
| segurança estática | `bandit` (MEDIUM+) | job `security` |
| deps vulneráveis | `pip-audit` | job `security` |

## Por que isso custou tempo de verdade

O job chama-se **`pytest`**, mas roda `ruff` como primeiro passo. Quando ele
reprovou, a mensagem que chegou foi *"falhou no pytest"* — e eu passei três rodadas
da suíte local procurando falha de teste (cheguei a investigar plugin de ordenação
aleatória) antes de olhar o CI. A suíte estava verde nas duas pontas o tempo todo; o
que reprovava era um `import re` sem uso.

A frase do `CLAUDE.md` foi o que me fez **não** cogitar lint. Ela não é só desatualizada;
ela desvia o diagnóstico.

## O que sugiro escrever no lugar

Trocar a frase por algo como:

> Há uma suíte `pytest` (pasta `tests/`) e **quatro portões de qualidade no CI**, todos
> bloqueantes: `ruff check .` (lint, roda ANTES da suíte no job chamado "pytest"),
> `--cov-fail-under=77` (piso de cobertura), `bandit` (MEDIUM+) e `pip-audit`. Não há
> build. Antes de empurrar, rode o mesmo que o CI roda:
>
> ```bash
> ruff check . && pytest -q --cov=mente_digital --cov-fail-under=77
> ```

E vale um aviso explícito em algum lugar visível: **o job do CI se chama `pytest` mas
o primeiro passo dele é o `ruff`** — uma reprovação "no pytest" pode não ter nada a ver
com testes.

## Um segundo item: a contagem de testes do README está defasada em ~62%

O `README.md` afirma **885 testes** em cinco lugares — e um deles é um badge:

| linha | contexto |
|---|---|
| `README.md:12` | badge (`testes-885`) |
| `README.md:31` | texto |
| `README.md:102` | texto |
| `README.md:178` | texto |
| `README.md:210` | texto |

A suíte está hoje em **1430** (medido: `pytest -q` em 2026-07-31, após os commits desta
leva). O número 885 é de meados de julho.

Sugestão: se for atualizar, considere **parar de citar o número** no corpo do texto e
deixá-lo só no badge — cinco cópias de um número que muda a cada PR garantem que ele
volte a ficar errado. Um badge gerado pelo CI não desatualiza.

(A frase sobre o linter, essa sim, está no `CLAUDE.md:23` — não no README.)

## Terceiro item: os dois arquivos de requirements

`requirements.txt` e `requirements-ci.txt` divergem **de propósito** (o do CI é mínimo
para manter o job em ~1 minuto: sem `llama-cpp-python`, `torch`, `chromadb`,
`faster-whisper`). Isso já causou duas reprovações do tipo "passa aqui, falha lá" —
a de hoje foi `Pillow`, que existe no `requirements.txt` e faltava no do CI.

Se houver um lugar natural na documentação para isso, vale registrar a regra:

> Ao adicionar uma dependência usada por **testes**, ela precisa entrar também no
> `requirements-ci.txt` — senão a suíte passa na máquina de desenvolvimento (env conda
> `llama-omni`, que tem tudo) e reprova no CI.

---

Nada aqui exige mudança de código — é tudo documentação. Os quatro portões estão
corretos e funcionando; o problema é só a documentação afirmar o contrário.
