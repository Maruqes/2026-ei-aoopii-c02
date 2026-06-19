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
```

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

The API returns `200` immediately and transcribes the recording in a background thread.
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
