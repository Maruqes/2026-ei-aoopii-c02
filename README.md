# Discord Anthropologist

- 31415 Gonçalo Marques
- 31394 Ricardo Fernandes

## Run

Copy `.env.example` to `.env`, configure Discord and the chosen transcription/LLM provider,
then run `make compose`. Database migrations apply automatically when the API starts.
For NVIDIA Whisper, set `WHISPER_DEVICE=cuda` before using the Makefile.

To use a ChatGPT account for commands, summaries and profiles, set
`LLM_PROVIDER=chatgpt` and follow [ChatGPT setup](CHATGPT_SETUP.md).
Run `make codex` to open the official ChatGPT login in your browser. Discord
`/models` lists, tests and selects models available to that account; `/effort`
changes reasoning effort. Speech-to-text keeps its own provider.

The bot records voice in short per-member slices, stores text/voice observations in
Postgres, builds funny but grounded recaps, and maintains living member profiles.
Use `/digest` to catch up on a text channel, `/recap` for calls, `/oracle` to search the
group's history, `/profile` and `/prompt` for lore, and `/guess` for quote trivia.
`/health`, `/keys` and `/retry` expose progress, consumption and recovery.

Use `/play url:<YouTube video link>` to play audio in your call, `/pause` to pause or
resume, `/skip` to advance, `/queue` to inspect the queue and `/musicstop` to clear it
without stopping conversation recording. Playback accepts public videos up to one
hour, with at most 20 queued tracks. Docker includes yt-dlp, FFmpeg and Node.js.
Playback controls require sharing the bot's voice channel; the bot needs Connect/Speak permissions.
Queues are kept in memory and cleared when the bot disconnects or restarts.

Completed recordings and their Speechmatics recovery files are deleted once the
transcript is committed to Postgres. A startup/periodic sweep retries leftover cleanup.
Pending/failed recordings remain available for `/retry`; `/forget` also removes the
member's audio. YouTube audio is streamed through pipes, with temporary workspaces
removed after success, failure or cancellation.

Accepted recordings and profile updates survive API restarts. Local outbox files replay
failed submissions after a bot restart. Long conversations are summarized in ordered
slices; partial transcripts are identified in their recaps.

Speechmatics percentages compare measured Batch hours with an optional configured
hours budget. They do not represent account credit balance; several keys can share
account usage. See configuration, recovery details and test commands in the
[service documentation](src/transcription-api/README.md).

## Layout

- `discord_bot/`: Go Discord gateway, audio capture, commands and submission outbox.
- `src/transcription-api/`: FastAPI transcription service, LLM prompts, worker queues and profiles.
- `src/data/`: Postgres migrations, persistence and guild context retrieval.
- `docker-compose.yml`: local Postgres, transcription API and Discord bot.
- `BrunoAPI/`: local API request examples.

## Transcrição Realtime (opcional)

Com `TRANSCRIPTION_PROVIDER=speechmatics` e keys configuradas, `/streaming mode:on`
ativa Realtime na chamada atual e guarda a preferência do servidor em Postgres.
`/streaming mode:off` volta a gravações Batch; `mode:status` mostra modo, vagas e fila.
O comando exige **Gerir Servidor** e responde de forma efémera. O valor inicial
`TRANSCRIPTION_STREAMING_ENABLED=false` aplica-se apenas a servidores sem preferência.

Cada segredo de key distinto dá duas reservas; utilizadores humanos são admitidos por
ordem de entrada e mantêm a reserva em silêncio. O excedente usa Batch até à promoção.
Realtime usa `enhanced/pt`, independentemente dos defaults Batch `melia-1/multi`.
O WAV temporário permanece até à confirmação durável; falhas recuperáveis usam Batch,
substituindo apenas o texto provisório daquele WAV. Créditos esgotados em todas as keys
suspendem a transcrição da chamada, eliminam áudio pendente e preservam texto confirmado.

Executar **um único processo da API** para este pool de reservas. Streaming vem desligado
por defeito; não é necessário mudar o comportamento de autojoin. Para detalhes de operação,
recuperação, debug e testes, consultar [a documentação da API](src/transcription-api/README.md#realtime).
