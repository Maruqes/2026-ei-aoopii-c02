# F11 — Oráculo e jogo de citações

O oráculo responde a perguntas usando o histórico do servidor. O jogo de citações escolhe uma frase guardada para adivinhar quem a disse.

**Uso:** `/oracle` recebe a pergunta; `/guess` inicia o jogo de citações.

**Onde está:**

- [main.go](../../discord_bot/main.go): `oracleHook` e `guessHook`.
- [main.py](../../src/transcription-api/app/main.py): `/v1/guilds/{guild_id}/oracle` e `/guess`.
- [repository.py](../../src/data/repository.py): recuperação de contexto e frases do servidor; `get_guild_oracle_context`.
- [llm.py](../../src/transcription-api/app/llm.py): `answer_guild_question` e regras de resposta com evidência.

**Contexto:** usa dados já guardados de mensagens, chamadas, perfis e memória. O contexto é consultado para o servidor onde o comando é executado.
