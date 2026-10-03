-- Assistant settings share the per-guild transcription preferences.
ALTER TABLE guild_transcription_settings ALTER COLUMN streaming_enabled DROP NOT NULL;
ALTER TABLE guild_transcription_settings ADD COLUMN IF NOT EXISTS assistant_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_transcription_settings ADD COLUMN IF NOT EXISTS assistant_phrase TEXT NOT NULL DEFAULT 'Hey Bot';
ALTER TABLE guild_transcription_settings ADD COLUMN IF NOT EXISTS assistant_channel_id TEXT;
ALTER TABLE guild_transcription_settings ADD COLUMN IF NOT EXISTS assistant_revision BIGINT NOT NULL DEFAULT 0;
