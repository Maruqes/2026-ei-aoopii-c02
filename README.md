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
