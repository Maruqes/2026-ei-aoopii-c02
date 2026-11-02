# F02 — Recording transcription

Converts WAV into text using local Whisper, Speechmatics or Deepgram. Requests are processed in the background and results are linked to the session and the author in PostgreSQL.

**Configuration:** `TRANSCRIPTION_PROVIDER` chooses the base provider. `TRANSCRIPTION_PROVIDER_ORDER` and `/stt order` define the preference and fallback between Deepgram and Speechmatics; `/stt status` shows the state.

**Where it lives:**

- [transcriber.py](../../src/transcription-api/app/transcriber.py): Whisper and Speechmatics.
- [deepgram.py](../../src/transcription-api/app/deepgram.py): Deepgram integration.
- [transcription_router.py](../../src/transcription-api/app/transcription_router.py) and [providers.py](../../src/transcription-api/app/providers.py): selection, limits and fallback.
- [main.py](../../src/transcription-api/app/main.py): `POST /v1/transcriptions` and `process_recording_file`.
- [transcription_commands.go](../../discord_bot/transcription_commands.go): `/stt` commands.

**Context:** the WAV also allows recovering from [Realtime](realtime-transcription.md) failures. Finished audio is cleaned up after the text is persisted.
