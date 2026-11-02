# Data Layer

Postgres schema and database helpers for the real implementation live here.

Start the local database:

```powershell
docker compose up -d postgres
```

Or:

```powershell
make compose
```

Apply migrations:

```powershell
python src/data/apply_migrations.py
```

Or:

```powershell
make migrate
```

The default connection URL is:

```text
postgresql://discord:discord@localhost:5432/discord_anthropologist
```

The `text_chunks` table stores rebuilt 30-minute channel windows. A transcription insert rebuilds only the windows touched by the inserted voice segments.

Migration `006_streaming.sql` adds per-guild Realtime preferences and reuses
`voice_recordings` as the durable WAV unit: participation token, generation, state,
Realtime key and separate usage. Messages point to the unit; `realtime_finals` guarantees
idempotency. Fallback replaces the unit's text atomically. Credit discard
and privacy invalidation are terminal and are not reopened by retry. Episode/warning
fields and pending cleanup allow recovering side effects after restart.
Operational rollback: `/streaming mode:off`; keep the additive tables/columns and the
pending WAVs until recovery completes. Do not remove the migration with pending work.
