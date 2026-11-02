# F15 — Status, usage and recovery

Shows service health, capacity and provider usage. It keeps persistent queues in the API and pending submission files in the bot to resume work after failures/restarts; it removes files from finished recordings.

**Usage:** `/ping`, `/health`, `/keys` and `/retry session:...`.

**Where it lives:**

- [main.go](../../discord_bot/main.go): diagnostic and recovery commands.
- [transcription_client.go](../../discord_bot/transcription_client.go): submission, retries and local outbox.
- [workers.py](../../src/transcription-api/app/workers.py): processing of persistent jobs.
- [recording_cleanup.py](../../src/transcription-api/app/recording_cleanup.py): WAV cleanup and recovery.
- [provider_routes.py](../../src/transcription-api/app/provider_routes.py), [speechmatics_usage.py](../../src/transcription-api/app/speechmatics_usage.py) and [speechmatics_usage_alerts.go](../../discord_bot/speechmatics_usage_alerts.go): metrics and alerts.

**Context:** local usage, reported usage and balance are different measurements. Recoverable failures preserve audio; confirmed exhaustion of all allowed providers can suspend capture and discard the affected pending audio.
