# F17 — Bot language

Switches messages and command descriptions between Portuguese and English, also passing the language to the AI requests that support it.

**Usage:** `/language` changes the language; `BOT_LANGUAGE` sets the initial value. The voice assistant keeps replies in PT-PT.

**Where it lives:**

- [bot_language.go](../../discord_bot/bot_language.go): current language, translations and command building.
- [main.go](../../discord_bot/main.go): `languageHook` and Discord command updates.
- [llm.py](../../src/transcription-api/app/llm.py): language instructions for summaries and answers.

**Context:** language state is global to the bot process, not a per-server preference.
