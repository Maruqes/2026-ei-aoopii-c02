# F13 — Word triggers

Runs a function when specific words appear in the transcription of a recording by the same author. It ignores case and punctuation, compares whole words and avoids repeating the same trigger in that recording.

**Usage in code:** register with `triggerSaidKeyworkd(callback, "palavra")`. The existing example detects "pijama" and writes `pijama` to an available text channel.

**Where it lives:**

- [said_keywords.go](../../discord_bot/said_keywords.go): registration, matching, lookup and the `sayPijama` example.
- [main.go](../../discord_bot/main.go): initial trigger registration.
- [said_keywords_test.go](../../discord_bot/said_keywords_test.go): examples and detection checks.

**Context:** works with confirmed Realtime text or Batch results. It does not use AI; registrations and deduplication are kept in memory.
