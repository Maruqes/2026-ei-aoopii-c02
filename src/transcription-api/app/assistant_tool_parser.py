"""Strict single-line USE{} parser for the voice assistant.

Grammar (v1):
- `USE{NAME(args)}` where NAME matches `[A-Z_][A-Z0-9_]*`.
- One call per block, one block per line (blocks spanning newlines are ignored).
- Args are comma-separated plain text; a comma inside matching single or
  double quotes does not split. Surrounding quotes are stripped.
- Malformed blocks (no closing `}`, lowercase name, nested braces, newline
  inside, overlong) are left as plain text and never executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_USE_BLOCKS = 5
MAX_BLOCK_CHARS = 500

_USE_RE = re.compile(r"USE\{\s*([A-Z_][A-Z0-9_]*)\s*\((.*?)\)\s*\}", re.DOTALL)


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: list[str]


def split_args(arg_str: str) -> list[str]:
    """Split on commas, respecting single/double quotes. Strips whitespace/quotes."""
    text = arg_str.strip()
    if not text:
        return []
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif ch == "," and not in_single and not in_double:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    cleaned: list[str] = []
    for part in parts:
        piece = part.strip()
        if len(piece) >= 2 and (
            (piece.startswith('"') and piece.endswith('"'))
            or (piece.startswith("'") and piece.endswith("'"))
        ):
            piece = piece[1:-1].strip()
        cleaned.append(piece)
    # A single empty arg (e.g. `STOP_MUSIC( )`) means no args.
    if len(cleaned) == 1 and cleaned[0] == "":
        return []
    return cleaned


def _valid_match(match: re.Match[str]) -> bool:
    full = match.group(0)
    if len(full) > MAX_BLOCK_CHARS:
        return False
    if "\n" in full or "\r" in full:
        return False
    args_str = match.group(2)
    if "{" in args_str or "}" in args_str:
        return False
    return True


def parse_use_blocks(text: str) -> list[ToolCall]:
    """Return up to MAX_USE_BLOCKS ordered calls from fresh LLM text only.

    Extra blocks beyond the cap are ignored (never executed). Callers that
    need to hide them from speech should use `strip_use_blocks`, which
    removes all valid blocks.
    """
    calls: list[ToolCall] = []
    for match in _USE_RE.finditer(text or ""):
        if not _valid_match(match):
            continue
        if len(calls) >= MAX_USE_BLOCKS:
            break
        name = match.group(1)
        args = split_args(match.group(2))
        calls.append(ToolCall(name=name, args=args))
    return calls


def strip_use_blocks(text: str) -> str:
    """Remove all valid USE{} blocks and collapse blank lines."""
    if not text:
        return ""

    def _replace(match: re.Match[str]) -> str:
        if not _valid_match(match):
            return match.group(0)
        return ""

    stripped = _USE_RE.sub(_replace, text)
    lines = [line.rstrip() for line in stripped.replace("\r\n", "\n").split("\n")]
    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        cleaned.append(line)
        previous_blank = blank
    return "\n".join(cleaned).strip()
