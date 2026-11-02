# Discord Anthropologist

- 31415 Gonçalo Marques
- 31394 Ricardo Fernandes

## Project map

See the [project context](docs/CONTEXT.md) and the [feature index](docs/features/README.md) to find each feature by name, ID and the files where it is implemented.

## Run

Copy `.env.example` to `.env`, configure Discord and the chosen transcription/LLM provider,
then run `make compose`. Database migrations apply automatically when the API starts.
For NVIDIA Whisper, set `WHISPER_DEVICE=cuda` before using the Makefile.

To use a ChatGPT account for commands, summaries and profiles, set
`LLM_PROVIDER=chatgpt` and follow [ChatGPT setup](CHATGPT_SETUP.md).
Run `make codex` to open the official ChatGPT login in your browser. Discord
`/models` lists, tests and selects models available to that account; `/effort`
changes reasoning effort. Speech-to-text keeps its own provider.

Alternatively, set `LLM_PROVIDER=opencode-go` and `OPENCODE_API_KEY` to use an OpenCode Go plan.
`/models` reads its catalog and the other AI paths work unchanged; no ChatGPT login is needed.
See [OpenCode Go setup](OPENCODE.md).

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

## Word triggers in the call

In `discord_bot/main.go`, register the triggers on startup:

```go
triggerSaidKeyworkd(sayPijama, "pijama")
triggerSaidKeyworkd(funcDarAudio, "bot", "audio")
// Also accepts a list: triggerSaidKeyworkd(funcDarAudio, []string{"bot", "audio"}...)
```

The function receives `SaidKeywordContext` and returns `error`; the context includes the
Discord session, server, text channel, author and recognised text. All words must
appear in the same recording by the same author, in any order. It ignores case
and punctuation, compares whole words and runs each trigger once per recording.
A function error allows another attempt on the next poll.

The "pijama" example is already registered: it sends `pijama` to the server's system
channel, or the first text channel where the bot can write. Transcriptions
are polled every second: Realtime reacts to confirmed text; Batch reacts
when the recording transcription becomes available. It needs no LLM or extra
dependencies. Registrations and deduplication are kept in memory.

## Real-time transcription (optional)

With `TRANSCRIPTION_PROVIDER=speechmatics` and configured keys, `/streaming mode:on`
enables Realtime in the current call and saves the server preference in Postgres.
`/streaming mode:off` returns to Batch recordings; `mode:status` shows mode, slots and queue.
The command requires **Manage Server** and replies ephemerally. The initial value
`TRANSCRIPTION_STREAMING_ENABLED=false` applies only to servers without a preference.

Each distinct key secret grants two reservations; human users are admitted
first-come-first-served and keep the reservation while silent. The surplus uses Batch until promotion.
Realtime uses `enhanced/pt`, regardless of the Batch defaults `melia-1/multi`.
The temporary WAV remains until durable confirmation; recoverable failures use Batch,
replacing only that WAV's provisional text. Credits exhausted across all keys
suspend call transcription, delete pending audio and preserve confirmed text.

Run **a single API process** for this reservation pool. Streaming is off
by default; there is no need to change autojoin behaviour. For operating details,
recovery, debug and tests, see [the API documentation](src/transcription-api/README.md#realtime).

## Voice assistant: questions and answers in the chat

With Realtime active (`/streaming mode:on`), the assistant is available by default
to humans with a reservation in the call. Say "Olá macaco" to hear "Diz" and receive
the confirmation in the chat. Ask the question and keep the conversation going without repeating activation;
it also accepts "Olá macaco, explica polimorfismo…". "Adeus macaco" or "obrigado,
adeus macaco" ends with a short goodbye. The assistant replies in PT-PT,
with 2–4 sentences by default and more detail on request, using the last five minutes
of the current call and up to 12 recent dialogue turns. Each conversation is saved and updates the profile and
lore of the speaker in the background, without waiting for the call to end. The bot's
replies are identified context, never facts about the person. It does not run commands.

- `/assistant status`: state, phrase, destination and participants with/without Realtime.
- `/assistant phrase value:"Olá macaco"`: phrase with 2–5 words, up to 50 characters.
- `/assistant channel value:#bot`: destination for confirmations and replies.
- `/assistant enable` and `/assistant disable`: turn on/off per server.

The phrase is searched at the start, middle or end of the utterance, ignoring case,
accents and punctuation. It accepts a small recognition error, up to two
interleaved words and joined/separated words: "olha macaco", "olá meu macaco" and
"olamacaco" also activate. The question before and after activation is preserved,
even when split across finals; a pause longer than the configured silence time
separates utterances.
`/assistant phrase value:"outra frase"` changes the phrase used by the server.

Memory also follows the conversation without the activation phrase: on each closed bulk
of `GROUP_MEMORY_BULK_MINUTES` (5 by default), it stores topics, decisions and lore, and updates
participants' profiles. `GROUP_MEMORY_CONTEXT_BULKS=3` gives reactions the last
3 bulks (accepts 1–3); the stored history remains searchable in `/oracle`.
The bot can comment or make a joke in text and, during a call pause, in voice.
It waits for the assistant, music and speech; new speech cancels the spontaneous voice.
Without full Realtime coverage, it posts in the chat, without voice. It is not required to make a joke
per bulk and respects `GROUP_MEMORY_REACTION_COOLDOWN_MINUTES=10`.

GIFs use actions, scenes and visual analogies linked to the conversation, only when the joke
arises naturally. It can post text with a GIF or just a GIF, without voice in the latter case.
The GIPHY search with `GIPHY_API_KEY` uses short English terms, chooses among the
best new results and avoids the last 20 IDs per server while the bot is
running. If the first page is exhausted, it tries a second within the same
5-second limit. It posts the animated `.gif` file, preferring a compact version,
instead of the GIPHY page; the channel needs the **Embed Links** permission to
show the animation. Search failures preserve the text; a GIF-only reaction that
fails is marked `failed`, without sending empty messages. Logs distinguish
missing search/key, GIPHY errors and exhausted results. The integration does not access
Discord favourites or the private GIF picker; without a key, it continues with
text/voice. `ASSISTANT_VOICE_ENABLED=false`
turns off voice. `GROUP_MEMORY_REACTIONS_ENABLED=false` turns off interventions while keeping
memory and profiles; `/assistant disable` silences that server's reactions.

Changes require **Manage Server**, are persisted and cancel pending requests.
The initial destination is the call's summary channel; without access to a valid destination,
the assistant does not start requests. Only the activating author supplies the question. There is one
conversation at a time; other activations receive a busy notice, without a queue.
All of the author's speech during the conversation is addressed to the bot; other voices serve
as context. New recognised speech from the author interrupts generation or voice, preserves
a question still without a delivered answer and starts the next turn. If the answer
was already in the chat, the history identifies the interrupted voice. "Cancela" as an isolated
utterance ends the conversation; leaving the call also cancels immediately.

The question joins segments by the same author and ends after a continuous period
without sound from that author. Set `ASSISTANT_SILENCE_SECONDS=5` in `.env` to wait
5 seconds (default; accepts 0.5–20 seconds, with a period in decimal values).
Speaking again restarts the counter; other voices and delayed finals do not restart it.
The counter uses received audio and recognised speech times, to include quiet
speech. After silence, it still waits for finals corresponding to the audio.
Only final text
enters the AI request; numbers, dates and punctuation are preserved.
`ASSISTANT_INACTIVITY_SECONDS=30` gives 30 available seconds to answer and closes
the conversation silently if there is no speech. Generation and voice suspend that clock;
the end of a reply or a recoverable warning opens a new window.
`ASSISTANT_CAPTURE_SECONDS=60` limits each turn to 60 seconds; both values
accept 5–300 seconds. The 2000-character limit also remains: a truncated
question is never sent. The LLM keeps the independent 30-second deadline.
Missing finals, streaming failures or LLM failures produce a short warning and keep the
conversation so you can repeat without activation. Batch still serves recordings, but never
activates or feeds assistant turns.
Overlapping times in finals do not cancel capture; already received words and
empty markers do not repeat the question. Invalid times continue to be rejected.
Limits close only the turn: the same person can continue the conversation.
A normal recording/SSRC change keeps the question and waits for the previous recording's
finals, ordered by audio times. Silence or empty finals after activation
do not bring forward the deadline to start the question. Speechmatics uses finals with
`max_delay=2` and flexible mode, preserving number and date formatting.
Voice is synthesised in segments; the confirmation and the chat reply are posted
when the first audio is ready to play. If synthesis fails, it keeps the
reply in text. A question already included in the activation skips the spoken "Diz".
The confirmation only happens at the opening; the active history is kept in memory and cleared after closing
or restart. The full prompt respects `LLM_CONTEXT_CHARS`: it prioritises the recent
dialogue and identifies partial coverage when space is missing, without summarising each turn
through another LLM call.

The existing Realtime reservation limit remains. `/assistant status` and coverage
warnings identify who lacks a Realtime reservation. Warnings wait for the first
synchronisation and 15 seconds of stable state, with at most one warning per minute.
Coverage is not guaranteed
for everyone without confirming account capacity. Silence sent to
close utterances also consumes Realtime minutes; check `/keys` and the
`assistant activation`, `assistant turn closed`, `assistant delivery` and
`assistant response` logs to measure opening, closing, first delivery and end; `/keys`
shows usage. `voice_ready=true` identifies delivery at the start of the voice.

Speech detection initially uses PCM energy (`ASSISTANT_SPEECH_RMS=500`); it should be validated with real microphones,
noise and music. The Discord trial, capacity and latency targets are still
pending.

The assistant also reads the reply into the call with a local European Portuguese
voice using [Piper](https://github.com/OHF-Voice/piper1-gpl) and the Tugão neural voice,
included in Docker. Speech uses phonemes 30% longer, without changing pitch, and pauses
of 400 ms between sentences. It needs no other key or network during synthesis.
Music yields output during speech and resumes at the same
point; a manual pause is kept. The full reply stays in the chat even if
synthesis fails. For long replies, it speaks up to 2000 characters and points to the chat
for the rest. Disabling the assistant, changing configuration or leaving the call
cancels ongoing speech.

Emojis in the reply give expression to the immediately preceding sentence (or the first
sentence, when they appear at the start): 😊/😄/🎉 make the voice happier, 😂/🤣/😆
add a short laugh, 😢/😔/💔 soften and slow the voice, and 😭 adds a
slight tremble. 😮/😱/🤯 indicate surprise, 😡/😠 irritation, ❤️/🥰/🙏 affection,
😉/😜/😏 playfulness, 🤔 thought and 😴 tiredness. Symbols and skin colour
variants are not read; repeated emojis do not repeat the laugh. The chat text stays.
These are approximations through tone, rhythm, volume and synthesised vocalisations: Tugão is not an
emotional acting model and does not produce natural crying or laughter.
The model loads once per reply; after 12 expression changes, the
remaining text is read with the normal voice.

`ASSISTANT_VOICE_ENABLED=true` is the default. Use `false` for chat-only replies.
After updating, rebuild the API and bot with
`docker compose up -d --build api discord-bot`.
To run outside Docker, install `piper-tts==1.8.0` and download the
[Tugão model](https://huggingface.co/rhasspy/piper-voices/tree/v1.0.0/pt/pt_PT/tug%C3%A3o/medium)
and the `.onnx.json` configuration into the same directory. Put `piper` in `PATH` and
set `ASSISTANT_VOICE_MODEL` to the `.onnx` path (22 050 Hz, mono).
The [continuous conversation plan](plan/voice-conversation.md) keeps the Discord trial,
first voice/usage measurements and the evaluation with two people, noise and music
pending.


## Deepgram and Speechmatics

Set `DEEPGRAM_API_KEY` (or numbered `DEEPGRAM_API_KEY_01`, `_02`, …) in `.env`, then
set `TRANSCRIPTION_PROVIDER_ORDER=deepgram,speechmatics`. Rebuild/restart the API and
bot with `make up`. Migration `009_transcription_providers.sql` is applied on API
startup. Secrets and project associations are read only by the Python API.

`/stt status` shows the effective order; `/stt order providers:speechmatics,deepgram`
switches it without restarting. The four choices include either provider alone.
Preferences persist per server, override the environment, and apply to new WAV units.
A healthy stream finishes its current unit before changing provider. `/streaming`
controls streaming independently, and `/keys` shows both providers even when one is
only a fallback. With an empty/absent order, the legacy `TRANSCRIPTION_PROVIDER`
continues to select Whisper or one remote provider.

Deepgram uses Nova-3, `pt-PT`, mono 48 kHz streaming, and mono WAV uploads derived
from the original safety recording. `DEEPGRAM_KEYTERMS` supplies comma-separated
terms to both modes. REST and WebSocket URLs must use the same host/region; for EU
use `https://api.eu.deepgram.com/v1` and `wss://api.eu.deepgram.com/v1/listen`.

Deepgram limits are shared per project, not multiplied per key. Unknown associations
share one group and also observe an aggregate ceiling. Configure `DEEPGRAM_PROJECT_ID`
and optional suffix overrides (`DEEPGRAM_PROJECT_ID_02`,
`DEEPGRAM_STREAMING_LIMIT_02`, `DEEPGRAM_BATCH_LIMIT_02`) only for actual account
associations/quotas. The default local ceilings are 150 streams and 50 WAV requests.
When management permissions identify a project, counters merge and the smallest
configured group ceiling wins. Speechmatics retains its local two-streams-per-key
configuration. Admission counters require one API process.

Capacity errors cool down the affected product/group for ten seconds; invalid
credentials affect the key/mode, and confirmed credit failures affect the billing
group. Fallback works in either order. If all streams are occupied, capture continues
as WAV and participants wait FIFO. A failed stream immediately ends its WAV unit,
recovers it through the durable queue, and gets a fresh assignment. Batch recovery
replaces the unit's text atomically and never sends old speech to the assistant.
Only confirmed exhaustion of every allowed configured provider, without pending
remote work/results, permits the existing credit-discard policy. Authentication and
temporary failures preserve pending audio; temporary failures retry after 15 seconds.
`/streaming on` explicitly revalidates locally rejected credentials after correction.

Deepgram successful REST results and request IDs are atomically saved in
`<recording>.deepgram.json` before final DB persistence; they can be reused after
restart. Speechmatics job IDs/old sidecars retain their original key identity.
A lost HTTP response can require a second billed request. `/forget`, normal cleanup,
and credit cleanup include both providers' sidecars and derived WAVs.

`/keys` shows credits, streams in use against the configured local limit, key health,
and estimated costs in a compact summary. Deepgram balances require `billing:read`;
a 403 is shown as missing permission, and unknown balances never look like zero.
Management failures never disable transcription. Detailed local audio hours, models,
provider usage and pricing remain in `GET /v1/transcription/keys`. Shared balances
and remote Speechmatics usage must not be added per key. Cost estimates exclude
grants, discounts and taxes. `/health` and `/streaming status` identify active providers.

Validation uses local REST/WebSocket simulators and a disposable PostgreSQL database:
`TEST_DATABASE_URL=... pytest` and `cd discord_bot && go test -race ./...`.
Before choosing a provider on recognition quality, compare the same real PT-PT audio
against a human reference; the simulators validate protocol/recovery, not accuracy.
Rollback: choose Speechmatics alone with `/stt order`, or disable streaming. Complete
Deepgram units before downgrading the binary; leave the additive migration in place.
