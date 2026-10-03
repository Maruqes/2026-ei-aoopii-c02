-- A WAV is the durable audio unit in either mode. Each Realtime epoch owns one WAV.
CREATE TABLE IF NOT EXISTS guild_transcription_settings (
    guild_id TEXT PRIMARY KEY,
    streaming_enabled BOOLEAN NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS realtime_seconds DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS realtime_key_name TEXT;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS stream_token TEXT;
ALTER TABLE voice_recordings ADD COLUMN IF NOT EXISTS cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS recording_id BIGINT REFERENCES voice_recordings(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_messages_recording ON messages(recording_id);
CREATE TABLE IF NOT EXISTS realtime_finals (
    recording_id BIGINT NOT NULL REFERENCES voice_recordings(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    identity TEXT NOT NULL,
    PRIMARY KEY (recording_id, generation, identity)
);
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS credits_exhausted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS credits_notice_sent BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS credits_episode INTEGER NOT NULL DEFAULT 0;
