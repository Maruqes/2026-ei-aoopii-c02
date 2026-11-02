# F05 — Bot voice

Synthesizes replies with a natural European Portuguese neural voice. It uses Microsoft
Edge TTS (`pt-PT-RaquelNeural` by default, `pt-PT-DuarteNeural` as an alternative),
streams audio in segments and shapes expression from emojis with native rate, pitch
and volume. If Edge is unreachable, the same reply falls back to the local Piper
Tugão voice at a natural pace. Music yields the output while speaking and resumes
afterwards.

**Configuration:** `ASSISTANT_VOICE_ENABLED` turns voice on/off;
`ASSISTANT_TTS_PROVIDER` selects `edge` (default) or `piper`;
`ASSISTANT_VOICE` chooses the Edge voice; `ASSISTANT_VOICE_MODEL` points to the
Piper fallback model. If synthesis fails, the reply stays in the chat.

**Where it lives:**

- [speech.go](../../discord_bot/speech.go): preparation, playback and cancelling speech.
- [speech_synthesis.py](../../discord_bot/speech_synthesis.py): providers, synthesis,
  segmentation and expression.
- [music.go](../../discord_bot/music.go): coordinating output with music.
- [Dockerfile](../../discord_bot/Dockerfile): Edge TTS, Piper and voice model installation.

**Connects to:** [voice assistant](voice-assistant.md) and [spontaneous reactions](spontaneous-reactions.md).
