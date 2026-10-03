-- Migrations are replayed on startup. The old column default marks this upgrade
-- so a later explicit /assistant phrase "Hey Bot" is preserved on subsequent runs.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attrdef d
        JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
        WHERE d.adrelid = 'guild_transcription_settings'::regclass
          AND a.attname = 'assistant_phrase'
          AND pg_get_expr(d.adbin, d.adrelid) = quote_literal('Hey Bot') || '::text'
    ) THEN
        UPDATE guild_transcription_settings
        SET assistant_phrase = 'Olá macaco', assistant_revision = assistant_revision + 1,
            updated_at = NOW()
        WHERE lower(btrim(assistant_phrase)) = 'hey bot';
        ALTER TABLE guild_transcription_settings
            ALTER COLUMN assistant_phrase SET DEFAULT 'Olá macaco';
    END IF;
END $$;
