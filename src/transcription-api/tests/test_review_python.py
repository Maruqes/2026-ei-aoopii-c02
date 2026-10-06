import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from app.llm import ConversationClient, split_evidence
from app.speechmatics_usage import SpeechmaticsAPIKey
from app.transcriber import (
    SpeechmaticsTranscriber,
    _provider_completed_at,
    _provider_duration_seconds,
)


class BoundedClient(ConversationClient):
    context_chars = 4000

    def __init__(self):
        self.inputs = []

    def _chat(self, **kwargs):
        self.inputs.append(kwargs)
        assert len(kwargs["system"]) + len(kwargs["user"]) + 128 <= self.context_chars
        if not kwargs["user"].startswith('{"slice":'):
            assert "Do not narrate your sources or processing" in kwargs["system"]
        if kwargs.get("json_format"):
            return json.dumps(
                dict(
                    anthropologist_title="Programador",
                    summary="Programa em Go.",
                    interests="Go",
                    communication_style="Direto",
                    persona_notes="Ajuda nos projetos.",
                    recent_updates="Disse que programa.",
                    lore_title="Discussão de código",
                    new_observations=["Disse que usa Go."],
                    reinforced_patterns=[],
                    changed_interpretations=[],
                    weakened_or_retired_patterns=[],
                )
            )
        if kwargs["user"].startswith('{"slice":'):
            return "Retained dated evidence."
        return "Uma resposta útil."


def test_final_context_budget_includes_all_prompt_and_memory():
    evidence = "\n".join(
        f"[2026-10-02 10:{i % 60:02d}] Alice: I like programming and play games. " * 5
        for i in range(100)
    )
    client = BoundedClient()
    assert client.summarize_session(evidence, session_context="Voice call with Alice.")
    assert client.answer_guild_question(
        guild_context=evidence, question="What games did we discuss?"
    )
    assert client.answer_profile_question(
        username="Alice", profile_doc_text=evidence, question="What does Alice like?"
    )
    assert client.update_profile_from_text(
        username="Alice",
        existing_profile=None,
        existing_doc_text=evidence,
        observations=evidence,
    ).summary
    assert any(call.get("json_format") for call in client.inputs)


def test_json_escaped_slices_stay_bounded_without_losing_source():
    evidence = '"\\\n\t\x00' * 4000
    client = BoundedClient()
    client._distill(evidence, max_chars=1800)
    source_slices = []
    for call in client.inputs:
        payload = json.loads(call["user"])
        part = payload["evidence"]
        if "\x00" in part:
            source_slices.append(part)
    assert "".join(source_slices) == evidence


def test_huge_question_fails_before_any_provider_call():
    client = BoundedClient()
    with pytest.raises(ValueError, match="too small"):
        client.answer_guild_question(guild_context="A fact", question="x" * 4000)
    assert not client.inputs


def test_empty_summary_cannot_be_published():
    class EmptyClient(ConversationClient):
        def _chat(self, **kwargs):
            return "   "

    with pytest.raises(ValueError, match="empty session summary"):
        EmptyClient().summarize_session("Alice: Hello!")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-10-02T00:15:00Z", datetime(2026, 10, 2, 0, 15, tzinfo=timezone.utc)),
        (
            "2026-10-02T00:15:00+01:00",
            datetime(2026, 10, 1, 23, 15, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 10, 2, 0, 15),
            datetime(2026, 10, 2, 0, 15, tzinfo=timezone.utc),
        ),
        ("invalid", None),
        ("2026-10-02", None),
        (None, None),
    ],
)
def test_provider_output_date_is_utc_and_missing_dates_remain_unknown(value, expected):
    assert (
        _provider_completed_at(
            SimpleNamespace(metadata=SimpleNamespace(created_at=value))
        )
        == expected
    )


@pytest.mark.parametrize(
    "duration,expected",
    [
        (0, 0),
        (120.5, 120.5),
        ("60", 60),
        (None, None),
        (True, None),
        (-1, None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_provider_duration_ignores_invalid_values(duration, expected):
    assert (
        _provider_duration_seconds(
            SimpleNamespace(job=SimpleNamespace(duration=duration))
        )
        == expected
    )


def test_real_sdk_job_is_saved_before_polling_and_resumed_after_timeout(tmp_path):
    from speechmatics.batch import Transcript
    from speechmatics.batch._models import JobInfo, RecognitionMetadata

    transcript = Transcript(
        format="2.9",
        job=JobInfo(
            id="remote-job",
            created_at="2026-10-01T20:00:00Z",
            data_name="audio.wav",
            duration=42.5,
        ),
        metadata=RecognitionMetadata(
            created_at="2026-10-02T00:03:00Z", type="transcription"
        ),
        results=[],
    )
    events = []
    first_poll = True

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "secret"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def submit_job(self, *args, **kwargs):
            events.append("submit")
            return SimpleNamespace(id="remote-job")

        async def wait_for_completion(self, job_id, **kwargs):
            nonlocal first_poll
            assert job_id == "remote-job"
            assert "saved" in events
            events.append("poll")
            if first_poll:
                first_poll = False
                raise TimeoutError("remote job is still running")
            return transcript

    transcriber = SpeechmaticsTranscriber("secret", client_factory=FakeClient)

    def save(job_id, key_name):
        assert job_id == "remote-job"
        assert key_name == "SPEECHMATICS_API_KEY"
        events.append("saved")

    with pytest.raises(TimeoutError):
        transcriber.transcribe_recording(
            tmp_path / "audio.wav", job_id=None, key_name=None, save_job=save
        )
    result = transcriber.transcribe_recording(
        tmp_path / "audio.wav",
        job_id="remote-job",
        key_name="SPEECHMATICS_API_KEY",
        save_job=save,
    )
    assert events == ["submit", "saved", "poll", "poll"]
    assert result.duration_seconds == 42.5
    assert result.provider_completed_at == datetime(
        2026, 10, 2, 0, 3, tzinfo=timezone.utc
    )


def test_api_key_representations_never_disclose_secret():
    assert "secret" not in repr(SpeechmaticsAPIKey("primary", "secret"))


def test_invalid_split_limit_cannot_loop_forever():
    with pytest.raises(ValueError):
        split_evidence("evidence", 0)


def test_hierarchical_compaction_has_a_round_limit():
    class BarelyShrinkingClient(ConversationClient):
        context_chars = 120

        def __init__(self):
            self.calls = 0

        def _chat(self, **kwargs):
            self.calls += 1
            return json.loads(kwargs["user"])["evidence"][:-12]

    client = BarelyShrinkingClient()
    with pytest.raises(RuntimeError, match="within 8 rounds"):
        client._distill("x" * 1500)
    assert client.calls <= 8 * 13


def test_profile_lore_uses_observation_date_and_retries_do_not_duplicate(tmp_path):
    from dataclasses import replace
    from datetime import date

    from app.docs_client import LocalMarkdownProfileClient
    from app.llm import GeneratedProfile, LoreEvent

    docs = LocalMarkdownProfileClient(profile_dir=tmp_path)
    profile = GeneratedProfile(
        "Programador",
        "Usa Go.",
        "Go",
        "Direto",
        "Ajuda o grupo.",
        "Discutiu código.",
        LoreEvent("Call sobre Go", ["Disse que usa Go."], [], [], []),
    )
    kwargs = dict(
        doc_id="user-1.md",
        username="Alice",
        profile=profile,
        observed_on=date(2026, 9, 20),
        observation_id="voice-session-42",
    )
    docs.upsert_profile_doc(**kwargs)
    docs.upsert_profile_doc(**kwargs)
    markdown = docs.read_doc_text("user-1.md")
    assert "2026-09-20 - Call sobre Go" in markdown
    assert markdown.count("### 2026-09-20 -") == 1
    docs.upsert_profile_doc(
        **(
            kwargs
            | {
                "profile": replace(profile, recent_updates="Novo comentário."),
                "observed_on": date(2026, 9, 21),
                "observation_id": "voice-session-43",
            }
        )
    )
    markdown = docs.read_doc_text("user-1.md")
    assert markdown.count("### 2026-09-20 -") == 1
    assert markdown.count("### 2026-09-21 -") == 1
    assert "Novo comentário." in markdown
