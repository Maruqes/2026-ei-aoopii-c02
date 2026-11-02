# F07 — Spontaneous reactions

The bot can comment on or joke about the conversation through text, voice and GIF, or react with a GIF alone when the image is enough. Voice waits for a pause and yields to the assistant, music and participants; new speech cancels the voice intervention. GIF-only reactions never use TTS and respect the same cooldown.

**Configuration:** `GROUP_MEMORY_REACTIONS_ENABLED`, `GROUP_MEMORY_REACTION_COOLDOWN_MINUTES` (10 by default) and `GIPHY_API_KEY`. `/assistant disable` silences the server's reactions.

**Where it lives:**

- [discord_bot/group_memory.go](../../discord_bot/group_memory.go): collection, delivery, GIF selection and voice coordination.
- [app/group_memory.py](../../src/transcription-api/app/group_memory.py): creation, claiming and state of reactions.
- [llm.py](../../src/transcription-api/app/llm.py): conversation evaluation and reaction proposal.

**Context:** depends on [group memory](group-memory.md). Without a GIPHY key it continues with text/voice; without full Realtime coverage it posts in the chat, without voice. It does not necessarily generate a joke per bulk.

**GIFs:** searches with short English keywords (up to 50 characters), `pg-13` rating, and chooses among the five best new results. It uses a compact animated version, or the original when necessary, and avoids repeating the last 20 IDs per server, even when the URL changes. If the first page is exhausted, it tries a second; every search has a 5-second limit. The bot needs **Embed Links** in the channel for Discord to show the animation.

**Production diagnostics:** look for `group reaction GIF` in the bot logs. The events indicate whether the model did not propose a search, whether configuration/permission is missing, whether GIPHY failed (including HTTP 403/429), whether there are no new results or whether a GIF was selected. They do not include the key or the conversation. A GIPHY failure preserves the text; without text or an available GIF, the reaction ends as `failed` and does not post an empty message. The claim still prevents duplicates.

The search limit follows the [GIPHY Search contract](https://developers.giphy.com/docs/api/endpoint/search/); animated versions come from `images`, according to the [GIF schema](https://developers.giphy.com/docs/api/schema/).
