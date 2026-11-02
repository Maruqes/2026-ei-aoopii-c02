# F09 — Call summaries

When the session closes, the API uses the transcriptions to create a summary and update profiles. Long conversations are handled in ordered parts; summaries identify partial coverage when transcription is missing.

**Usage:** the summary is posted at the end of the call; `/recap` queries a session and allows viewing the summary or the transcription.

**Where it lives:**

- [audio.go](../../discord_bot/audio.go): `finishSessionAndPostSummary`.
- [main.go](../../discord_bot/main.go): `recapHook`.
- [agent.py](../../src/transcription-api/app/agent.py): session processing and participant updates.
- [llm.py](../../src/transcription-api/app/llm.py): `summarize_session` and summary prompts.
- [main.py](../../src/transcription-api/app/main.py): session closing, lookup and `build_session_recap`.

**Context:** depends on [capture](call-capture.md) and [transcription](recording-transcription.md); failed sessions can be recovered with `/retry`.
