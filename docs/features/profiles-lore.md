# F10 — Profiles and lore

Builds living member profiles from text and voice observations, with traits and memorable events. It also keeps local Markdown documents with the lore.

**Usage:** `/profile` shows the profile; `/prompt` asks a question about the person; `/sync` forces text observation synchronization.

**Where it lives:**

- [profile_updater.py](../../src/transcription-api/app/profile_updater.py): profile updates from observations.
- [agent.py](../../src/transcription-api/app/agent.py): updates from voice sessions.
- [docs_client.py](../../src/transcription-api/app/docs_client.py): reading/writing lore documents.
- [llm.py](../../src/transcription-api/app/llm.py): profile generation and answers about the person.
- [main.go](../../discord_bot/main.go): `profileHook`, `promptHook` and `syncHook`.

**Context:** receives data from chat, calls, the assistant and group memory. `LOCAL_PROFILE_DIR` defines the documents folder; PostgreSQL stores profiles and references.
