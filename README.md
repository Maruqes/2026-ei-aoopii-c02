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

## Real implementation

The production code is split between `src/` and the Go bot in `discord_bot/`.

- `src/data`: Postgres migrations and DB/chunk rebuilding helpers.
- `src/transcription-api`: FastAPI transcription service with local Whisper or Speechmatics.
- `docker-compose.yml`: local Postgres, transcription API, and Discord bot.
- `BrunoAPI`: Bruno collection for local API requests.
