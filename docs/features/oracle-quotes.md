# F11 — Oracle and quote game

The oracle answers questions using the server's history. The quote game picks a stored sentence to guess who said it.

**Usage:** `/oracle` receives the question; `/guess` starts the quote game.

**Where it lives:**

- [main.go](../../discord_bot/main.go): `oracleHook` and `guessHook`.
- [main.py](../../src/transcription-api/app/main.py): `/v1/guilds/{guild_id}/oracle` and `/guess`.
- [repository.py](../../src/data/repository.py): server context and sentence retrieval; `get_guild_oracle_context`.
- [llm.py](../../src/transcription-api/app/llm.py): `answer_guild_question` and evidence-based answer rules.

**Context:** uses data already stored from messages, calls, profiles and memory. Context is queried for the server where the command runs.
