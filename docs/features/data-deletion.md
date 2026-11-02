# F16 — Deleting personal data

Deletes a member's stored data, including messages, profile, lore document and associated local audio. It coordinates capture and pending submissions to perform the removal.

**Usage:** `/forget` for your own data; deleting someone else's requires **Manage Server**.

**Where it lives:**

- [main.go](../../discord_bot/main.go): `forgetHook` and permission checks.
- [audio.go](../../discord_bot/audio.go): `forgetUserWithLocalAudio`, pausing/closing capture and removing local files.
- [main.py](../../src/transcription-api/app/main.py): `DELETE /v1/users/{discord_id}`.
- [repository.py](../../src/data/repository.py): removal of persisted data.
- [docs_client.py](../../src/transcription-api/app/docs_client.py) and [recording_cleanup.py](../../src/transcription-api/app/recording_cleanup.py): lore and audio/recovery file removal.

**Context:** it is a deletion operation, not a permanent preference to stop collecting future messages.
