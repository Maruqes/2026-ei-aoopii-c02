# Discord Anthropologist

- 31415 Gonçalo Marques
- 31394 Ricardo Fernandes

## PoC

- ~~Scrap chats messages~~
- ~~Scrap voice chats~~
- ~~Use one audio file per user~~
- ~~Voice to text~~
- Build full chat from each user audio file for Agent Context?
- Text -> DB -> Embedding? -> ChromaDB
    - Text messages into DB
    - Voice messages into trascribe into DB
    - Create Chunks for embedding. 30mins interval ish
      -"USERID1_USERNAME1_MESSAGE1" ; "USERID2_USERNAME2_MESSAGE2";"USERID1_USERNAME1_MESSAGE3"
- OpenClaw for autonomous agent??
- API for openclaw usage search_chunks() get_user() update_userReport()

## Real implementation

The production code is split between `src/` and the Go bot in `discord_bot/`.

- `src/data`: Postgres migrations and DB/chunk rebuilding helpers.
- `src/transcription-api`: local FastAPI wrapper around Whisper.
- `docker-compose.yml`: local Postgres and transcription API.
- `BrunoAPI`: Bruno collection for local API requests.

## Voice sessions, summaries, and profiles

The bot now creates a voice session when it joins a voice channel. Finished recordings are attached to that session, and when the session ends the API waits for transcription jobs to finish before running the agent.

- The agent generates one sentence for the session summary and the bot posts it to `DISCORD_SUMMARY_CHANNEL_ID`.
- User profiles are cached in Postgres and can be shown with `/profile user:@name`.
- Profile documents are written as local Markdown files by default under `profiles/`.
- Profile sections are Summary, Interests, Communication Style, Persona Notes, and Recent Updates.
- LLM provider is selected with `LLM_PROVIDER`. Use `xai` for Grok or `ollama` for local free testing.
- For Ollama with Docker, use `OLLAMA_BASE_URL=http://host.docker.internal:11434`. For a direct local API run, use `http://localhost:11434`.
- Recommended local test model: `ollama pull qwen2.5:7b`, then set `OLLAMA_MODEL=qwen2.5:7b`.
- To use local profile files, set `PROFILE_DOCS_PROVIDER=local` and `LOCAL_PROFILE_DIR=/app/profiles`. Docker maps this to `./profiles` in the repo.
