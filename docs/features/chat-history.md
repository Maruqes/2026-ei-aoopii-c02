# F08 — Chat history and summaries

Stores members' text messages for context, profiles and lookup. It ignores private messages, bots, webhooks and messages without text. It can summarize decisions, highlights and lore from a channel.

**Usage:** `/digest hours:24` summarizes the current channel; it accepts 1–168 hours and considers up to 2000 recent messages.

**Where it lives:**

- [text_messages.go](../../discord_bot/text_messages.go): reception, filters and author/channel identification.
- [transcription_client.go](../../discord_bot/transcription_client.go): sending messages to the API.
- [main.go](../../discord_bot/main.go): `digestHook`.
- [main.py](../../src/transcription-api/app/main.py): `POST /v1/messages` and `/v1/guilds/{guild_id}/digest`.
- [repository.py](../../src/data/repository.py): history persistence and lookup.

**Connects to:** [profiles and lore](profiles-lore.md) and [oracle](oracle-quotes.md).
