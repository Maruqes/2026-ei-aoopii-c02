# Transcription API

FastAPI transcription service. Local Whisper remains the default provider; Speechmatics
Melia 1 can be enabled through an environment variable.

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r src/transcription-api/requirements.txt
```

Run the API:

```powershell
uvicorn app.main:app --app-dir src/transcription-api --reload
```

Or:

```powershell
make api
```

Select the provider in `.env`:

```text
TRANSCRIPTION_PROVIDER=whisper
```

This preserves the main-branch local Whisper behavior. Its existing `WHISPER_*`,
PyTorch, VAD, CPU, and GPU settings continue to apply.

Docker Compose installs CPU-only PyTorch wheels and runs Whisper with
`WHISPER_DEVICE=cpu` by default. To select the runtime when using the
`Makefile`, set it in `.env`:

```text
# CPU, does not require an NVIDIA driver
WHISPER_DEVICE=cpu
PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
```

```text
# NVIDIA GPU, requires a working NVIDIA driver/container runtime
WHISPER_DEVICE=cuda
PYTORCH_CUDA_INDEX_URL=https://download.pytorch.org/whl/cu126
```

With `WHISPER_DEVICE=cuda`, `make compose`, `make api`, and `make migrate`
add `docker-compose.gpu.yml` automatically. When running
`docker compose` directly instead of `make`, include the GPU override file
explicitly.

Whisper quality settings:

```text
WHISPER_MODEL=large-v3
WHISPER_LANGUAGE=pt
WHISPER_BEAM_SIZE=10
WHISPER_FP16=true
WHISPER_INITIAL_PROMPT=
WHISPER_CARRY_INITIAL_PROMPT=false
WHISPER_CONDITION_ON_PREVIOUS_TEXT=false
WHISPER_HALLUCINATION_SILENCE_THRESHOLD=2.0
WHISPER_MAX_NO_SPEECH_PROB=0.6
WHISPER_NO_SPEECH_THRESHOLD=0.6
WHISPER_LOGPROB_THRESHOLD=-0.8
WHISPER_COMPRESSION_RATIO_THRESHOLD=2.0
WHISPER_VAD_ENABLED=true
WHISPER_VAD_AGGRESSIVENESS=3
WHISPER_VAD_FRAME_MS=30
WHISPER_VAD_PADDING_MS=500
WHISPER_VAD_MIN_SPEECH_MS=400
```

To use Speechmatics instead, create an API key in the
[Speechmatics portal](https://portal.speechmatics.com/) and set:

```text
TRANSCRIPTION_PROVIDER=speechmatics
SPEECHMATICS_API_KEY=your_speechmatics_api_key
SPEECHMATICS_API_KEY_01=
SPEECHMATICS_API_KEY_02=
SPEECHMATICS_API_KEY_03=
SPEECHMATICS_BATCH_URL=https://eu1.asr.api.speechmatics.com/v2
SPEECHMATICS_LANGUAGE=multi
SPEECHMATICS_MODEL=melia-1
```

Speechmatics settings:

```text
SPEECHMATICS_POLLING_INTERVAL_SECONDS=2
SPEECHMATICS_TIMEOUT_SECONDS=600
SPEECHMATICS_SEGMENT_GAP_SECONDS=1.5
SPEECHMATICS_ADDITIONAL_VOCAB=
SPEECHMATICS_USAGE_LIMIT_HOURS=0
SPEECHMATICS_USAGE_SINCE=
```

Multiple Speechmatics API keys can be configured with numbered or named suffixes:

```text
SPEECHMATICS_API_KEY_01=...
SPEECHMATICS_API_KEY_02=...
SPEECHMATICS_API_KEY_03=...
```

When multiple keys are present, usage requests run concurrently and are cached for key
selection for 60 seconds. Keys with the lowest reported hours are preferred; ties rotate.
An unavailable usage endpoint does not prevent trying a configured transcription key.
Repeated copies of the same secret are treated as one key.

`/keys` reports Batch usage for the **account/project accessible by each key**. Several
keys can share that usage; creating a new key does not create a new allowance. The period
is the current UTC calendar month, or `SPEECHMATICS_USAGE_SINCE` through today.
The request sends an explicit `until=today`: EU1 returned today's jobs in the
2026-10-03 investigation despite the documentation excluding today. Today's report is
**provisional**, may lag and is not guaranteed in every region. Local Batch WAVs are
not added to that total, to avoid counting the same job twice.

`/keys` also shows this bot's locally sent Realtime audio for units started in the
same UTC period, including active units. The first audio packet is checkpointed,
then every ~5 seconds while sending audio and again on close. No audio means no
consumption. A crash can lose the interval since the last checkpoint. Model and
key are recorded per unit; historical units without a recorded model are shown
as **unknown**, with no invented cost. Changing the configured model does not
reprice older units as that model.

Cost estimates use each model's hours and the public PAYG base rates checked on
2026-10-03, in USD per hour:

| Model | Batch | Realtime |
| --- | ---: | ---: |
| Melia 1 | $0.24 | — |
| Enhanced | $0.75 | $0.80 |
| Standard | $0.45 | $0.45 |
| Oak 1 | $0.30 | — |

These are gross transcription estimates, excluding model-training/pack/subscription
discounts, grants, taxes and add-ons. They are not an invoice, amount due or credit
balance. Unsupported models or unavailable Batch usage make the estimate **partial**;
known priced rows remain visible. Streaming followed by Batch fallback can consume
both products, so both are included. Keys may share Batch usage: do not sum the
per-key estimates as independent account costs.

Spend levels are display bands, not Speechmatics billing tiers: level 1 below $1,
level 2 from $1 to below $5, level 3 from $5 to below $10, level 4 at $10 or above.
The command shows only one short cost/level line per key. Rate details remain in the API response.

`SPEECHMATICS_USAGE_LIMIT_HOURS` is an optional local hours budget for this period. With
`0` (default), `/keys` displays hours without a percentage. With `50`, 10 hours means 20%
**of that configured budget**, not 20% of the provider's remaining credit balance.
Usage errors are displayed as unavailable, never as zero. Budget alerts follow 10% steps.
See [Speechmatics usage](https://docs.speechmatics.com/administration/usage) and
[UTC reporting semantics](https://legacy.docs.speechmatics.com/en/cloud/understanding-saas-usage/).

Melia 1 automatically handles multilingual audio and language switching, including
Portuguese and English. It does not support custom vocabulary. Speechmatics returns
word timestamps; the API groups them at sentence boundaries or after
`SPEECHMATICS_SEGMENT_GAP_SECONDS` of silence before inserting messages into Postgres.
Changing `TRANSCRIPTION_PROVIDER` requires restarting the API container:

```powershell
docker compose up -d --build api
```

Transcribe an audio file:

```powershell
curl -X POST http://localhost:8000/v1/transcriptions `
  -F "recording_filename=123-example.wav" `
  -F "discord_id=123" `
  -F "username=Ricardo" `
  -F "channel_name=general" `
  -F "recording_started_at=2026-04-28T10:00:00Z"
```

The API returns `200` after persisting the recording metadata in Postgres. Bounded
workers process the durable queue; accepted jobs survive API restarts. Reposting the
same recording is idempotent. New recordings cannot be admitted into an already-closed
session. Speechmatics job IDs and key names are persisted before polling, allowing
recovery to poll the original job rather than create another paid job. A local job-ID
sidecar also covers a database failure after submission. If submission itself times out
before a job ID is returned, its outcome is unknown; an explicit retry may submit again.
It expects `recording_filename` to exist inside `RECORDINGS_DIR`. Locally, the default is
`discord_bot/recordings`; in Docker Compose, `./discord_bot/recordings` is mounted into
the API container as `/app/recordings`, so the Go bot and Python API share the same WAV files.

Session flow:

```powershell
curl -X POST http://localhost:8000/v1/sessions `
  -H "Content-Type: application/json" `
  -d '{"guild_id":"guild","voice_channel_id":"voice","channel_name":"General","summary_channel_id":"text"}'

curl -X POST http://localhost:8000/v1/sessions/1/finish `
  -H "Content-Type: application/json" `
  -d '{}'

curl http://localhost:8000/v1/sessions/1/summary
curl http://localhost:8000/v1/users/123/profile
```

Use any OpenAI-compatible chat completions API:

```text
LLM_PROVIDER=openai
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

The Discord `/models` command lists models from the configured provider. Selecting one sends a
small `Ola!` test prompt and only activates the model if that request succeeds. The selection is
persisted in `LLM_MODEL_SELECTION_FILE` (default `.tmp/llm_model_selection.json`) and overrides
the model configured in the environment after restart. If no model has been persisted yet, the
environment model is used.

With `LLM_PROVIDER=chatgpt`, `/models` fetches the signed-in account's catalog directly from
`https://api.openai.com/v1/models` on each request. All entries with valid model IDs are included,
even those marked hidden or without visibility metadata, and Discord paginates the choices.
Models that fail the test prompt are not activated.

For Groq, set `OPENAI_BASE_URL=https://api.groq.com/openai/v1` and choose a Groq model such as
`llama-3.3-70b-versatile`. The legacy `LLM_PROVIDER=groq` and `GROQ_*` environment variables are
still accepted for existing local setups.

For OpenCode Go, an active Go plan is required (https://opencode.ai/console). The chat API is
OpenAI-compatible, but its model catalog uses a separate URL, so this provider has its own branch:

```text
LLM_PROVIDER=opencode-go
OPENCODE_API_KEY=your_api_key
OPENCODE_BASE_URL=https://opencode.ai/inference/go/openai/v1
OPENCODE_MODELS_BASE_URL=https://opencode.ai/inference/go/v1
OPENCODE_MODEL=deepseek-v4.1-flash
```

Every request sends `x-opencode-session` (one generated id per API process, or `OPENCODE_SESSION_ID`
when set) and a `discord-anthropologist/1.0` User-Agent. DeepSeek models require
Regions → Global in the OpenCode Console. `/models` reads the catalog URL, so model selection works
like the other providers. See [OPENCODE.md](../../OPENCODE.md) for setup and troubleshooting.

Use Ollama for free local testing:

```powershell
ollama pull qwen2.5:7b
```

Then set:

```text
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

Use `http://localhost:11434` for `OLLAMA_BASE_URL` only when the API is running directly on the host instead of inside Docker.


## Intelligence and recovery

The API applies migrations on startup, including `005_durable_jobs.sql`; `make migrate`
remains available. Keep the Postgres and recordings volumes when rebuilding. Failed
legacy recordings without metadata are marked failed rather than blocking summaries forever.

Voice and text conversations above `LLM_CONTEXT_CHARS` are distilled in chronological
slices before synthesis. Prompts preserve speakers, dates, topic changes, explicit decisions,
owners and stated deadlines. Recaps end with memorable moments when the evidence supports
them. `/oracle` and `/prompt` answer directly in natural prose with playful irony grounded in
the supplied evidence. `/profile` descriptions use the same tone when profiles are generated or
updated. New voice recaps and `/digest` summaries group topics and weave humor into the recap,
using chronology only when the sequence matters. Dates and uncertainty are included when they
help answer the question; humor never adds invented facts or quotes. Already stored profiles and
recaps keep their previous wording until regenerated.
The oracle retrieves older messages matching question terms within the requested guild.
No voice context is selected solely by a channel name shared across servers.

Profile updates preserve supported earlier facts, distinguish isolated remarks from
recurring patterns and validate JSON before writing. Profiles use stable member IDs in
filenames and atomic writes. Summaries publish before profile work; pending voice profiles
are stored in their own durable queue. Voice and text updates for the same member are
serialized. Text updates use a snapshot watermark so messages arriving during a batch
are kept for the next batch.

Settings:

```text
LLM_TIMEOUT_SECONDS=90
LLM_CONTEXT_CHARS=24000
LLM_MAX_OUTPUT_TOKENS=2500
TRANSCRIPTION_WORKERS=2
TEXT_PROFILE_SYNC_ENABLED=true
TEXT_PROFILE_SYNC_INTERVAL_HOURS=12
```

Hosted providers retry one transient failure through the SDK. Ollama has an explicit
request deadline. Truncated completions and malformed/empty profiles are rejected;
existing profiles remain available. Character budgets approximate model token budgets;
choose a smaller value for a small-context local model.

## Discord commands

- `/digest hours:24`: recap the invoking text channel over 1–168 hours, including
  decisions, loose ends and highlights. At most the latest 2000 stored messages are
  covered, and the response says when coverage is limited.
- `/recap session:123`: view the latest/specified voice session summary, or the captured
  transcript while processing.
- `/retry session:123`: recover a failed/partial session. Completed recordings are not
  retranscribed; failed jobs retain their remote IDs. Then use `/recap` to read the result.
- `/oracle question:...`: answer from guild history and relevant older messages.
- `/profile` and `/prompt`: show the member profile or ask about their lore.
- `/health`: show recording, summary and profile queues, failures and the active model.
- `/keys`: inspect measured Batch hours and an optional configured budget.
- `/guess`: guess a speaker from an actual captured voice quote.
- `/play url:...`: play a public YouTube video in the caller's voice channel.
- `/pause`, `/skip`, `/queue`, `/musicstop`: pause/resume, advance, inspect or clear playback.

YouTube playback runs in the Go bot using yt-dlp, FFmpeg and Node.js (included in its
Docker image). Native launches need those executables on `PATH`, including a supported
Node.js runtime (22+) and `yt-dlp[default]` with EJS; see the
[official runtime setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
Videos must be public, finite and at most one hour long; playlists are not imported.
The queue holds at most 20 waiting tracks and is cleared on disconnect/restart.
Only members in the bot's voice channel can control playback. The bot needs Connect
and Speak permissions and must not be server-muted. `/musicstop` leaves transcription
active, while `/stop` disconnects the bot and ends playback. The bot's own voice packets
are excluded from transcripts; audio echoed through a member's microphone may still be
captured. YouTube can reject extraction from particular server IPs or restrict a video;
failed tracks are reported in the session's summary channel and the queue continues.
Update the pinned yt-dlp package and rebuild the bot image if YouTube changes break extraction.

Commands that fetch data acknowledge the Discord interaction before calling the API.
Generated messages disable automatic mentions. Changing global bot settings/models and
retrying sessions requires Manage Server; `/forget` can delete your own data, while
removing someone else's data requires Manage Server. Bot language, model and enable/leave
settings currently apply globally to this bot process, including when it joins multiple guilds.
Profiles currently aggregate a member's observations across guilds; oracle/digest context is guild-scoped.

Audio settings (bot):

```text
RECORDING_IDLE_SECONDS=10
RECORDING_MAX_SECONDS=300
RECORDING_CLEANUP_INTERVAL_SECONDS=60
SESSION_SUMMARY_TIMEOUT=30m
SESSION_SUMMARY_POLL_INTERVAL=5s
API_REQUEST_TIMEOUT=5m
```

Short WAV slices are submitted during a call. Pauses close the current slice and the next
slice receives its own UTC timestamp, saving long silence without shifting the transcript.
Discord audio is decoded once into per-member stereo PCM WAV (48 kHz, 16 bit),
without another lossy encoding. A 60 ms window reorders RTP packets per speaker
before Opus decoding and removes duplicates; missing packets use the existing
bounded Opus FEC/PLC recovery. Buffered audio is flushed at recording boundaries.
Realtime silence waits for that window and discounts padding from RTP gaps.
If late speech overlaps silence already sent upstream, the bot replaces those
trailing zeros in the WAV and falls back to Batch, preserving the full speech
instead of dropping it or extending the recording. This cannot restore microphone
clipping, background noise or speech lost beyond recovery; recognition quality
still requires a real call trial.
The bot waits for recording admission before finishing a session. Failed submissions stay
in `.request.json` outbox files; startup replays them. Finish intent markers recover a crash
between admission and session finish. API retries are bounded; `/recap` remains available
if waiting for a posted summary exceeds the configured deadline.

Completed raw recordings and their Speechmatics recovery sidecars are deleted after
the transcript is committed to Postgres. Summaries and profiles use the saved text.
A cleanup worker starts with the API and sweeps every 60 seconds (configurable with
`RECORDING_CLEANUP_INTERVAL_SECONDS`) so a restart or temporary filesystem error does
not leave completed audio indefinitely. Cleanup failures do not undo transcription.
Pending, active and failed recordings are retained for `/retry`, as are unacknowledged
bot outbox requests. `/forget` also removes that member's audio and recovery files.
If a transcription worker is currently using that member's audio, `/forget` asks you
to retry once it finishes instead of deleting a file that the provider is still reading.
Files with no matching database record are preserved because they may still be waiting
for admission; the automatic sweep does not guess ownership or completion from age.
The legacy `KEEP_UPLOADS` flag does not prevent deletion of completed audio.

YouTube audio/video is not downloaded to media files: yt-dlp returns metadata and
FFmpeg streams decoded audio through a pipe. Both run in isolated temporary workspaces
that are removed after their processes terminate, including on errors, skip and stop.
The backend request deadline is configurable up to 15 minutes; reads are capped at
30 seconds and recording admission/session updates at 20 seconds so a stalled API
does not leave Discord commands waiting indefinitely.

## Tests without paid API calls

For ChatGPT account login, model selection and VM setup, see
[CHATGPT_SETUP.md](../../CHATGPT_SETUP.md). Select `LLM_PROVIDER=chatgpt` to use
the signed-in account for every LLM feature. Run `make codex` to sign in;
Discord `/models` changes the model and `/effort level:low` changes reasoning effort.
The initial effort is set with `CHATGPT_REASONING_EFFORT` in `.env`.

```bash
python -m venv .venv
.venv/bin/pip install -r src/transcription-api/requirements.txt -r src/transcription-api/requirements-dev.txt
PYTHONPATH=src:src/transcription-api .venv/bin/pytest -q src/transcription-api/tests
(cd discord_bot && go test -race ./...)
```

To include integration tests, set `TEST_DATABASE_URL` to a disposable Postgres database.
Tests create/drop their own schemas and use simulated providers; no Discord or paid AI
credentials are needed. Go tests require the Opus and opusfile development libraries.

## Realtime

Configuration in the API (also exposed in Compose):

```dotenv
TRANSCRIPTION_PROVIDER=speechmatics
TRANSCRIPTION_STREAMING_ENABLED=false
TRANSCRIPTION_STREAMING_DEBUG=false
SPEECHMATICS_REALTIME_URL=wss://eu.rt.speechmatics.com/v2/
SPEECHMATICS_REALTIME_MODEL=enhanced
SPEECHMATICS_REALTIME_LANGUAGE=pt
```

Existing Batch keys are reused only in Python. Each distinct secret has at most two
reservations, assuming one account per secret as decided in the project.
The provider's real limit is per account; `quota_exceeded`/close `4005` implies fallback,
**not** discard. Only `timelimit_exceeded`/close `4006` or HTTP `402` confirms a lack of
credits. Budget percentages and usage queries are not a credit balance.

The bot sends the ordered presence to `POST /v1/sessions/{id}/streaming`. The API reserves
slots atomically in its single event loop. `GET/POST /v1/guilds/{guild}/streaming` reads/writes
the per-server mode; the environment only sets the default. Use a single replica and a single
Uvicorn worker. A second call continues recording without creating another pool.

`/v1/streaming/audio` is an internal WebSocket on the same private network as the existing API.
The first JSON message contains the session, user, WAV filename, absolute timestamp
and participation token. The API waits for `RecognitionStarted` before confirming `ready`.
Binary frames contain a uint64 little-endian sequence and mono PCM S16LE at 48 kHz.
Go downmixes into int32, copies the Opus buffer and queues up to 100 chunks per epoch;
a full queue aborts that transport and keeps the WAV for Batch. Heartbeats, message
limits and timeouts prevent unbounded growth and network operations in the capture loop.

Each WAV is an independent unit and epoch; the reservation survives closing due to silence,
rotation and a new SSRC. Promotion and mode boundaries close the previous WAV before
creating the next one. The API stores idempotent finals with `recording_id` and generation.
Partials are active only to tell the bot about speech timings still pending
finalisation; partial text only appears in the terminal when debug is enabled.
`EndOfStream` includes the final sequence;
the commit requires `EndOfTranscript` and a match between frames sent and the closed WAV.

A failure leaves the unit in `fallback_pending`. Batch admission invalidates the Realtime
generation; the Batch commit replaces that unit's messages and recalculates the chunks in the
same transaction. Final summaries and profiles wait for pending units and jobs. `/recap`
can read provisional finals; `/forget` pauses capture, invalidates units, cancels remote
polling and removes WAVs/sidecars before confirming. Remote delete is attempted with
`force=True`; even if it fails, delayed results cannot recreate data.

The safety outbox is written before opening Realtime. When the bot restarts, WAVs with an
incomplete header are repaired before replay. When the API restarts, Realtime units
stay in fallback; the worker recovers files stable for at least 15 seconds.
Completed or discarded units are not transcribed again by a restart or `/retry`.

All keys confirmed to be without credits cause a terminal discard in Postgres **before**
cleanup. Workers stop polling and try to cancel remote jobs; file cleanup confined
to the recordings folder is idempotent and repeated after a failure/restart.
A bot worker recovers pending credit notices, using a per-episode nonce and persisted
confirmation to avoid repetition. Discord only deduplicates nonces for a few
minutes; a prolonged simultaneous failure of the bot and of the confirmation after sending may require
manual reconciliation of that notice. `/streaming mode:on` or a new call re-evaluates the
keys without recovering discarded audio.

`GET /v1/speechmatics/realtime-usage` reports the full history of local PCM hours
sent per key. `GET /v1/transcription/keys` filters those units by the UTC period
and shows the model and estimated cost, separate from the Batch report. A reservation in silence does not count as audio; a WAV
recovered by Batch can consume both products. Normal logs contain neither transcribed
text nor credentials. The codes follow the
[Speechmatics Realtime reference](https://docs.speechmatics.com/api-ref/realtime-transcription-websocket).

Verification without a paid provider or real Discord:

```bash
# Set TEST_DATABASE_URL only for a disposable Postgres.
TEST_DATABASE_URL=postgresql://... PYTHONPATH=src:src/transcription-api python -m pytest -q src/transcription-api/tests
(cd discord_bot && go test -race ./...)
```

The tests use a local WebSocket server and Postgres isolated per schema. The first
real trial should use a controlled call, one key and debug enabled; validate PT-PT,
gaps and flush, then several keys. Turn debug off at the end. The real trial requires audio and
access to Speechmatics and is not replaced by the simulated tests.

### Continuous memory and reactions

`GROUP_MEMORY_ENABLED=true` starts a worker that checks closed windows every
15 seconds. Each window lasts `GROUP_MEMORY_BULK_MINUTES` (1–60, default 5).
The first start considers the last `GROUP_MEMORY_CONTEXT_BULKS` (1–3, default 3)
blocks; the starting point, the blocks and the origin of the observations are stored in Postgres.
The worker groups by receipt (`messages.observed_at`), preserving `tstamp` as the
original speech date: a late Batch recovery does not lose the observation. Empty windows
do not call the LLM. Summaries/lore survive restarts and enter the context of
`/oracle` and “Olá macaco”; question terms also search older blocks.

Profiles use only each member's messages, with the existing locks and one
observation identified per block. Generation failures leave the sources unprocessed;
profile failures stay pending. `/forget` invalidates the blocks derived from the affected
servers so they are rebuilt only with the remaining members, even after transcripts
are replaced. Recent blocks feed jokes/callbacks; old, repeated
or uninteresting interventions are suppressed. The minimum between generated interventions is
`GROUP_MEMORY_REACTION_COOLDOWN_MINUTES` (default 10).

`GET /v1/guilds/{guild}/memory` inspects the blocks. `GET /v1/memory/reactions`
returns the pending recent reactions; the bot checks destination/permissions and availability,
then does `POST /v1/memory/reactions/{id}/claim` and publishes text, optionally with a
GIPHY meme (`GIPHY_API_KEY` in the bot); it also allows GIF-only reactions, without voice,
with the same cooldown. The bot sends the animated file URL, avoids the last 20 IDs
per server and tries a second page if needed, within a maximum total of 5 seconds.
GIPHY failures stay in the logs and preserve the text; if a GIF-only reaction fails,
it does not send an empty message. The destination needs **Embed Links** to show the GIF.
It does `POST .../{id}/result` with `sent`/`failed`.
The claim is atomic; it does not repeat ambiguous result submissions or claims after a crash.
A reaction expires one block after its end. Voice reuses the existing TTS, waits for
Realtime silence and stopped music and is cancelled by new speech. Without full
monitoring, chat reactions remain available. There is no access to the private GIF store/favourites.
`GROUP_MEMORY_REACTIONS_ENABLED=false` keeps memory without publishing reactions.

### Hey Bot contract (explicit requests)

`GET/POST /v1/guilds/{guild}/assistant` reads/changes `enabled`, `phrase` and `channel_id`
in the existing settings table. The monotonic revision invalidates old configurations;
the write returns success only after the commit. The streaming preference remains
independent; a row created only for the assistant inherits the environment's streaming default.

`POST /v1/assistant/question` receives `{"question":"Explica polimorfismo?"}` and returns
`question`/`answer`. The bot also sends `session_id`, `discord_id`, `username` and
`display_name`: the API stores the question and the answer in the `messages` table with origin
`assistant`, queries the voice finals from the last five minutes of that call
(up to 500 messages, in temporal order, including Realtime units still open)
and starts updating the author's profile/lore in the background. Generated answers are identified
as bot context and are not biographical evidence. Failed updates stay
pending for `/syncprofiles` or the periodic sync; the conversation already stored
becomes available in persisted memory and in `/oracle`. Profiles keep aggregating
the member's observations across servers, as in voice and text updates.
Without session/author identity, it keeps a general answer without storing memory.
The bot also sends `history`, a list of up to 24 messages with `role` (`user` or
`assistant`), `content` and optional `interrupted` for bot voice already published and cut off.
Each author turn accepts up to 2000 characters; a response in the history accepts up to
12000. History requires full call/author identity and preserves earlier
questions still without a delivered answer. The prompt identifies the author, prioritises
recent turns, fills the remaining budget with call context and signals
partial coverage. `LLM_CONTEXT_CHARS` limits the full prompt, without cutting the current
question or using an extra call for summarisation. The active history is transient.
It uses the selected LLM client, PT-PT, 2–4 sentences by default and a 30-second generation
timeout, without tools.
It is an internal endpoint; Discord permissions are checked in the bot.

The WebSocket now returns `ready` with `recording_id`/`generation` and `final` events
only after persisting each new segment. Each final includes `session_id`,
`discord_id`, `recording_id`, `generation`, `identity`, `text`, `start`, `end` and `words`
with timings relative to that unit's PCM. Punctuation is preserved in the words.
Numbers and dates formatted as entities are also part of `words`.
`speech` events carry only `session_id`, `discord_id`, `recording_id`, `generation`,
`start` and `end`: they are emitted only for non-empty partials from the provider. They indicate
speech still pending finalisation, without text or activation; they can interrupt the answer and
open a turn when a conversation with that author already exists.
Duplicates, partials and Batch recovery do not generate activation events. A
persistence/delivery failure ends the stream with `fallback`; the bot discards the incomplete turn,
warns once and keeps the conversation until the timeout. Realtime recovery allows
repeating; Batch does not deliver old speech to the conversation.

In silence/DTX, Go sends zeros to Realtime and writes the same frames to the WAV.
The clock and RTP boundaries discount that padding to avoid duplicated silence.
Idle close still releases the transport, preserving the reservation and the possibility
of opening a new stream on the next speech. Those sent frames count towards Realtime usage.
Assistant logs measure up to publication, from the sentence's audio time and from the
last PCM speech or final word of the question; they do not record questions or answers. A failed
Discord publication is not repeated automatically.


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
