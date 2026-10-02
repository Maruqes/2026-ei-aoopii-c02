"""Run with TEST_DATABASE_URL pointing to a disposable Postgres database."""

from datetime import datetime, timedelta, timezone

import pytest
from app.agent import SessionAgent
from app.docs_client import LocalMarkdownProfileClient

from data.repository import MessageInsert


def session(r, guild="one"):
    return r.create_voice_session(
        guild_id=guild,
        voice_channel_id=guild,
        channel_name="general",
        summary_channel_id="text",
        started_at=datetime.now(timezone.utc),
    )


def recording(r, s, filename="test.wav"):
    return r.start_recording(
        session_id=s.id,
        recording_filename=filename,
        discord_id="123",
        metadata={
            "discord_id": "123",
            "username": "Alice",
            "channel_name": "general",
            "recording_started_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def insert(r, s, id, content="We agreed to ship the bot tomorrow."):
    return r.insert_transcription_segments(
        session_id=s.id,
        recording_id=id,
        discord_id="123",
        username="Alice",
        display_name="Alice",
        channel_name="general",
        duration_seconds=300,
        provider_completed_at=datetime.now(timezone.utc),
        messages=[MessageInsert(content, datetime.now(timezone.utc))],
    )


def test_recording_admission_and_commit_are_idempotent(repository):
    r = repository
    s = session(r)
    id = recording(r, s)
    assert recording(r, s) == id
    assert insert(r, s, id).message_ids
    assert insert(r, s, id).message_ids == []
    assert len(r.get_session_messages(s.id)) == 1
    r.finish_voice_session(s.id, datetime.now(timezone.utc))
    assert recording(r, s) == id
    with pytest.raises(ValueError):
        recording(r, s, "late.wav")
    assert r.get_session_recording_counts(s.id) == {"completed": 1}


def test_session_waits_for_recordings_and_retains_language(repository):
    r = repository
    s = session(r)
    id = recording(r, s)
    r.finish_voice_session(s.id, datetime.now(timezone.utc), "en")
    assert not r.claim_session_agent_run(s.id)
    insert(r, s, id)
    assert r.claim_session_agent_run(s.id)
    assert r.get_voice_session(s.id).response_language == "en"


def test_advisory_lock_prevents_competing_workers_and_recovers_after_exit(repository):
    r = repository
    with r.job_lock(101, 7) as first:
        assert first
        with r.job_lock(101, 7) as second:
            assert not second
    with r.job_lock(101, 7) as recovered:
        assert recovered


def test_failed_recording_retry_preserves_remote_job_id(repository):
    r = repository
    s = session(r)
    id = recording(r, s)
    r.save_provider_job(id, "remote-123", "key-one")
    r.mark_recording_failed(id, "timeout")
    r.finish_voice_session(s.id, datetime.now(timezone.utc))
    r.mark_session_agent_failed(s.id, "no transcript")
    r.retry_session(s.id)
    jobs = r.get_recording_jobs()
    assert jobs[0]["provider_job_id"] == "remote-123"
    assert jobs[0]["provider_key_name"] == "key-one"
    assert r.get_session_recording_counts(s.id) == {"pending": 1}


def test_partial_summary_is_published_without_waiting_for_profiles(
    repository, tmp_path
):
    r = repository
    s = session(r)
    good, bad = recording(r, s), recording(r, s, "failed.wav")
    insert(r, s, good)
    r.mark_recording_failed(bad, "timeout")

    class LLM:
        def summarize_session(self, *args, **kwargs):
            return "A useful recap."

        def update_profile(self, *args, **kwargs):
            raise AssertionError("Profiles must run independently")

    summary = SessionAgent(
        repository=r, llm=LLM(), docs=LocalMarkdownProfileClient(profile_dir=tmp_path)
    ).run_for_session(s.id)
    assert "Resumo parcial" in summary
    assert r.get_voice_session(s.id).status == "agent_done"
    assert len(r.get_voice_profile_jobs()) == 1


def test_guild_context_cannot_leak_a_same_named_channel(repository):
    r = repository
    one, two = session(r), session(r, "two")
    insert(r, one, recording(r, one), "Our private unicorn discussion.")
    insert(
        r, two, recording(r, two, "other.wav"), "Another server has a secret aardvark."
    )
    context = r.get_guild_oracle_context("one", "aardvark")
    assert "unicorn" in context
    assert "aardvark" not in context


def test_profile_watermark_excludes_newer_messages(repository):
    r = repository
    now = datetime.now(timezone.utc)
    for index, timestamp in enumerate(
        (now - timedelta(minutes=1), now + timedelta(minutes=1))
    ):
        r.insert_text_message(
            guild_id="one",
            channel_id="text",
            channel_name="general",
            discord_message_id=str(index),
            discord_id="123",
            username="Alice",
            display_name="Alice",
            content=f"Useful message {index}",
            tstamp=timestamp,
        )
    profile = r.get_user_profile_by_discord_id("123")
    messages = r.get_text_messages_for_profile(profile.user_id, None, now)
    assert len(messages) == 1
    assert messages[0]["content"] == "Useful message 0"


def test_text_digest_is_scoped_to_channel_and_guild(repository):
    r = repository
    for index, (guild, channel) in enumerate(
        (("one", "text"), ("two", "text"), ("one", "other"))
    ):
        r.insert_text_message(
            guild_id=guild,
            channel_id=channel,
            channel_name="general",
            discord_message_id=str(index),
            discord_id="123",
            username="Alice",
            display_name="Alice",
            content=f"Message {index}",
            tstamp=datetime.now(timezone.utc),
        )
    text, count, limited = r.get_text_digest("one", channel_id="text")
    assert count == 1 and not limited
    assert "Message 0" in text and "Message 1" not in text and "Message 2" not in text


def test_today_usage_does_not_double_count_completed_replays(repository):
    r = repository
    s = session(r)
    id = recording(r, s)
    r.save_provider_job(id, "remote", "key-one")
    insert(r, s, id)
    insert(r, s, id)
    assert r.get_local_speechmatics_hours()["key-one"] == pytest.approx(300 / 3600)


def test_older_text_delivery_cannot_revert_an_edit(repository):
    r = repository
    now = datetime.now(timezone.utc)
    args = dict(
        guild_id="one",
        channel_id="text",
        channel_name="general",
        discord_message_id="1",
        discord_id="123",
        username="Alice",
        display_name="Alice",
        tstamp=now,
    )
    r.insert_text_message(
        **args, content="Edited text", edited_at=now + timedelta(seconds=5)
    )
    r.insert_text_message(**args, content="Original text")
    text, count, _ = r.get_text_digest("one")
    assert "Edited text" in text and "Original text" not in text and count == 1
