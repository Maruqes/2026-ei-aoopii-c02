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
