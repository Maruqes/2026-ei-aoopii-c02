import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from app.agent import format_transcript
from app.docs_client import LocalMarkdownProfileClient
from app.llm import (
    ConversationClient,
    OpenAICompatibleClient,
    generated_profile_from_json,
    session_summary_system,
    split_evidence,
)


def valid_profile(**changes):
    values = dict(
        anthropologist_title="O oráculo do debug",
        summary="Gosta de programar.",
        interests="Go",
        communication_style="Direto",
        persona_notes="Ajuda o grupo.",
        recent_updates="Discutiu o projeto.",
        lore_title="Nova observação",
        new_observations=["Disse que usa Go."],
        reinforced_patterns=[],
        changed_interpretations=[],
        weakened_or_retired_patterns=[],
    )
    values.update(changes)
    return json.dumps(values)


def test_invalid_profile_cannot_erase_memory():
    with pytest.raises(ValueError):
        generated_profile_from_json('{"summary": ""}')
    with pytest.raises(ValueError):
        generated_profile_from_json(valid_profile(summary=""))
    with pytest.raises(ValueError):
        generated_profile_from_json(valid_profile(interests=["Go"]))
    with pytest.raises(ValueError):
        generated_profile_from_json(valid_profile(new_observations=[{}]))


def test_empty_fields_preserve_existing_observations():
    existing = SimpleNamespace(interests="Go e música")
    generated = generated_profile_from_json(
        valid_profile(interests=""), existing_profile=existing
    )
    assert generated.interests == "Go e música"


def test_long_conversation_retains_all_slices_and_order():
    class FakeClient(ConversationClient):
        context_chars = 120

        def __init__(self):
            self.inputs = []

        def _chat(self, **kwargs):
            payload = json.loads(kwargs["user"])
            self.inputs.append(payload["evidence"])
            return f"Topic {payload['slice']} retained."

    text = "".join(
        f"[10:{i:02d}] Alice: Topic {i}. A decision to discuss.\n" for i in range(12)
    )
    client = FakeClient()
    notes = client._distill(text)
    # First-level extraction sees every original message; no prefix-only truncation.
    first_level = client.inputs[: len(split_evidence(text, client.context_chars))]
    assert "".join(first_level) == text
    assert len(notes) <= client.context_chars


def test_splitter_preserves_even_a_single_oversized_message():
    text = "x" * 201 + "\nsecond line\n"
    parts = split_evidence(text, 100)
    assert "".join(parts) == text
    assert max(map(len, parts)) <= 100


def test_truncated_llm_output_is_rejected():
    client = OpenAICompatibleClient(
        api_key="fake", base_url="https://example.invalid", model="test"
    )

    def create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content='{"summary":"partial'),
                )
            ]
        )

    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    with pytest.raises(RuntimeError, match="truncated"):
        client._chat(system="test", user="test", json_format=True)


def test_profile_files_are_isolated_and_path_traversal_rejected(tmp_path):
    docs = LocalMarkdownProfileClient(profile_dir=tmp_path)
    profile = generated_profile_from_json(valid_profile())
    first = docs.upsert_profile_doc(
        doc_id="user-1.md", username="same name", profile=profile
    )
    second = docs.upsert_profile_doc(
        doc_id="user-2.md",
        username="same name",
        profile=replace(profile, summary="Outra pessoa."),
    )
    assert first.doc_id != second.doc_id
    assert "Outra pessoa" not in docs.read_doc_text(first.doc_id)
    assert "Outra pessoa" in docs.read_doc_text(second.doc_id)
    for path in ("../outside.md", str(tmp_path.parent / "outside.md")):
        with pytest.raises(ValueError):
            docs.upsert_profile_doc(doc_id=path, username="bad", profile=profile)


def test_attribution_and_full_date_survive_transcript_formatting():
    text = format_transcript(
        [
            dict(
                tstamp=datetime(2026, 10, 2, 23, 59, tzinfo=timezone.utc),
                discord_id="1",
                username="Alice",
                channel_name="general",
                content="We decided to wait.",
            )
        ]
    )
    assert "2026-10-02 23:59:00" in text
    assert "Alice [user=1]" in text
    assert "general" in text
    assert "untrusted evidence" in session_summary_system()
