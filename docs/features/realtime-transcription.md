# F03 — Real-time transcription

Sends audio during the call over WebSocket to Speechmatics or Deepgram. It manages per-participant reservations and a first-come-first-served queue; anyone without a slot continues with Batch recordings.

**Usage:** `/streaming mode:on`, `off` or `status`; changes require **Manage Server**. The preference is saved per server and starts disabled by default.

**Where it lives:**

- [streaming.go](../../discord_bot/streaming.go): participants, audio sending and recognised events.
- [streaming_routes.py](../../src/transcription-api/app/streaming_routes.py): preference, reservations and `/v1/streaming/audio`.
- [realtime.py](../../src/transcription-api/app/realtime.py): reservation pool and bridge to the providers.
- [providers.py](../../src/transcription-api/app/providers.py): provider capacity and health.

**Context:** failures can recover from the WAV without repeating text. The pool requires a single API process. It feeds the [voice assistant](voice-assistant.md).
