-- Configurable farewell phrase that closes a voice-assistant conversation.
ALTER TABLE guild_transcription_settings ADD COLUMN IF NOT EXISTS assistant_goodbye_phrase TEXT NOT NULL DEFAULT 'adeus macaco';
