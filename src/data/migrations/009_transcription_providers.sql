ALTER TABLE guild_transcription_settings ADD COLUMN IF NOT EXISTS provider_order TEXT[];
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS result_provider TEXT;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS result_model TEXT;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS provider_group TEXT;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS recovery_reason TEXT;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS voice_transcription_attempts (
    id BIGSERIAL PRIMARY KEY,
    recording_id BIGINT NOT NULL REFERENCES voice_recordings(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    product TEXT NOT NULL CHECK (product IN ('streaming', 'batch')),
    provider TEXT NOT NULL,
    key_name TEXT NOT NULL,
    quota_group TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    sent_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    remote_id TEXT,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_transcription_attempt_unit ON voice_transcription_attempts(recording_id);
-- Only records with concrete Realtime provenance are attributed historically.
UPDATE voice_recordings SET result_provider = 'speechmatics',
    result_model = metadata->>'speechmatics_realtime_model'
WHERE realtime_key_name IS NOT NULL AND result_provider IS NULL;
