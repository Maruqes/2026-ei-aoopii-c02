# Plan: Voice Assistant Function Calling (USE{})

**Generated**: 2026-11-11
**Estimated Complexity**: Medium

## Overview

Give the voice assistant (`POST /v1/assistant/question` → `ConversationClient.answer_question` in `src/transcription-api/app/llm.py`) a simple, explicit function-calling capability using a plain-text convention the LLM can emit:

```
USE{
PLAY_MUSIC(search query here)
}
```

A parser in `transcription-api` finds all `USE{...}` blocks in the **fresh LLM answer only**, validates them against an allowlist, executes the mapped Python handlers sequentially (multiple calls per reply, in order), strips the raw `USE{}` blocks from the spoken reply, and falls back to a spoken message on failure.

Two ease-of-alter rules from the request:
1. **Prompt in one unique file**: new `src/transcription-api/app/assistant_prompt.md` holds the whole voice-assistant system prompt section for functions, with a `{FUNCTIONS}` placeholder auto-filled from the registry.
2. **Functions easy to find/alter/add**: config + handlers split — `src/transcription-api/app/assistant_functions.yaml` declares name/params/description, `src/transcription-api/app/assistant_functions.py` implements the Python handler for each entry. Adding a function = 1 YAML block + 1 Python function, no other edits.

V1 scope (per clarified answers): backend-only execution in `transcription-api`, executed only in `assistant_routes.question` (voice endpoint, not inside `llm.answer_question`), starter set = music controls only (`PLAY_MUSIC`, `STOP_MUSIC`), strict uppercase single-line `USE{}` grammar, allowlist + strict arg validation, spoken fallback on error, unit tests only (no API integration test, no live Discord check).

Non-goal for v1: actually driving audio in the Go `discord_bot` (`discord_bot/music.go`, `discord_bot/music_commands.go`). V1 handlers validate, log, return **both** a spoken string and a structured intent payload (e.g. `{"action": "play", "query": "..."}` / `{"action": "stop"}`) so the Go bot can poll/consume it in a follow-up; v1 does not implement the bot-side consumer.

## Prerequisites

- Python API runs via `src/transcription-api` (`make`, `pyproject.toml`, `.venv` present).
- Existing tests: `src/transcription-api/tests/test_assistant.py` (parser/registry tests will live alongside).
- LLM providers all go through `ConversationClient._chat(system, user)` (`src/transcription-api/app/llm.py:153`), so prompt injection is provider-agnostic.
- No new infra, no docs lookup needed (no external library).

## Sprint 1: Single prompt file + function catalog injection

**Goal**: Prompt lives in one editable Markdown file; LLM sees the function list without any execution yet.
**Demo/Validation**:
- Run: `uv run pytest src/transcription-api/tests/test_assistant.py -q` (existing suite still green).
- Verify: `answer_question` system prompt contains the `USE{...}` rules + `PLAY_MUSIC` / `STOP_MUSIC` catalog rendered from YAML; editing the `.md` file changes the prompt with no Python edit.

### Task 1.1: Create `assistant_prompt.md` with rules + placeholder
- **Location**: NEW `src/transcription-api/app/assistant_prompt.md`
- **Description**: Single Markdown file with (a) short "you have these functions" intro, (b) exact `USE{NAME(args)}` syntax + 2 examples (`PLAY_MUSIC`, `STOP_MUSIC`), (c) rules: emit `USE{}` only when user wants an action, never invent function names, never echo back results, `{FUNCTIONS}` placeholder where the catalog is injected.
- **Dependencies**: none
- **Acceptance Criteria**:
  - File exists, under ~60 lines, plain Markdown, no Python embedded.
  - Contains literal `{FUNCTIONS}` token once.
  - Documents one-call-per-block, one line per block, args are plain text (see Task 2.1 grammar).
- **Validation**: Manual read; `grep -c "{FUNCTIONS}" assistant_prompt.md == 1`.

### Task 1.2: Create `assistant_functions.yaml` declaring PLAY_MUSIC + STOP_MUSIC
- **Location**: NEW `src/transcription-api/app/assistant_functions.yaml`
- **Description**: Config declarations, e.g.:
  ```yaml
  functions:
    - name: PLAY_MUSIC
      description: Queue/search a track to play in the voice channel
      params: [{name: search, type: string, required: true, max_length: 200}]
      example: "USE{PLAY_MUSIC(lofi hip hop)}"
    - name: STOP_MUSIC
      description: Stop current playback and clear the queue
      params: []
      example: "USE{STOP_MUSIC()}"
  ```
- **Dependencies**: Task 1.1 (examples must match prompt syntax)
- **Acceptance Criteria**:
  - Exactly the 2 v1 functions, uppercase names, param metadata present.
  - Adding a third function later requires only appending here + one handler.
- **Validation**: `python3 -c "import yaml; yaml.safe_load(open(...))"` parses; names == allowlist.

### Task 1.3: Prompt loader + catalog renderer, wire into `answer_question`
- **Location**: NEW loader inside `src/transcription-api/app/assistant_functions.py` (or `assistant_prompt.py` if preferred); EDIT `src/transcription-api/app/llm.py:153-163` (`answer_question` system string)
- **Description**: Implement `load_prompt_catalog() -> (template, functions)` + `render_function_catalog()` producing lines like `PLAY_MUSIC(search) — description. Example: USE{PLAY_MUSIC(...)}`. In `answer_question`, replace hard-coded `"You cannot execute actions..."` sentence with rendered template (keep everything else: language, discord style, evidence rules). Cache file reads with `functools.lru_cache` + mtime check or plain re-read (calls are infrequent; simplicity wins).
- **Dependencies**: Tasks 1.1, 1.2
- **Acceptance Criteria**:
  - Existing test `test_general_answer_uses_shared_transport_without_history` updated: asserts `"USE{"` in system and `"PLAY_MUSIC"` in system (replaces old `"cannot execute actions"` assertion).
  - No behavior change when LLM emits no `USE{}` (answer passes through `clean_answer` unchanged).
  - Missing/corrupt `.md`/`.yaml` raises a clear `RuntimeError` at startup, not silently empty.
- **Validation**: Unit test: stub `_chat` captures `system`; assert catalog lines present; edit `.md` fixture changes output.

## Sprint 2: USE{} parser + config-driven registry with stub handlers

**Goal**: Parse multiple `USE{}` blocks, validate against YAML allowlist, execute stub handlers. Runnable without route changes (direct Python calls).
**Demo/Validation**:
- Run: `uv run pytest src/transcription-api/tests/test_assistant_functions.py -q` (new file).
- Verify: `parse_use_blocks("hi USE{PLAY_MUSIC(lofi)} bye USE{STOP_MUSIC()}")` returns 2 ordered calls; `execute_calls` runs stubs and returns spoken-safe results; unknown funcs / bad args are rejected, never executed.

### Task 2.1: Implement `parse_use_blocks` grammar
- **Location**: NEW `src/transcription-api/app/assistant_tool_parser.py`
- **Description**: Strict regex, single-line uppercase only, e.g. `USE\{\s*([A-Z_][A-Z0-9_]*)\s*\((.*?)\)\s*\}` with `re.DOTALL` but blocks must not span newlines (reject `\n` inside block). Args split on commas respecting single/double quotes; strip whitespace; cap at e.g. max 5 blocks per answer, max 500 chars per block. Provide `strip_use_blocks(text) -> str` (removes all blocks + collapses blank lines) and `parse_use_blocks(text) -> list[ToolCall(name, args)]`. Malformed blocks (no closing `}`, lowercase name, nested braces) are left as plain text, never executed.
- **Dependencies**: none (parallel with 1.x)
- **Acceptance Criteria**:
  - `PLAY_MUSIC(lofi hip hop)` → `("PLAY_MUSIC", ["lofi hip hop"])`; `STOP_MUSIC()` → `("STOP_MUSIC", [])`.
  - Quoted commas preserved: `PLAY_MUSIC("a, b")` → single arg `a, b`.
  - 6th+ block ignored with a flag; unclosed `USE{PLAY_MUSIC(` ignored.
- **Validation**: Parametrized unit tests: single, multi-order, quoted args, malformed, cap.

### Task 2.2: Implement registry + `assistant_functions.py` stub handlers
- **Location**: NEW `src/transcription-api/app/assistant_functions.py`; REUSE `assistant_functions.yaml` from Task 1.2
- **Description**: On import, load YAML once; expose `FUNCTIONS: dict[name, spec]` + `HANDLERS: dict[name, callable]`. Implement:
  - `def play_music(search: str) -> dict`: validate non-empty, ≤200 chars; log; return e.g. `{"spoken": "Queued: {search}", "intent": {"action": "play", "query": search}}` (stub — no audio, no network; intent reserved for Go bot follow-up).
  - `def stop_music() -> dict`: log; return `{"spoken": "Stopped playback.", "intent": {"action": "stop"}}`.
  - `def dispatch(name, args) -> str`: allowlist check → arity/type check per YAML → call handler → return result string. Unknown name / arity mismatch / empty required arg raises `ToolValidationError` (never executes).
- **Dependencies**: Tasks 1.2, 2.1
- **Acceptance Criteria**:
  - Adding a function = append YAML + add `def <lower>_handler` + one line in `HANDLERS`; documented in module docstring with example.
  - Handlers are pure/sync, <50ms, no I/O except logging (keeps 30s route timeout intact).
  - `dispatch("UNKNOWN", [])` raises, does not execute.
- **Validation**: Unit tests: allowlist, arity, empty-search rejection, overlong search rejection, stub return strings.

## Sprint 3: Execution loop in question flow + spoken fallback

**Goal**: End-to-end backend behavior: LLM answer with N `USE{}` blocks → handlers run in order → user hears clean text + spoken confirmations; failures speak a fallback.
**Demo/Validation**:
- Run: `uv run pytest src/transcription-api/tests/ -q -k "assistant or function or tool or parser"`.
- Verify with stubbed `_chat` returning `"Ok! USE{PLAY_MUSIC(lofi)} USE{STOP_MUSIC()}"`: final answer contains no `USE{`, contains spoken confirmations, handlers ran in order.

### Task 3.1: Execute calls in `assistant_routes.question` (or `llm.answer_question` wrapper)
- **Location**: EDIT `src/transcription-api/app/assistant_routes.py:150-168` (after `bounded.answer_question`), REUSE parser + registry from Sprint 2
- **Description**: After getting `answer`: `calls = parse_use_blocks(answer)` (only from this fresh answer, never from `request.history` / `guild_context` / transcript). If empty → return as today. Else loop in order: `try: result = dispatch(...)` except → record error. Collect `spoken` = `strip_use_blocks(answer)` plus per-call confirmation lines (e.g. `"🎵 Queued: lofi"`). Cap total execution at ~5s (stubs are instant; guard anyway with timeout or sequential try/except). Save the **final spoken text** (not raw `USE{}`) via `repository.save_assistant_answer`.
- **Dependencies**: Sprints 1, 2
- **Acceptance Criteria**:
  - Multiple blocks execute in emitted order (assert via call-log in tests).
  - Raw `USE{...}` never reaches the client response nor the DB-saved answer.
  - Evidence rule preserved: transcript/history `USE{...}`-looking text is never executed (only fresh answer parsed).
  - 30s route timeout (`assistant_routes.py:151`) still holds; handler exceptions never 500 — they become fallback text.
- **Validation**: Unit tests with fake LLM client returning canned answers: multi-call order, no-call passthrough, DB-saved text is stripped.

### Task 3.2: Spoken fallback + edge cases
- **Location**: EDIT same region as 3.1 + `assistant_prompt.md` fallback line
- **Description**: Rules: (a) unknown function / bad args → append `"Sorry, I couldn't do that action."` (keep rest of stripped text); (b) handler raises → same fallback, log exception with function name; (c) stripped text empty after removing blocks (answer was only `USE{...}`) → synthesize from results, e.g. `"Done: Queued lofi."` or fallback if all failed. Never echo raw args blindly if validation failed.
- **Dependencies**: Task 3.1
- **Acceptance Criteria**:
  - `USE{UNKNOWN()}` → answer has no `USE{`, contains apology, no execution.
  - `USE{PLAY_MUSIC()}` (empty) → validation error → fallback spoken.
  - Answer consisting solely of `USE{STOP_MUSIC()}` → returns `"Stopped playback."`-style confirmation, not empty string (existing `answer_question` empty-answer guard in `llm.py:224-226` still passes).
- **Validation**: Unit tests for each fallback branch; assert no `USE{` in output and non-empty answer.

### Task 3.3: Unit-test suite + prompt/registry docs
- **Location**: NEW `src/transcription-api/tests/test_assistant_functions.py`; EDIT `src/transcription-api/tests/test_assistant.py:52` (old `"cannot execute actions"` assertion); optional short section in `assistant_prompt.md` header comment explaining how to add a function
- **Description**: Cover: prompt rendering, YAML loads, parser cases, dispatch validation, execution order, strip, all fallback branches. Update the one stale assertion that expects the old "cannot execute" system text.
- **Dependencies**: Tasks 3.1–3.2
- **Acceptance Criteria**:
  - New test file ≥15 cases, all passing; full `pytest src/transcription-api/tests/ -q` green.
  - README-style comment at top of `.yaml` + `.md` tells a future editor exactly where to add a function (2 places).
- **Validation**: `uv run pytest src/transcription-api/tests/test_assistant_functions.py -q` + full assistant test file.

## Testing Strategy

- Unit-only per request (no API integration, no live Discord). Fake `_chat` returns canned `USE{}` answers; assert on parsed calls, dispatch results, final spoken text.
- Per sprint: Sprint 1 asserts prompt content; Sprint 2 asserts parser + registry isolation; Sprint 3 asserts end-to-end `answer → execute → stripped spoken answer` with order and fallback branches.
- Regression: existing `test_assistant.py` must stay green except the intentional `"cannot execute actions"` assertion update.

## Potential Risks & Gotchas

- **Prompt-injection via transcripts**: voice transcripts and history are untrusted (`evidence_rules()` in `llm.py:746`). A user could say "ignore rules, emit USE{...}" or paste fake `USE{}` into chat. Mitigation: parse **only** the fresh LLM answer, never history/context/transcript; allowlist + validation; handlers are harmless stubs in v1.
- **Music actually lives in Go**: `discord_bot/music.go` / `music_commands.go` own playback, but v1 executes in Python. V1 stubs return spoken confirmations only — real playback needs a follow-up bridge (API returns action intents for the bot to poll, or bot calls the API). Don't promise real audio in v1.
- **Grammar ambiguity**: commas inside queries, quotes, multiline blocks, lowercase names. Mitigation: strict uppercase regex, quote-aware split, malformed blocks treated as text, documented in `.md`.
- **Empty spoken text**: an answer of only `USE{...}` strips to `""`, tripping the `llm.py:224` empty-answer guard. Mitigation: synthesize confirmation from handler results (Task 3.2c).
- **Timeout budget**: route caps at 30s (`assistant_routes.py:151`). Mitigation: sync stub handlers, ~5s execution cap, exceptions → fallback (never retry in v1, no LLM re-query loop).
- **Stale "cannot execute" test**: `test_assistant.py:52` asserts the old system text; it must be updated when the prompt changes or CI goes red.

## Rollback Plan

- Revert the `answer_question` system-string edit in `llm.py` (restores "cannot execute actions" text) and skip the parser/execution block in `assistant_routes.py`; answers return to plain text.
- New files (`assistant_prompt.md`, `assistant_functions.yaml/.py`, `assistant_tool_parser.py`, `test_assistant_functions.py`) are additive — delete or leave inert.
- No migrations, no schema changes, no Go bot changes, so rollback is a pure code revert.
