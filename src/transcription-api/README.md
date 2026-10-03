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

Configuração na API (também exposta no Compose):

```dotenv
TRANSCRIPTION_PROVIDER=speechmatics
TRANSCRIPTION_STREAMING_ENABLED=false
TRANSCRIPTION_STREAMING_DEBUG=false
SPEECHMATICS_REALTIME_URL=wss://eu.rt.speechmatics.com/v2/
SPEECHMATICS_REALTIME_MODEL=enhanced
SPEECHMATICS_REALTIME_LANGUAGE=pt
```

As keys Batch existentes são reutilizadas apenas no Python. Cada segredo distinto tem
no máximo duas reservas, assumindo uma conta por segredo conforme a decisão do projeto.
O limite real do fornecedor é por conta; `quota_exceeded`/close `4005` implica fallback,
**não** descarte. Só `timelimit_exceeded`/close `4006` ou HTTP `402` confirma falta de
créditos. Percentagens do orçamento e consultas de uso não são saldo de créditos.

O bot envia a presença ordenada a `POST /v1/sessions/{id}/streaming`. A API reserva slots
atomicamente no seu único event loop. `GET/POST /v1/guilds/{guild}/streaming` lê/grava o
modo por servidor; o ambiente define apenas o default. Use uma única réplica e um único
worker Uvicorn. Uma segunda chamada continua em gravação sem criar outro pool.

`/v1/streaming/audio` é um WebSocket interno na mesma rede privada da API existente.
A primeira mensagem JSON contém sessão, utilizador, filename WAV, timestamp absoluto
e token da participação. A API espera `RecognitionStarted` antes de confirmar `ready`.
Frames binários contêm sequência uint64 little endian e PCM S16LE mono a 48 kHz.
O Go faz downmix em int32, copia o buffer Opus e enfileira até 100 chunks por epoch;
fila cheia aborta esse transporte e mantém o WAV para Batch. Heartbeats, limites de
mensagem e timeouts impedem crescimento ilimitado e operações de rede no loop de captura.

Cada WAV é uma unidade e epoch independentes; a reserva sobrevive ao fecho por silêncio,
rotação e novo SSRC. As fronteiras de promoção e modo fecham o WAV anterior antes de
criar o seguinte. A API guarda finais idempotentes com `recording_id` e geração; parciais
só aparecem no terminal se debug estiver ligado. `EndOfStream` inclui a sequência final;
o commit exige `EndOfTranscript` e correspondência entre frames enviados e WAV fechado.

Uma falha deixa a unidade em `fallback_pending`. A admissão Batch invalida a geração
Realtime; o commit Batch substitui as mensagens dessa unidade e recalcula os chunks na
mesma transação. Resumos finais e perfis aguardam unidades e jobs pendentes. `/recap`
pode consultar finais provisórios; `/forget` pausa captura, invalida unidades, cancela
polling remoto e elimina WAVs/sidecars antes de confirmar. Delete remoto é tentado com
`force=True`; mesmo que falhe, resultados atrasados não podem recriar dados.

O outbox de segurança é escrito antes de abrir Realtime. Ao reiniciar o bot, WAVs com
header incompleto são reparados antes do replay. Ao reiniciar a API, unidades Realtime
ficam em fallback; o worker recupera ficheiros estáveis há pelo menos 15 segundos.
Unidades concluídas ou descartadas não são transcritas novamente por restart ou `/retry`.

Todas as keys confirmadas sem créditos provocam descarte terminal em Postgres **antes**
da limpeza. Workers interrompem polling e tentam cancelar jobs remotos; a limpeza de
ficheiros confinada à pasta de gravações é idempotente e repetida após falha/restart.
Um worker do bot recupera avisos de créditos pendentes, usando nonce por episódio e
confirmação persistida para evitar repetição. Discord só deduplica nonces por alguns
minutos; uma falha simultânea prolongada do bot e da confirmação após envio pode exigir
reconciliação manual desse aviso. `/streaming mode:on` ou uma nova chamada reavalia as
keys sem recuperar áudio descartado.

`GET /v1/speechmatics/realtime-usage` reporta todo o histórico de horas locais de PCM
enviadas por key. `/keys` filtra essas unidades pelo período UTC e apresenta modelo
e custo estimado, separados do reporte Batch. Reserva em silêncio não conta como áudio; um WAV
recuperado por Batch pode consumir ambos os produtos. Logs normais não contêm texto
transcrito nem credenciais. Os códigos seguem a
[referência Realtime da Speechmatics](https://docs.speechmatics.com/api-ref/realtime-transcription-websocket).

Verificação sem fornecedor pago nem Discord real:

```bash
# Definir TEST_DATABASE_URL apenas para um Postgres descartável.
TEST_DATABASE_URL=postgresql://... PYTHONPATH=src:src/transcription-api python -m pytest -q src/transcription-api/tests
(cd discord_bot && go test -race ./...)
```

Os testes usam um servidor WebSocket local e Postgres isolado por schema. O primeiro
ensaio real deve usar uma chamada controlada, uma key e debug ligado; validar PT-PT,
vagas e flush, depois várias keys. Desligar debug no fim. O ensaio real requer áudio e
acesso à Speechmatics e não é substituído pelos testes simulados.
