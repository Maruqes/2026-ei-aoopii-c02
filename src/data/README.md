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
