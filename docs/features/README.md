# Feature index

Reference names for talking about the project. Each MD explains the behaviour, how to use it and where it is implemented. [General context](../CONTEXT.md).

| ID | Name and document | Main code entry point |
| --- | --- | --- |
| F01 | [Call capture](call-capture.md) | `discord_bot/audio.go` |
| F02 | [Recording transcription](recording-transcription.md) | `src/transcription-api/app/transcriber.py` |
| F03 | [Real-time transcription](realtime-transcription.md) | `discord_bot/streaming.go` |
| F04 | [Voice assistant](voice-assistant.md) | `discord_bot/assistant.go` |
| F05 | [Bot voice](bot-voice.md) | `discord_bot/speech.go` |
| F06 | [Group memory](group-memory.md) | `src/transcription-api/app/group_memory.py` |
| F07 | [Spontaneous reactions](spontaneous-reactions.md) | `discord_bot/group_memory.go` |
| F08 | [Chat history and summaries](chat-history.md) | `discord_bot/text_messages.go` |
| F09 | [Call summaries](call-summaries.md) | `src/transcription-api/app/agent.py` |
| F10 | [Profiles and lore](profiles-lore.md) | `src/transcription-api/app/profile_updater.py` |
| F11 | [Oracle and quote game](oracle-quotes.md) | `discord_bot/main.go`: `oracleHook`, `guessHook` |
| F12 | [Music in the call](music.md) | `discord_bot/music.go` |
| F13 | [Word triggers](word-triggers.md) | `discord_bot/said_keywords.go` |
| F14 | [AI configuration](ai-configuration.md) | `src/transcription-api/app/llm.py` |
| F15 | [Status, usage and recovery](status-recovery.md) | `src/transcription-api/app/workers.py` |
| F16 | [Deleting personal data](data-deletion.md) | `discord_bot/main.go`: `forgetHook` |
| F17 | [Bot language](language.md) | `discord_bot/bot_language.go` |

The features exist in the code; some depend on keys, models or configuration. These names organise the documentation and do not change the existing commands.
