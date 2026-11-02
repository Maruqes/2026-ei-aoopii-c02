# Project context

The **Discord Anthropologist** follows voice and text conversations on a Discord server. It transcribes calls, creates summaries, maintains member profiles and lore, answers questions and plays music.

## What is where

| Part | Location | Responsibility |
| --- | --- | --- |
| Discord bot (Go) | [discord_bot](../discord_bot/) | Commands, audio/text capture, playback and reply delivery. |
| API (Python/FastAPI) | [src/transcription-api/app](../src/transcription-api/app/) | Transcription, AI, memory and background processing. |
| Data (PostgreSQL) | [src/data](../src/data/) | History, sessions, profiles, preferences and persistent queues. |
| Startup | [docker-compose.yml](../docker-compose.yml), [Makefile](../Makefile) | API, bot and database; `make compose` starts the services. |
| Configuration | [.env.example](../.env.example) | Example environment variables. |
| Sample requests | [BrunoAPI](../BrunoAPI/) | Collection for trying the API. |
| Plans and decisions | [plan](../plan/) | Design history and checks; consult the code for current behaviour. |

## Main flow

1. The bot receives audio or messages on Discord.
2. The API transcribes the audio and stores the observations in PostgreSQL.
3. The AI uses that history for summaries, profiles, memory and answers.
4. The bot posts text, voice or reactions on Discord.

Transcription and AI have separate providers: Whisper/Speechmatics/Deepgram for audio; OpenAI-, Groq-, Ollama- or ChatGPT-compatible clients for AI. Recovery files and queues allow resuming work after failures.

## How to refer to features

Use the **name**, **ID** or **file** from the [feature index](features/README.md). Example: "change F04 — Voice assistant" or "see `voice-assistant.md`".

This map describes the code and documentation read on **06/10/2026**. It does not confirm a real run on Discord. The [voice conversation plan](../plan/voice-conversation.md) still marks real trials with two people, noise, music and latency/usage measurements as pending.
