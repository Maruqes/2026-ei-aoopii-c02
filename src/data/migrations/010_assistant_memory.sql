ALTER TABLE messages ADD COLUMN IF NOT EXISTS assistant_answer TEXT;

CREATE INDEX IF NOT EXISTS idx_messages_assistant_profile_pending
ON messages (user_id, tstamp)
WHERE source_type = 'assistant';
