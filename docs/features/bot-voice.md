# F05 — Bot voice

Synthesizes replies locally with Piper and the Portuguese Tugão voice. It delivers audio in segments and adjusts expression from emojis. Music yields the output while speaking and resumes afterwards.

**Configuration:** `ASSISTANT_VOICE_ENABLED` turns voice on/off; `ASSISTANT_VOICE_MODEL` points to the model. If synthesis fails, the reply stays in the chat.

**Where it lives:**

- [speech.go](../../discord_bot/speech.go): preparation, playback and cancelling speech.
- [speech_synthesis.py](../../discord_bot/speech_synthesis.py): synthesis, segmentation and expression.
- [music.go](../../discord_bot/music.go): coordinating output with music.
- [Dockerfile](../../discord_bot/Dockerfile): Piper and voice model installation.

**Connects to:** [voice assistant](voice-assistant.md) and [spontaneous reactions](spontaneous-reactions.md).
