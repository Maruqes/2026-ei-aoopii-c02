ALTER TABLE messages ADD COLUMN IF NOT EXISTS guild_id TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS channel_id TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS discord_message_id TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ;

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_text_seen_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_discord_message_id
ON messages (discord_message_id)
WHERE discord_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_text_profile_pending
ON messages (user_id, tstamp)
WHERE source_type = 'text';

CREATE INDEX IF NOT EXISTS idx_messages_guild_channel_tstamp
ON messages (guild_id, channel_id, tstamp);
