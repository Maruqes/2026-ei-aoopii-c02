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

A migração `006_streaming.sql` acrescenta preferências Realtime por guild e reutiliza
`voice_recordings` como unidade durável WAV: token de participação, geração, estado,
key Realtime e consumo separado. Mensagens apontam à unidade; `realtime_finals` garante
idempotência. Fallback substitui o texto da unidade atomicamente. Descarte por créditos
e invalidação por privacidade são terminais e não são reabertos por retry. Campos de
episódio/aviso e limpeza pendente permitem recuperar side effects depois de restart.
Rollback operacional: `/streaming mode:off`; conservar tabelas/colunas aditivas e os
WAVs pendentes até concluir a recuperação. Não remover a migração com trabalho pendente.
