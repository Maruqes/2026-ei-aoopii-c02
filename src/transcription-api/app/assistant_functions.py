"""Config-driven function registry + validators for the voice assistant.

To add a function:
  1) Append a `- name: ...` block to `assistant_functions.yaml`.
  2) Add `def <name_lower>(...) -> dict` below returning
     `{"spoken": ..., "intent": ...}`. For actions the Discord bot executes
     (music), return `"spoken": ""` and the intent payload — the bot speaks
     the real outcome (same strings as /play and /musicstop).
  3) Add one line in `HANDLERS` mapping the uppercase name to the function.

No other edits needed. Handlers are pure/sync (<50ms, logging only, no I/O)
so the 30s voice route budget is preserved. Real side effects happen in the
Go bot (`discord_bot/assistant_music.go`), never here.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .assistant_tool_parser import parse_use_blocks, strip_use_blocks

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = PACKAGE_DIR / "assistant_prompt.md"
FUNCTIONS_PATH = PACKAGE_DIR / "assistant_functions.yaml"

FALLBACK_MESSAGE = "Sorry, I couldn't do that action."
MAX_EXECUTION_SECONDS = 5.0

FUNCTIONS: dict[str, dict] = {}
HANDLERS: dict[str, object] = {}


class ToolValidationError(ValueError):
    """Raised when a USE{} call fails allowlist or argument validation."""


def play_music(search: str) -> dict:
    """Validate a music search, return the intent for the Discord bot to execute.

    Returns empty spoken: the bot appends the real playback confirmation
    (track title/position from EnqueueText, same strings as /play). Keeping
    Python silent avoids double-speaking "Queued: X" + the real title.
    """
    query = (search or "").strip()
    if not query:
        raise ToolValidationError("PLAY_MUSIC requires a non-empty search query")
    if len(query) > 200:
        raise ToolValidationError("PLAY_MUSIC search is too long (max 200 chars)")
    logger.info("PLAY_MUSIC query=%r", query)
    return {"spoken": "", "intent": {"action": "play", "query": query}}


def stop_music() -> dict:
    """Validate a stop request, return the intent for the Discord bot.

    Returns empty spoken: the bot appends the real confirmation after Clear
    (same strings as /musicstop). Keeps voice recording running.
    """
    logger.info("STOP_MUSIC")
    return {"spoken": "", "intent": {"action": "stop"}}


HANDLERS = {
    "PLAY_MUSIC": play_music,
    "STOP_MUSIC": stop_music,
}


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required for assistant functions (pip install pyyaml)"
        ) from exc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Assistant functions file missing: {path}") from exc
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise RuntimeError(f"Assistant functions file corrupt: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Assistant functions file corrupt: {path}: expected mapping")
    return data


def _load_functions_spec() -> dict[str, dict]:
    data = _load_yaml(FUNCTIONS_PATH)
    raw = data.get("functions")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"Assistant functions file corrupt: {FUNCTIONS_PATH}: empty 'functions'")
    specs: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise RuntimeError(f"Assistant functions file corrupt: {FUNCTIONS_PATH}: bad entry {entry!r}")
        name = str(entry["name"]).strip()
        if name != name.upper() or not name.replace("_", "").replace("0", "a").replace("1", "a").isalpha() and not name.replace("_", "").isalnum():
            # Strict uppercase check is done via regex-equivalent below.
            pass
        import re

        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            raise RuntimeError(f"Assistant functions file corrupt: {FUNCTIONS_PATH}: bad name {name!r}")
        if name in specs:
            raise RuntimeError(f"Assistant functions file corrupt: {FUNCTIONS_PATH}: duplicate {name}")
        specs[name] = entry
    return specs


# Load once at import so a missing/corrupt catalog fails fast at startup.
FUNCTIONS = _load_functions_spec()
_missing_handlers = [name for name in FUNCTIONS if name not in HANDLERS]
if _missing_handlers:
    raise RuntimeError(f"Assistant functions missing handlers for: {_missing_handlers}")


def load_prompt_template() -> str:
    try:
        template = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Assistant prompt file missing: {PROMPT_PATH}") from exc
    if template.count("{FUNCTIONS}") != 1:
        raise RuntimeError(
            f"Assistant prompt file corrupt: {PROMPT_PATH}: expected exactly one {{FUNCTIONS}} placeholder"
        )
    return template


def render_function_catalog(functions: dict[str, dict] | None = None) -> str:
    specs = functions if functions is not None else FUNCTIONS
    lines: list[str] = []
    for name, spec in specs.items():
        params = spec.get("params") or []
        param_names = ", ".join(str(p.get("name", "")) for p in params if isinstance(p, dict))
        desc = str(spec.get("description", "")).strip()
        example = str(spec.get("example", f"USE{{{name}()}}")).strip()
        signature = f"{name}({param_names})"
        line = f"- {signature}"
        if desc:
            line += f" — {desc}"
        details: list[str] = []
        for p in params:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name", "arg"))
            req = "required non-empty" if p.get("required") else "optional"
            max_length = p.get("max_length")
            if isinstance(max_length, int):
                details.append(f"{pname}: {req}, max {max_length} chars")
            else:
                details.append(f"{pname}: {req}")
        if details:
            line += f". Args: {'; '.join(details)}"
        else:
            line += ". Args: none"
        line += f". Example: {example}"
        lines.append(line)
    return "\n".join(lines)


def get_assistant_function_prompt() -> str:
    """Render the prompt section injected into `answer_question` system text."""
    template = load_prompt_template()
    catalog = render_function_catalog()
    if not catalog.strip():
        raise RuntimeError(f"Assistant functions file corrupt: {FUNCTIONS_PATH}: empty catalog")
    return template.replace("{FUNCTIONS}", catalog)


def dispatch(name: str, args: list[str]) -> dict:
    """Validate against the YAML allowlist and run the mapped handler."""
    spec = FUNCTIONS.get(name)
    if spec is None:
        raise ToolValidationError(f"Unknown function: {name}")
    handler = HANDLERS.get(name)
    if handler is None or not callable(handler):
        raise ToolValidationError(f"No handler for function: {name}")
    params = spec.get("params") or []
    if not isinstance(params, list):
        raise ToolValidationError(f"Bad param spec for {name}")
    required = [p for p in params if isinstance(p, dict) and p.get("required")]
    if len(args) < len(required):
        raise ToolValidationError(f"{name} expects {len(required)} arg(s), got {len(args)}")
    if len(args) > len(params):
        raise ToolValidationError(f"{name} expects at most {len(params)} arg(s), got {len(args)}")
    for value, param in zip(args, params):
        if not isinstance(param, dict):
            continue
        label = str(param.get("name", "arg"))
        text = str(value or "").strip()
        if param.get("required") and not text:
            raise ToolValidationError(f"{name} requires non-empty {label}")
        max_length = param.get("max_length")
        if isinstance(max_length, int) and len(text) > max_length:
            raise ToolValidationError(f"{name} {label} too long (max {max_length})")
    try:
        result = handler(*args)  # type: ignore[operator]
    except ToolValidationError:
        raise
    except Exception as exc:
        logger.exception("Assistant function handler failed: %s", name)
        raise ToolValidationError(f"{name} failed: {exc}") from exc
    if not isinstance(result, dict) or "spoken" not in result or "intent" not in result:
        raise ToolValidationError(f"{name} handler returned invalid result")
    return result


def execute_assistant_answer(raw_answer: str) -> tuple[str, list[dict]]:
    """Run USE{} calls from a fresh LLM answer; return (spoken_text, intents).

    Never raises: per-call failures become FALLBACK_MESSAGE, and unexpected
    errors fall back to stripped text + fallback. Raw USE{} never appears
    in the returned spoken text.
    """
    try:
        calls = parse_use_blocks(raw_answer or "")
        if not calls:
            return raw_answer, []
        base = strip_use_blocks(raw_answer or "")
        parts: list[str] = [base] if base else []
        intents: list[dict] = []
        start = time.monotonic()
        for call in calls:
            if time.monotonic() - start > MAX_EXECUTION_SECONDS:
                logger.warning("Assistant function budget exceeded; skipping %s", call.name)
                parts.append(FALLBACK_MESSAGE)
                continue
            try:
                result = dispatch(call.name, list(call.args))
            except ToolValidationError as exc:
                logger.warning("Assistant function rejected %s(%s): %s", call.name, call.args, exc)
                parts.append(FALLBACK_MESSAGE)
                continue
            except Exception:  # pragma: no cover - dispatch already wraps
                logger.exception("Assistant function failed: %s", call.name)
                parts.append(FALLBACK_MESSAGE)
                continue
            spoken = str(result.get("spoken", "")).strip()
            intent = result.get("intent")
            if isinstance(intent, dict):
                intents.append(intent)
            if spoken:
                parts.append(spoken)
        final = "\n".join(p for p in parts if p and p.strip()).strip()
        if not final:
            if intents:
                final = "Done."
            else:
                final = FALLBACK_MESSAGE
        # Collapse accidental blank runs (mirrors clean_answer without import).
        lines = [line.rstrip() for line in final.replace("\r\n", "\n").split("\n")]
        cleaned: list[str] = []
        prev_blank = False
        for line in lines:
            blank = not line.strip()
            if blank and prev_blank:
                continue
            cleaned.append(line)
            prev_blank = blank
        return "\n".join(cleaned).strip(), intents
    except Exception:
        logger.exception("Assistant function execution failed")
        fallback_base = strip_use_blocks(raw_answer or "").strip()
        if fallback_base:
            return f"{fallback_base}\n{FALLBACK_MESSAGE}".strip(), []
        return FALLBACK_MESSAGE, []
