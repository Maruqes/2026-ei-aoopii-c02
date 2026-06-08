# Transcription API

Local FastAPI service wrapping `openai-whisper`.

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

With `WHISPER_DEVICE=cuda`, `make compose`, `make api`, `make test`, and
`make migrate` add `docker-compose.gpu.yml` automatically. When running
`docker compose` directly instead of `make`, include the GPU override file
explicitly.

Whisper quality settings:

```text
# `turbo` is fast; compare `large-v3` when quality matters and the runtime can handle it.
WHISPER_MODEL=large-v3
WHISPER_LANGUAGE=pt
# Beam search is slower than greedy decoding but usually gives a better first decode.
WHISPER_BEAM_SIZE=5
# Optional vocabulary hint for names, acronyms, and project-specific words.
WHISPER_INITIAL_PROMPT=Transcricao de uma conversa em portugues de Portugal sobre Discord, FastAPI e Whisper.
WHISPER_CARRY_INITIAL_PROMPT=true
```

Set `WHISPER_LANGUAGE=auto` only when recordings are genuinely multilingual or the
fixed language is wrong. If long recordings start repeating an incorrect phrase,
try `WHISPER_CONDITION_ON_PREVIOUS_TEXT=false`; this reduces the context carried
between Whisper windows and can make phrasing less consistent.

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
kept in memory; restarting the API restores the model configured in the environment.

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
