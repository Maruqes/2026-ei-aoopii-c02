# F01 — Call capture

The bot follows voice channel joins/leaves, joins automatically when applicable and records audio into small per-member WAV files. It identifies who spoke, opens a session and closes it to produce the summary.

**Usage:** `/start`, `/stop` and `/timeout` control operation and the leave timeout.

**Where it lives:**

- [audio.go](../../discord_bot/audio.go): joining the call, participants, recordings and closing the session.
- [audio_convert.go](../../discord_bot/audio_convert.go) and [audio_buffer.go](../../discord_bot/audio_buffer.go): conversion and organisation of received audio.
- [ssrc_user_map.go](../../discord_bot/ssrc_user_map.go): mapping between audio streams and users.
- [main.go](../../discord_bot/main.go): `startHook`, `stopHook`, `timeoutHook` and connection recovery on startup.

**Connects to:** [recording transcription](recording-transcription.md), [Realtime](realtime-transcription.md) and [call summaries](call-summaries.md).
