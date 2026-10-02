"""HTTP regression checks using a disposable Postgres schema and no external APIs."""

from datetime import datetime, timezone

import pytest
from app import main
from app.config import Settings
from app.docs_client import LocalMarkdownProfileClient
from fastapi.testclient import TestClient
from test_repository_integration import insert, recording, session


class SummaryLLM:
    def __init__(self):
        self.calls = []

    def summarize_session(self, transcript, **kwargs):
        self.calls.append((transcript, kwargs))
        return "An evidence-based recap."


@pytest.fixture
def api(repository, tmp_path, monkeypatch):
    application = main.create_app()
    llm = SummaryLLM()
    settings = Settings(
        database_url=repository.database_url,
        recordings_dir=tmp_path,
        text_profile_sync_enabled=False,
    )
    application.dependency_overrides[main.get_repository] = lambda: repository
    application.dependency_overrides[main.get_llm_client] = lambda: llm
    application.dependency_overrides[main.get_settings] = lambda: settings
    application.dependency_overrides[main.get_docs_client] = lambda: (
        LocalMarkdownProfileClient(profile_dir=tmp_path)
    )
    monkeypatch.setattr(
        main, "maybe_schedule_session_agent", lambda *args, **kwargs: None
    )
    # Deliberately do not run the lifespan: assertions control durable workers directly.
    client = TestClient(application)
    try:
        yield client, llm
    finally:
        client.close()


def text(repository, number, guild="one", channel="text"):
    repository.insert_text_message(
        guild_id=guild,
        channel_id=channel,
        channel_name="general",
        discord_message_id=str(number),
        discord_id="123",
        username="Alice",
        display_name="Alice",
        content=f"Decision {number}: ship the bot tomorrow.",
        tstamp=datetime.now(timezone.utc),
    )


def test_digest_endpoint_filters_evidence_and_forwards_language(api, repository):
    client, llm = api
    text(repository, 1)
    text(repository, 2, guild="two")
    text(repository, 3, channel="other")
    response = client.post(
        "/v1/guilds/one/digest",
        json={"hours": 12, "channel_id": "text", "language": "en"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "summary": "An evidence-based recap.",
        "message_count": 1,
        "limited": False,
        "hours": 12,
    }
    transcript, kwargs = llm.calls[0]
    assert "Decision 1" in transcript
    assert "Decision 2" not in transcript and "Decision 3" not in transcript
    assert kwargs["language"] == "en"


@pytest.mark.parametrize("hours", [0, 169])
def test_digest_rejects_invalid_period_before_calling_llm(api, hours):
    client, llm = api
    assert (
        client.post("/v1/guilds/one/digest", json={"hours": hours}).status_code == 422
    )
    assert llm.calls == []


def test_empty_digest_avoids_an_llm_call(api):
    client, llm = api
    assert client.post("/v1/guilds/one/digest", json={}).status_code == 404
    assert llm.calls == []


def test_retry_endpoint_enforces_guild_and_active_session_state(api, repository):
    client, _ = api
    voice = session(repository)
    assert client.post(f"/v1/guilds/two/sessions/{voice.id}/retry").status_code == 404
    assert client.post(f"/v1/guilds/one/sessions/{voice.id}/retry").status_code == 409
    repository.finish_voice_session(voice.id, datetime.now(timezone.utc))
    assert client.post(f"/v1/guilds/one/sessions/{voice.id}/retry").status_code == 409


def test_retry_waits_for_active_summary_lock_and_keeps_remote_id(api, repository):
    client, _ = api
    voice = session(repository)
    recording_id = recording(repository, voice)
    repository.save_provider_job(recording_id, "remote-existing", "key-one")
    repository.mark_recording_failed(recording_id, "timeout")
    repository.finish_voice_session(voice.id, datetime.now(timezone.utc), "en")
    repository.mark_session_agent_failed(voice.id, "transcription unavailable")
    with repository.job_lock(102, voice.id) as acquired:
        assert acquired
        assert (
            client.post(f"/v1/guilds/one/sessions/{voice.id}/retry").status_code == 409
        )
    response = client.post(f"/v1/guilds/one/sessions/{voice.id}/retry")
    assert response.status_code == 200
    assert response.json()["status"] == "finished"
    assert repository.get_voice_session(voice.id).response_language == "en"
    job = repository.get_recording_jobs()[0]
    assert job["provider_job_id"] == "remote-existing"
    assert job["provider_key_name"] == "key-one"
    assert not repository.claim_session_agent_run(voice.id)


def test_transcription_endpoint_admission_survives_finish_and_duplicate(
    api, repository, tmp_path
):
    client, _ = api
    voice = session(repository)
    (tmp_path / "accepted.wav").write_bytes(b"placeholder; worker owns audio parsing")
    (tmp_path / "late.wav").write_bytes(b"placeholder")
    data = dict(
        recording_filename="accepted.wav",
        session_id=voice.id,
        discord_id="123",
        username="Alice",
        channel_name="general",
        recording_started_at=datetime.now(timezone.utc).isoformat(),
    )
    assert client.post("/v1/transcriptions", data=data).status_code == 200
    assert repository.get_session_recording_counts(voice.id) == {"pending": 1}
    assert (
        client.post(
            f"/v1/sessions/{voice.id}/finish", json={"language": "en"}
        ).status_code
        == 200
    )
    assert not repository.claim_session_agent_run(voice.id)
    assert client.post("/v1/transcriptions", data=data).status_code == 200
    assert (
        client.post(
            "/v1/transcriptions", data={**data, "recording_filename": "late.wav"}
        ).status_code
        == 409
    )
    assert repository.get_session_recording_counts(voice.id) == {"pending": 1}


def test_retry_profile_queue_waits_for_recovered_transcript_and_summary(
    api, repository
):
    client, _ = api
    voice = session(repository)
    good = recording(repository, voice, "good.wav")
    failed = recording(repository, voice, "failed.wav")
    insert(repository, voice, good)
    repository.mark_recording_failed(failed, "timeout")
    repository.finish_voice_session(voice.id, datetime.now(timezone.utc))
    repository.mark_session_agent_done(voice.id, "Partial old recap.")
    user_id = repository.get_voice_profile_jobs()[0][1]
    repository.mark_voice_profile_job(voice.id, user_id, "completed")
    assert client.post(f"/v1/guilds/one/sessions/{voice.id}/retry").status_code == 200
    assert repository.get_voice_profile_jobs() == []
    insert(repository, voice, failed, "The recovered decision was to postpone launch.")
    assert repository.get_voice_profile_jobs() == []
    repository.mark_session_agent_done(voice.id, "The corrected complete recap.")
    assert repository.get_voice_profile_jobs() == [(voice.id, user_id)]


@pytest.mark.parametrize("stale_status", ["completed", "failed"])
def test_profile_callback_from_before_retry_cannot_consume_recovered_job(
    api, repository, stale_status
):
    client, _ = api
    voice = session(repository)
    good = recording(repository, voice, "good.wav")
    failed = recording(repository, voice, "failed.wav")
    insert(repository, voice, good)
    repository.mark_recording_failed(failed, "timeout")
    repository.finish_voice_session(voice.id, datetime.now(timezone.utc))
    repository.mark_session_agent_done(voice.id, "Old partial recap.")
    user_id = repository.get_voice_profile_jobs()[0][1]
    stale_revision = repository.get_voice_profile_job_revision(voice.id, user_id)
    assert stale_revision is not None
    assert client.post(f"/v1/guilds/one/sessions/{voice.id}/retry").status_code == 200
    assert repository.get_voice_profile_job_revision(voice.id, user_id) is None
    # This callback models a slow LLM job which began before the retry.
    repository.mark_voice_profile_job(
        voice.id, user_id, stale_status, expected_revision=stale_revision
    )
    insert(
        repository, voice, failed, "The recovered participant shared a new decision."
    )
    repository.mark_session_agent_done(voice.id, "Full corrected recap.")
    current_revision = repository.get_voice_profile_job_revision(voice.id, user_id)
    assert current_revision > stale_revision
    assert repository.get_voice_profile_jobs() == [(voice.id, user_id)]
    repository.mark_voice_profile_job(
        voice.id, user_id, "completed", expected_revision=current_revision
    )
    assert repository.get_voice_profile_jobs() == []


def test_failed_summary_still_exposes_captured_transcript(repository):
    r = repository
    s = session(r)
    insert(r, s, recording(r, s), "We agreed to preserve this useful evidence.")
    r.finish_voice_session(s.id, datetime.now(timezone.utc))
    r.mark_session_agent_failed(s.id, "LLM unavailable")
    from app.main import build_session_recap

    source, recap = build_session_recap(r, r.get_voice_session(s.id))
    assert source == "transcript"
    assert "preserve this useful evidence" in recap
    assert "/retry" in recap
