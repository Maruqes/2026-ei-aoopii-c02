-- Metadata makes accepted recordings recoverable after API restarts.
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS metadata JSONB;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS provider_job_id TEXT;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS provider_key_name TEXT;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE PRECISION;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS response_language TEXT NOT NULL DEFAULT 'pt';
CREATE INDEX IF NOT EXISTS idx_recordings_work ON voice_recordings (status, id);

-- Older jobs have no metadata from which to safely reconstruct their timestamps.
UPDATE voice_recordings SET status = 'failed', error = 'Legacy recording has no recoverable metadata'
WHERE status IN ('pending', 'transcribing') AND metadata IS NULL;

CREATE TABLE IF NOT EXISTS voice_profile_jobs (
    session_id BIGINT NOT NULL REFERENCES voice_sessions(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    PRIMARY KEY (session_id, user_id)
);

ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS provider_completed_at TIMESTAMPTZ;
ALTER TABLE voice_profile_jobs ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0;
