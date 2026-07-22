# Roteiro de voz — validar consertos #32/#33/#34/#37 + composto (PR #41)

> Siga na ordem. Em cada passo: **fale** a frase, veja a **resposta**, e confira o **terminal**.
> Entre testes diferentes, comece uma **conversa nova** (botão no painel) pra não misturar contexto.

## Antes de começar
- [ ] `.env` com `MENTE_RAG_DEBUG=true` e `MENTE_WHISPER_DESCARTAR_INCERTO=true` (já aplicados).
- [ ] Servidor rodando: `C:\ProgramData\miniconda3\envs\llama-omni\python.exe main.py`
- [ ] Terminal visível ao lado pra ler os logs.

---

## 1. #33 — declarativa NÃO vira agendamento
🎤 **"Vou viajar pra Salvador na sexta."**
- ✅ Responde como quem ANOTOU (memória), NÃO tenta agendar/pedir horário.
- 👀 Terminal: **NÃO** deve aparecer `rota=tool:criar_lembrete`. Segue o pipeline normal.

🎤 (em seguida, mesma conversa) **"Pra onde eu vou viajar?"**
- ✅ Responde **Salvador** (veio da memória da sessão).
- ❌ Se falar de música/turismo aleatório → anote e me conte.

---

## 2. #34 — modo confidencial não vaza (PRIVACIDADE)
🎤 **"mestre, modo confidencial"** → deve confirmar ("fica só nesta sessão").
🎤 Fale 2–3 coisas quaisquer (ex.: *"me explica o que é uma rede neural"*).
🎤 **Encerre a sessão** (botão encerrar/fechar a aba, ou espere o idle).
- 👀 Terminal: **`[SERVER] Sessão confidencial — idle de conhecimento pulado (nada atomizado).`**
- ✅ Confirmação final: **nenhum** arquivo novo em `Cerebro_Digital\Conhecimento_Novo\` depois disso.

---

## 3. Composto — lista + lembrete numa frase só (falhava 62×)
🎤 **"mestre, adiciona pão na lista e me lembra de comprar leite amanhã às 9h"**
- ✅ Faz **as duas**: "pão" entra na lista **e** cria o lembrete.
- ⚠️ Tem que ter **hora** ("amanhã às 9h", "daqui a 30 minutos"). "amanhã" sozinho é ambíguo e não agenda — isso é esperado.
- 👀 Terminal: `adicionar_item` **e** `criar_lembrete` (mensagem ~"comprar leite").

---

## 4. #32 — pergunta geral não puxa nota-piada
🎤 **"explica RAG"**
- ✅ Dá uma **explicação de verdade** de RAG (da web OU dos seus átomos legítimos de RAG).
- ❌ NÃO pode devolver piada/tangente pessoal (o antigo "RAG = base do Tarkov").
- 👀 Terminal: `pergunta_definicional` reconhecida; se o vault for fraco no tema, escala pra web.

> Nota: como você TEM átomos legítimos de RAG, responder do vault é OK — o que importa é ser uma resposta real, não a nota-piada.

---

## 5. #37 parte 1 — gate de confiança do STT (calibração)
Isto roda o tempo todo em segundo plano. Para provocar de propósito:
🎤 Fale **baixo/abafado uma palavra curta** (um resmungo, "ãhn", algo ruidoso).
- 👀 Terminal: **`[WHISPER] Descartada transcrição incerta: '...' (confiança=-1.4x).`**

**Calibração** (o objetivo deste passo):
- Se **fala SUA real e curta** ("sim", "não", "oi") for descartada por engano → **baixe** o limiar: `MENTE_WHISPER_CONFIANCA_MIN_LOGPROB=-1.5` no `.env` e reinicie.
- Se **lixo** ("oração", "atenção") ainda **passar** e for respondido → **suba**: `-0.7`.
- Anote o valor que ficou bom.

---

## 6. Coletar dados pros follow-ups (issue #37 p2 e #42)
Enquanto testa, se acontecer:
- **Alguma resposta puxar um átomo do assunto errado** → copie do terminal a linha `[LOCAL_DBG]  dist=... [arquivo]` desse átomo. (é o número que calibra o teto do #42)
- **Um follow-up quebrar** (ex.: "E em que dia?" responder a data de hoje) → anote a frase exata e o que ele fez (issue #37 parte 2).

---

## Resumo do que me trazer depois
1. ✅/❌ de cada um dos 5 testes.
2. O **valor de `CONFIANCA_MIN_LOGPROB`** que ficou bom.
3. Qualquer linha **`[LOCAL_DBG] dist=...`** de contaminação que aparecer.
4. Qualquer follow-up que quebrou (frase + comportamento).
