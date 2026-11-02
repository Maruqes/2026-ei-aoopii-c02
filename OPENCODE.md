# OpenCode Go: provider setup and models

OpenCode Go runs every AI feature with an OpenCode Go plan: group memory, session
recaps, profile updates, `/digest`, `/oracle` and `/prompt`. Transcription keeps
using `TRANSCRIPTION_PROVIDER`. The bot does not change; `/models`, `/status` and
the health endpoint read the generic `provider`/`model` fields from the API.

## Configuration

Requirements:

- An active OpenCode Go plan and an API key from
  [the OpenCode Console](https://opencode.ai/console).
- Outbound HTTPS access from the API container to `opencode.ai`.
- For DeepSeek models (the default), **Regions → Global** selected in the
  Console. Without it, the default model fails.

Keep the Discord, Postgres and transcription variables and add to `.env`:

```dotenv
LLM_PROVIDER=opencode-go
OPENCODE_API_KEY=your_opencode_go_api_key
OPENCODE_BASE_URL=https://opencode.ai/inference/go/openai/v1
OPENCODE_MODELS_BASE_URL=https://opencode.ai/inference/go/v1
OPENCODE_MODEL=deepseek-v4.1-flash
# Optional stable routing/cache id; empty generates one per API process.
OPENCODE_SESSION_ID=
```

`OPENCODE_MODEL` is the initial model. A choice saved by `/models` has priority
and survives restarts. After editing `.env`, recreate the services:

```bash
docker compose up -d --build
```

Do not reuse `OPENAI_API_KEY` or `GROQ_API_KEY`: each provider has its own
variables, so switching back and forth needs no key juggling. `.env` is ignored
by git; never commit a real key.

## Two different base URLs

The chat API is OpenAI-compatible, but the model catalog is not under the chat
base URL:

| Use | Variable | Default |
| --- | --- | --- |
| Chat completions | `OPENCODE_BASE_URL` | `https://opencode.ai/inference/go/openai/v1` |
| Model catalog | `OPENCODE_MODELS_BASE_URL` | `https://opencode.ai/inference/go/v1` |

Calling `/models` on the chat base URL returns 404, which is why this provider
has its own branch instead of reusing `LLM_PROVIDER=openai` with a custom URL.
If `/models` in Discord fails with 404, check `OPENCODE_MODELS_BASE_URL`.

## Required request headers

Every request carries:

- `Authorization: Bearer <OPENCODE_API_KEY>`.
- `x-opencode-session: <stable id>`. Without it the chat endpoint returns
  `400 {"error":{"type":"MissingSessionID"}}`.
- `User-Agent: discord-anthropologist/1.0`, instead of a generic SDK name.

The session id is generated once per API process, so the gateway can reuse
prompt caches across group-memory, recap and profile calls. Set
`OPENCODE_SESSION_ID` to pin a specific id; leaving it empty is enough for normal
use. Persisting it across restarts is optional.

These headers live in `OpenCodeGoClient` only, and the tests assert them.

## Choosing a model in Discord

After the API starts, `/models` lists the live OpenCode Go catalog. Selecting a
model sends the small `Ola!` test prompt and only stores it if the request
succeeds. The selection is written to `LLM_MODEL_SELECTION_FILE`
(`/app/.tmp/state/llm_model_selection.json` in Docker) per provider, so it does
not leak between `opencode-go`, `chatgpt`, `openai`, `groq` and `ollama`.

`/models model:<id>` still rejects IDs that are not in the live catalog.

The default `deepseek-v4.1-flash` has a 1M context. Good fallbacks without extra
settings are `mimo-v2.6-flash` or `glm-5.3-flash`. Avoid `muse-spark-*` and
`iceman-contributor-free`, which train on request data.

`/effort` remains ChatGPT-only; OpenCode Go always uses the model default.

## Verify

```bash
curl -s localhost:8000/health
curl -s localhost:8000/v1/models
```

The health payload should show `llm_provider: opencode-go` and the current
model; `/v1/models` should return the OpenCode Go ids (around 28 chat models).

Then, in Discord:

1. Run `/models` and select a model; the test prompt must succeed.
2. Run one real group-memory bulk, one `/digest`, one `/prompt` and one profile
   update, and confirm 200s in the API log with no `MissingSessionID`.

## Switching providers

To go back, set `LLM_PROVIDER` to `openai`, `groq`, `ollama` or `chatgpt` and
recreate the services. `/models` and the saved selection follow the active
provider; the ChatGPT login flow is documented in
[ChatGPT setup](CHATGPT_SETUP.md) and is not needed here.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `OPENCODE_API_KEY is required when LLM_PROVIDER=opencode-go` | Fill `OPENCODE_API_KEY` in `.env` and recreate the API. |
| `400 MissingSessionID` | Upgrade the API; the header is sent by `OpenCodeGoClient` on every chat and catalog request. |
| `401` / `403` | Key invalid, revoked, or the Go plan is inactive. Issue a new key in the Console. |
| `/models` fails with 404 | `OPENCODE_MODELS_BASE_URL` points at the chat URL. Restore `/inference/go/v1`. |
| Default model fails while others work | Select **Regions → Global** in the Console, or choose `mimo-v2.6-flash` / `glm-5.3-flash`. |
| `LLM output was truncated` | Raise `LLM_MAX_OUTPUT_TOKENS`, or ask for a shorter answer. |
| `LLM_CONTEXT_CHARS is too small` | Raise `LLM_CONTEXT_CHARS` or use a larger-context model. |
| Limit reached or quota error | Check monthly usage in the Console; the Go plan has a monthly limit. |
| `Unsupported LLM_PROVIDER` | Use `opencode-go` exactly, then rebuild the API. |

## Known constraints

- OpenCode Go documents itself for OpenCode and other coding agents. This bot's
  conversational traffic fits the request shape, but the usage policy does not
  explicitly cover it; use it only where you accept that risk.
- Reasoning effort and prompt-cache persistence are out of scope for this
  provider. If the session header contract changes, only `OpenCodeGoClient`
  needs updating.

## Official references

- [OpenCode Go documentation](https://opencode.ai/v2/docs/console/go/)
- [OpenCode Console](https://opencode.ai/console)
