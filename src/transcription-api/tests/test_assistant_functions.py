from __future__ import annotations

import re
from pathlib import Path

import pytest
from app import assistant_functions as af
from app.assistant_functions import (
    FALLBACK_MESSAGE,
    ToolValidationError,
    dispatch,
    execute_assistant_answer,
    get_assistant_function_prompt,
    load_prompt_template,
    render_function_catalog,
)
from app.assistant_tool_parser import (
    MAX_USE_BLOCKS,
    parse_use_blocks,
    strip_use_blocks,
)
from app.llm import ConversationClient

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_prompt_template_has_single_placeholder():
    text = (APP_DIR / "assistant_prompt.md").read_text(encoding="utf-8")
    assert text.count("{FUNCTIONS}") == 1
    assert len(text.splitlines()) <= 60


def test_prompt_render_injects_catalog():
    rendered = get_assistant_function_prompt()
    assert "{FUNCTIONS}" not in rendered
    assert "USE{" in rendered
    assert "PLAY_MUSIC" in rendered
    assert "STOP_MUSIC" in rendered


def test_yaml_loads_two_v1_functions():
    assert set(af.FUNCTIONS) == {"PLAY_MUSIC", "STOP_MUSIC"}
    assert af.FUNCTIONS["PLAY_MUSIC"]["params"][0]["name"] == "search"
    assert af.FUNCTIONS["STOP_MUSIC"]["params"] == []


def test_catalog_render_lines():
    catalog = render_function_catalog()
    assert "PLAY_MUSIC(search)" in catalog
    assert "STOP_MUSIC()" in catalog
    assert "USE{PLAY_MUSIC" in catalog


def test_answer_question_system_includes_catalog():
    captured = {}

    class Client(ConversationClient):
        def _chat(self, **kwargs):
            captured.update(kwargs)
            return "ok"

    Client().answer_question(question="hi")
    assert "USE{" in captured["system"]
    assert "PLAY_MUSIC" in captured["system"]


def test_parse_single_play():
    calls = parse_use_blocks("hi USE{PLAY_MUSIC(lofi hip hop)} bye")
    assert [(c.name, c.args) for c in calls] == [("PLAY_MUSIC", ["lofi hip hop"])]


def test_parse_stop_empty():
    calls = parse_use_blocks("USE{STOP_MUSIC()}")
    assert [(c.name, c.args) for c in calls] == [("STOP_MUSIC", [])]


def test_parse_multi_order():
    calls = parse_use_blocks("a USE{PLAY_MUSIC(lofi)} b USE{STOP_MUSIC()} c")
    assert [c.name for c in calls] == ["PLAY_MUSIC", "STOP_MUSIC"]
    assert calls[0].args == ["lofi"]


def test_parse_quoted_comma_preserved():
    calls = parse_use_blocks('USE{PLAY_MUSIC("a, b")}')
    assert calls[0].args == ["a, b"]
    calls2 = parse_use_blocks("USE{PLAY_MUSIC('x, y, z')}")
    assert calls2[0].args == ["x, y, z"]


def test_parse_malformed_unclosed_ignored():
    assert parse_use_blocks("USE{PLAY_MUSIC(lofi") == []
    assert strip_use_blocks("USE{PLAY_MUSIC(lofi") == "USE{PLAY_MUSIC(lofi"


def test_parse_lowercase_ignored():
    assert parse_use_blocks("USE{play_music(lofi)}") == []


def test_parse_multiline_ignored():
    assert parse_use_blocks("USE{PLAY_MUSIC(a\nb)}") == []


def test_parse_nested_braces_ignored():
    assert parse_use_blocks("USE{PLAY_MUSIC(a{b})}") == []


def test_parse_cap_five():
    text = " ".join(f"USE{{PLAY_MUSIC(song{i})}}" for i in range(7))
    calls = parse_use_blocks(text)
    assert len(calls) == MAX_USE_BLOCKS == 5
    assert calls[0].args == ["song0"]
    assert calls[4].args == ["song4"]


def test_strip_removes_blocks_and_collapses():
    out = strip_use_blocks("hello\nUSE{PLAY_MUSIC(lofi)}\n\n\nworld")
    assert "USE{" not in out
    assert "hello" in out and "world" in out
    assert "\n\n\n" not in out


def test_strip_leaves_malformed_text():
    text = "keep USE{play_music(x)} end"
    assert strip_use_blocks(text) == text


def test_dispatch_play_ok():
    result = dispatch("PLAY_MUSIC", ["lofi hip hop"])
    assert result["spoken"] == "Queued: lofi hip hop"
    assert result["intent"] == {"action": "play", "query": "lofi hip hop"}


def test_dispatch_stop_ok():
    result = dispatch("STOP_MUSIC", [])
    assert result["spoken"] == "Stopped playback."
    assert result["intent"] == {"action": "stop"}


def test_dispatch_unknown_raises():
    with pytest.raises(ToolValidationError):
        dispatch("UNKNOWN", [])


def test_dispatch_empty_search_rejects():
    with pytest.raises(ToolValidationError):
        dispatch("PLAY_MUSIC", [])
    with pytest.raises(ToolValidationError):
        dispatch("PLAY_MUSIC", ["   "])


def test_dispatch_overlong_rejects():
    with pytest.raises(ToolValidationError):
        dispatch("PLAY_MUSIC", ["x" * 201])


def test_dispatch_arity_mismatch():
    with pytest.raises(ToolValidationError):
        dispatch("STOP_MUSIC", ["extra"])
    with pytest.raises(ToolValidationError):
        dispatch("PLAY_MUSIC", ["a", "b"])


def test_execute_multi_order_and_stripped():
    order: list[str] = []
    orig_play = af.HANDLERS["PLAY_MUSIC"]
    orig_stop = af.HANDLERS["STOP_MUSIC"]

    def play(search: str):
        order.append(f"play:{search}")
        return orig_play(search)

    def stop():
        order.append("stop")
        return orig_stop()

    af.HANDLERS["PLAY_MUSIC"] = play
    af.HANDLERS["STOP_MUSIC"] = stop
    try:
        spoken, intents = execute_assistant_answer(
            "Ok! USE{PLAY_MUSIC(lofi)} done USE{STOP_MUSIC()}"
        )
    finally:
        af.HANDLERS["PLAY_MUSIC"] = orig_play
        af.HANDLERS["STOP_MUSIC"] = orig_stop
    assert order == ["play:lofi", "stop"]
    assert "USE{" not in spoken
    assert "Queued: lofi" in spoken
    assert "Stopped playback." in spoken
    assert intents == [{"action": "play", "query": "lofi"}, {"action": "stop"}]


def test_execute_no_call_passthrough():
    spoken, intents = execute_assistant_answer("Just a normal answer.")
    assert spoken == "Just a normal answer."
    assert intents == []


def test_execute_unknown_fallback_keeps_text():
    spoken, _ = execute_assistant_answer("Hello USE{UNKNOWN()}")
    assert "USE{" not in spoken
    assert "Hello" in spoken
    assert FALLBACK_MESSAGE in spoken


def test_execute_empty_search_fallback():
    spoken, _ = execute_assistant_answer("Sure USE{PLAY_MUSIC()}")
    assert "USE{" not in spoken
    assert FALLBACK_MESSAGE in spoken


def test_execute_only_block_synthesizes_confirmation():
    spoken, _ = execute_assistant_answer("USE{STOP_MUSIC()}")
    assert spoken.strip() != ""
    assert "USE{" not in spoken
    assert "Stopped playback." in spoken


def test_execute_only_failing_block_returns_fallback():
    spoken, _ = execute_assistant_answer("USE{UNKNOWN()}")
    assert spoken.strip() != ""
    assert "USE{" not in spoken
    assert FALLBACK_MESSAGE in spoken


def test_load_prompt_template_missing_placeholder_raises(tmp_path, monkeypatch):
    bad = tmp_path / "assistant_prompt.md"
    bad.write_text("no placeholder here", encoding="utf-8")
    monkeypatch.setattr(af, "PROMPT_PATH", bad)
    with pytest.raises(RuntimeError):
        load_prompt_template()


def test_system_prompt_reflects_md_edit():
    assert "one call per block" in get_assistant_function_prompt().lower()
    assert re.search(r"USE\{NAME\(args\)\}", get_assistant_function_prompt())
