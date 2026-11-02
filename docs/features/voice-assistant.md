# F04 — Voice assistant

"Olá macaco" opens a conversation with the bot. After activation, the same person can continue without repeating the phrase. The bot waits for silence, replies in PT-PT in the chat and can read the answer into the call. It uses the last five minutes of the call and the recent dialogue; "Adeus macaco" or "Cancela" ends the conversation.

**Usage:** `/assistant status`, `enable`, `disable`, `phrase` and `channel`. Configuration is per server; changes require **Manage Server**.

**Where it lives:**

- [assistant.go](../../discord_bot/assistant.go): activation, question capture, silence, interruptions and delivery.
- [assistant_routes.py](../../src/transcription-api/app/assistant_routes.py): settings and `POST /v1/assistant/question`.
- [llm.py](../../src/transcription-api/app/llm.py): `answer_question` and AI context building.
- [voice-conversation.md](../../plan/voice-conversation.md): decisions and pending trials.

**Context:** requires [Realtime](realtime-transcription.md) for the speaker; there is one active conversation at a time. New speech can interrupt generation or the voice. Timings are configured through `ASSISTANT_*`.
