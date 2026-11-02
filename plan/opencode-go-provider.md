# Plan: OpenCode Go as an LLM provider

Date: 2026-11-02. Status: implemented 2026-11-02.

## Goal

Let the API use OpenCode Go (`LLM_PROVIDER=opencode-go`) for every AI feature: group memory,
session recaps, profile updates, `/digest`, `/oracle` and `/prompt`. Transcription providers stay
untouched. Switching between `openai`, `groq`, `ollama`, `chatgpt` and `opencode-go` must keep
working through `/models` and environment variables, without changing the Go bot.

## Verified facts (probed on 2026-11-02)

Source: <https://opencode.ai/v2/docs/console/go/> and live requests against the inference API.

| Item | Value |
| --- | --- |
| Provider id | `opencode-go` |
| Chat endpoint | `https://opencode.ai/inference/go/openai/v1` |
| Models endpoint | `https://opencode.ai/inference/go/v1/models` |
| Auth | `Authorization: Bearer <key>` (`OPENCODE_API_KEY`) |
| Protocol | OpenAI Chat Completions; `max_completion_tokens` and `response_format: {"type":"json_object"}` both accepted |
| Required header | `x-opencode-session: <stable id>` — without it chat returns `400 MissingSessionID` |
| Required header | Own `User-Agent`, e.g. `discord-anthropologist/1.0` (generic SDK names are discouraged) |
| Catalog | 28 chat models; every entry has `"type":"chat"`, so the bot's existing filter accepts all |
| Default candidate | `deepseek-v4.1-flash` — 1M context, `$0.15/$0.60` per 1M input/output, `$100` monthly Go limit |

Live checks that shape the design:

```bash
# works
curl -s -H "Authorization: Bearer $KEY" https://opencode.ai/inference/go/v1/models
# 200, {"object":"list","data":[{"id":"deepseek-v4.1-flash",...}]}

# works (JSON mode + max_completion_tokens)
curl -s -H "Authorization: Bearer $KEY" -H "x-opencode-session: ses_x" \
  -H "User-Agent: discord-anthropologist/1.0" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"..."}],
       "max_completion_tokens":64,"response_format":{"type":"json_object"}}' \
  https://opencode.ai/inference/go/openai/v1/chat/completions

# fails: no session header -> 400 {"error":{"type":"MissingSessionID",...}}
# fails: GET https://opencode.ai/inference/go/openai/v1/models -> 404
```

The 404 on the OpenAI base URL is the reason this cannot be a pure configuration change:
`OpenAICompatibleClient.list_models()` calls `client.models.list()`, which would append `/models`
to the chat base URL, break `/models` in Discord, and with it every model selection.

## Design decisions

1. **Dedicated provider, not `LLM_PROVIDER=openai` with a custom base URL.** The session header and
   the separate models URL cannot be expressed through the current `openai` branch. A dedicated
   `opencode-go` branch also keeps the existing `openai`/`groq`/`ollama`/`chatgpt` behavior
   unchanged, which the tests already pin.
2. **Reuse `OpenAICompatibleClient`.** Only two things differ: default headers and the models base
   URL. Add an optional `default_headers` argument to the base class instead of duplicating the lazy
   client construction.
3. **Stable session id, one per deployment process.** The client is already cached per
   `(settings, model, effort)`, so a module-level id is stable for the process lifetime and lets the
   gateway reuse prompt caches across group-memory, recap and profile calls. An env override keeps
   it adjustable; persisting it across restarts is a later option, not required for correctness.
4. **No new SDK.** `openai` is already a dependency; only `urllib`/`openai` are used today, so
   nothing is added to `requirements.txt`.
5. **No Go changes.** `/models`, `/effort` and `/status` read generic `provider`/`model` strings from
   the API. `/effort` stays ChatGPT-only (see Open questions).

## Implementation

### 1. `app/config.py`

Add fields next to the existing LLM block:

```python
opencode_api_key: str | None = None
opencode_base_url: str = "https://opencode.ai/inference/go/openai/v1"
opencode_models_base_url: str = "https://opencode.ai/inference/go/v1"
opencode_model: str = "deepseek-v4.1-flash"
opencode_session_id: str = ""
```

and in `from_env()`, following the existing `env_str` pattern with `.rstrip("/")` for both URLs.

### 2. `app/llm.py`

Extend the base client so headers can be injected:

```python
def __init__(self, ..., default_headers: dict[str, str] | None = None):
    self.default_headers = dict(default_headers or {})
    ...
    self._client = OpenAI(
        api_key=self.api_key,
        base_url=self.base_url,
        timeout=self.timeout_seconds,
        max_retries=getattr(self, "max_retries", 1),
        default_headers=self.default_headers or None,
    )
```

Add the provider class:

```python
OPENCODE_SESSION = f"discord-anthropologist-{uuid.uuid4()}"
OPENCODE_USER_AGENT = "discord-anthropologist/1.0"

class OpenCodeGoClient(OpenAICompatibleClient):
    """OpenCode Go: OpenAI-compatible chat, separate catalog URL, session header."""

    def __init__(self, *, api_key, base_url, models_base_url, model, session_id="", **kwargs):
        headers = {
            "x-opencode-session": session_id or OPENCODE_SESSION,
            "User-Agent": OPENCODE_USER_AGENT,
        }
        super().__init__(
            api_key=api_key, base_url=base_url, model=model,
            api_key_env="OPENCODE_API_KEY", provider_name="opencode-go",
            default_headers=headers, **kwargs,
        )
        self.models_base_url = models_base_url.rstrip("/")
        self._models_client_instance = None
        self._models_client_lock = threading.Lock()

    def _models_client(self):
        # Lazy, double-checked; mirrors _load_client so the catalog has its own base URL.
        ...

    def list_models(self) -> list[str]:
        if not self.api_key:
            raise RuntimeError("OPENCODE_API_KEY is required when LLM_PROVIDER=opencode-go")
        models = self._models_client().models.list()
        return sorted({str(m.id).strip() for m in models.data if getattr(m, "id", None) and str(m.id).strip()})
```

Keep `_chat()` inherited: it already sends `max_completion_tokens`, optional
`response_format={"type":"json_object"}`, and maps `finish_reason == "length"` to the existing
`LLM_MAX_OUTPUT_TOKENS` error. No prompt changes.

### 3. `app/model_selection.py`

`configured_model()` gains `if settings.llm_provider == "opencode-go": return settings.opencode_model`.
Selection persistence already keys by provider name, so `/models` choices survive restarts and do not
leak between providers.

### 4. `app/main.py`

* `build_llm_client()`: new branch building `OpenCodeGoClient` with timeout, context chars and
  output-token limits from settings; extend the unsupported-provider message to list `opencode-go`.
* Nothing else changes: `/v1/models`, `/v1/models/current`, `/v1/effort` guards, `/status` and the
  health payload are provider-agnostic. `/models model:<id>` keeps rejecting IDs outside the live
  catalog, which is correct here because the OpenCode Go catalog endpoint responds.

### 5. Configuration surface

`.env.example`, `docker-compose.yml` (`api` service environment) and `.env`:

```dotenv
# LLM provider: openai, groq, ollama, opencode-go, or chatgpt.
LLM_PROVIDER=opencode-go

# Optional: OpenCode Go (https://opencode.ai/console). Requires an active Go plan.
OPENCODE_API_KEY=
OPENCODE_BASE_URL=https://opencode.ai/inference/go/openai/v1
OPENCODE_MODELS_BASE_URL=https://opencode.ai/inference/go/v1
OPENCODE_MODEL=deepseek-v4.1-flash
# Optional stable routing/cache id; empty generates one per API process.
OPENCODE_SESSION_ID=
```

Compose additions must follow the existing `${VAR:-default}` style and must not reuse
`OPENAI_API_KEY`, so switching providers back and forth needs no key juggling.

### 6. Tests

New `src/transcription-api/tests/test_opencode_go.py`, matching the style of `test_chatgpt.py`
(`SimpleNamespace` fakes, `monkeypatch`, `replace(settings, ...)`):

1. `Settings.from_env()` reads the `OPENCODE_*` variables, including URL trailing-slash trimming.
2. `configured_model()` returns `OPENCODE_MODEL` and `current_model()` honors a persisted
   `{"opencode-go": "glm-5.3"}` selection.
3. `build_llm_client()` returns an `OpenCodeGoClient` with the expected model, `provider_name` and
   `api_key_env`.
4. Monkeypatched `openai.OpenAI` captures kwargs: `base_url` is the chat URL, `default_headers`
   contains `x-opencode-session` and the custom `User-Agent`, and the explicit
   `OPENCODE_SESSION_ID` wins over the generated default.
5. `list_models()` hits the models base URL (assert the second client's `base_url`) and returns a
   sorted de-duplicated list.
6. Missing key raises `RuntimeError` naming `OPENCODE_API_KEY`.
7. Extend the provider matrix in `test_chatgpt.py::test_existing_providers_are_still_available` with
   `("opencode-go", "deepseek-v4.1-flash")`, and the parametrized
   `test_other_providers_still_require_catalog_membership` with `"opencode-go"`.

Run: `python -m pytest src/transcription-api/tests -q` and `ruff check src`.

### 7. Documentation

* `src/transcription-api/README.md` (LLM section): add an OpenCode Go block next to Groq, noting the
  required Go subscription, the separate catalog URL and the fixed headers.
* `docs/features/ai-configuration.md`: add the provider to the supported list.
* Root `README.md` line ~17: mention `LLM_PROVIDER=opencode-go` as an alternative to ChatGPT setup.
* `CHATGPT_SETUP.md`: unchanged (its instructions stay specific to `chatgpt`).

## Manual verification

1. `curl -s localhost:8000/v1/health` shows `llm_provider: opencode-go` and the chosen model.
2. `curl -s localhost:8000/v1/models` returns the ~28 OpenCode Go ids.
3. In Discord: `/models` lists them; selecting one sends the `Ola!` test prompt and persists it.
4. One real group-memory bulk, one `/digest`, one `/prompt` and one profile update, checking the API
   log for 200s and no `MissingSessionID`.
5. Switch back to `chatgpt` (or `openai`) and confirm `/models` and answers still work.

## Acceptance criteria

* `LLM_PROVIDER=opencode-go` with only `OPENCODE_API_KEY` set starts the API and answers every
  existing AI path.
* `/models`, `/models model:<id>` and `/status` behave as with the other providers.
* No regression in the `openai`, `groq`, `ollama` and `chatgpt` tests.
* No changes required in `discord_bot`.

## Risks and open questions

1. **Intended use.** OpenCode Go's documentation says it is for OpenCode "and other coding agents
   that produce similar types of requests", with abuse monitoring. This project is a Discord bot
   with moderate, conversational traffic; model quality and request shape fit, but the usage policy
   does not explicitly cover it. Decide whether to accept that risk, or use OpenCode Go only for
   local/experimental runs.
2. **Workspace privacy settings.** DeepSeek V4.1 Flash / V4 Pro require **Regions → Global** in the
   Console; without it the default model fails. Fallback model with no extra setting:
   `mimo-v2.6-flash` or `glm-5.3-flash`. Do not pick `muse-spark-*` or `iceman-contributor-free`,
   which train on request data.
3. **Undocumented-but-required header.** `x-opencode-session` is part of the current contract. If it
   changes, only `OpenCodeGoClient` needs updating; the tests assert the header so a silent
   regression becomes a test failure.
4. **Reasoning effort.** OpenCode Go exposes `reasoning_effort` for some models, but `/effort` is
   currently restricted to `chatgpt`. Extending it is a separate change; out of scope here.
5. **Prompt caching** depends on a stable session id. The process-level default is enough; persisting
   the id to `.tmp/state` is an optional refinement.

Suggested order: config + client + wiring (1 h), tests (40 min), docs and compose (20 min), manual
check in Discord (15 min).
