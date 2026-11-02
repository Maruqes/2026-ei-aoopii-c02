"""Audio lifecycle regressions with real transactions and disposable media."""

from datetime import datetime, timezone

import pytest
from app import main
from app.config import Settings
from app.recording_cleanup import RecordingCleanup, remove_recording_files
from fastapi.testclient import TestClient
from test_repository_integration import insert, recording, session

from data.repository import MessageInsert


def cleaner(repository, directory):
    return RecordingCleanup(
        repository=repository,
        settings=Settings(
            database_url=repository.database_url, recordings_dir=directory
        ),
    )


def files(directory, filename):
    paths = [
        directory / (filename + suffix)
        for suffix in ("", ".speechmatics.json", ".speechmatics.tmp", ".request.json")
    ]
    for path in paths:
        path.write_bytes(b"local recording or recovery metadata")
    return paths


def test_completed_media_and_provider_sidecars_removed_but_outbox_owned_by_go(
    repository, tmp_path
):
    voice = session(repository)
    completed = recording(repository, voice, "done.wav")
    paths = files(tmp_path, "done.wav")
    pending = files(tmp_path, "pending.wav")
    recording(repository, voice, "pending.wav")
    failed_id = recording(repository, voice, "failed.wav")
    failed = files(tmp_path, "failed.wav")
    repository.mark_recording_failed(failed_id, "retry useful")
    unknown = files(tmp_path, "unknown.wav")
    insert(repository, voice, completed)
    cleanup = cleaner(repository, tmp_path)
    cleanup.sweep()
    cleanup.sweep()
    assert not any(path.exists() for path in paths[:3])
    assert paths[3].exists()
    assert all(path.exists() for path in pending + failed + unknown)
    assert len(repository.get_session_messages(voice.id)) == 1


def test_janitor_does_not_delete_file_held_by_worker(repository, tmp_path):
    voice = session(repository)
    recording_id = recording(repository, voice)
    paths = files(tmp_path, "test.wav")
    insert(repository, voice, recording_id)
    cleanup = cleaner(repository, tmp_path)
    with repository.job_lock(101, recording_id) as locked:
        assert locked
        assert not cleanup.cleanup_completed_file("test.wav")
        assert all(path.exists() for path in paths)
    assert cleanup.cleanup_completed_file("test.wav")


def test_cleanup_permission_failure_leaves_committed_transcript_retryable(
    repository, tmp_path, monkeypatch
):
    voice = session(repository)
    recording_id = recording(repository, voice)
    files(tmp_path, "test.wav")
    insert(repository, voice, recording_id)
    cleanup = cleaner(repository, tmp_path)
    with monkeypatch.context() as context:
        context.setattr(
            "app.recording_cleanup.remove_recording_files",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                PermissionError("storage readonly")
            ),
        )
        assert not cleanup.cleanup_completed_file("test.wav")
    assert repository.get_session_recording_counts(voice.id) == {"completed": 1}
    assert cleanup.cleanup_completed_file("test.wav")


def test_completed_duplicate_accepted_after_media_cleanup_with_strict_metadata(
    repository, tmp_path
):
    voice = session(repository)
    recording_id = recording(repository, voice)
    insert(repository, voice, recording_id)
    repository.finish_voice_session(voice.id, datetime.now(timezone.utc))
    application = main.create_app()
    application.dependency_overrides[main.get_repository] = lambda: repository
    application.dependency_overrides[main.get_settings] = lambda: Settings(
        database_url=repository.database_url, recordings_dir=tmp_path
    )
    data = {
        "recording_filename": "test.wav",
        "session_id": voice.id,
        "discord_id": "123",
        "username": "Alice",
        "channel_name": "general",
        "recording_started_at": datetime.now(timezone.utc).isoformat(),
    }
    client = TestClient(application)
    try:
        assert client.post("/v1/transcriptions", data=data).status_code == 200
        assert (
            client.post(
                "/v1/transcriptions", data={**data, "discord_id": "other"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/v1/transcriptions", data={**data, "session_id": voice.id + 1}
            ).status_code
            == 409
        )
    finally:
        client.close()
    assert repository.get_session_recording_counts(voice.id) == {"completed": 1}
    assert len(repository.get_session_messages(voice.id)) == 1


def test_forget_erases_only_owner_recordings_and_preserves_active_worker(
    repository, tmp_path
):
    voice = session(repository)
    owner_id = recording(repository, voice)
    owner_paths = files(tmp_path, "test.wav")
    insert(repository, voice, owner_id)
    other_id = repository.start_recording(
        session_id=voice.id,
        recording_filename="other.wav",
        discord_id="456",
        metadata={"discord_id": "456"},
    )
    other_paths = files(tmp_path, "other.wav")
    repository.insert_transcription_segments(
        recording_id=other_id,
        session_id=voice.id,
        discord_id="456",
        username="Bob",
        display_name=None,
        channel_name="general",
        messages=[MessageInsert("Other owner", datetime.now(timezone.utc))],
    )

    def forget():
        return repository.delete_user_by_discord_id(
            "123",
            remove_recording=lambda filename: remove_recording_files(
                tmp_path, filename, include_request=True
            ),
        )

    with repository.job_lock(101, owner_id) as locked:
        assert locked
        with pytest.raises(ValueError, match="Transcription in progress"):
            forget()
        assert all(path.exists() for path in owner_paths)
        assert repository.recording_receipt("test.wav", "123", voice.id)
    assert forget()["messages_deleted"] == 1
    assert not any(path.exists() for path in owner_paths)
    assert all(path.exists() for path in other_paths)
    with pytest.raises(ValueError, match="removed"):
        insert(repository, voice, owner_id)
    assert repository.get_user_profile_by_discord_id("123") is None


def test_forget_storage_failure_keeps_db_owner_for_explicit_retry(repository, tmp_path):
    voice = session(repository)
    recording_id = recording(repository, voice)
    insert(repository, voice, recording_id)

    def fail(filename):
        raise PermissionError("storage readonly")

    with pytest.raises(PermissionError):
        repository.delete_user_by_discord_id("123", remove_recording=fail)
    assert (
        repository.recording_receipt("test.wav", "123", voice.id)["status"]
        == "completed"
    )
    assert len(repository.get_session_messages(voice.id)) == 1


def test_cleanup_refuses_symlinks_and_path_traversal(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-private.wav")
    outside.write_bytes(b"private")
    (tmp_path / "linked.wav").symlink_to(outside)
    try:
        with pytest.raises(ValueError):
            remove_recording_files(tmp_path, "linked.wav")
        with pytest.raises(ValueError):
            remove_recording_files(tmp_path, "../" + outside.name)
        assert outside.read_bytes() == b"private"
    finally:
        outside.unlink()


@pytest.mark.parametrize(
    "outcome", ["success", "transcription_failure", "cleanup_failure"]
)
def test_worker_audio_cleanup_happens_only_after_transcript_commit(
    repository, tmp_path, monkeypatch, outcome
):
    import wave

    from app.transcriber import TranscriptionResult, TranscriptionSegment

    voice = session(repository)
    recording_id = recording(repository, voice)
    paths = files(tmp_path, "test.wav")
    with wave.open(str(paths[0]), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 160)

    class Transcriber:
        provider_name = "offline"
        model_name = "test"

        def transcribe(self, path):
            assert path.exists()
            if outcome == "transcription_failure":
                raise RuntimeError("offline provider unavailable")
            return TranscriptionResult(
                text="Ship tomorrow.",
                segments=[TranscriptionSegment(0, 1, "Ship tomorrow.")],
            )

    if outcome == "cleanup_failure":
        monkeypatch.setattr(
            "app.recording_cleanup.remove_recording_files",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                PermissionError("storage readonly")
            ),
        )
    with repository.job_lock(101, recording_id) as locked:
        assert locked
        main.process_recording_file(
            recording_path=paths[0],
            recording_id=recording_id,
            recording_lock_held=True,
            session_id=voice.id,
            discord_id="123",
            username="Alice",
            display_name=None,
            channel_name="general",
            recording_started_at=datetime.now(timezone.utc),
            settings=Settings(
                database_url=repository.database_url,
                recordings_dir=tmp_path,
                keep_uploads=True,
            ),
            repository=repository,
            transcriber=Transcriber(),
        )
    if outcome == "success":
        assert not any(path.exists() for path in paths[:3])
    else:
        assert all(path.exists() for path in paths)
    expected = "failed" if outcome == "transcription_failure" else "completed"
    assert repository.get_session_recording_counts(voice.id) == {expected: 1}
    assert bool(repository.get_session_messages(voice.id)) == (expected == "completed")


def test_cleanup_runs_on_startup_and_stops_orderly(repository, tmp_path, monkeypatch):
    from threading import Event

    voice = session(repository)
    recording_id = recording(repository, voice)
    paths = files(tmp_path, "test.wav")
    insert(repository, voice, recording_id)
    removed = Event()
    original = remove_recording_files

    def remove(*args, **kwargs):
        original(*args, **kwargs)
        removed.set()

    monkeypatch.setattr("app.recording_cleanup.remove_recording_files", remove)
    cleanup = cleaner(repository, tmp_path)
    cleanup.start()
    try:
        assert removed.wait(3)
        assert not paths[0].exists()
    finally:
        cleanup.close()
    assert not cleanup.thread.is_alive()


def test_forget_before_first_transcription_commit_respects_worker_and_removes_pending(
    repository, tmp_path
):
    from app.docs_client import LocalMarkdownProfileClient

    voice = session(repository)
    recording_id = recording(repository, voice)
    paths = files(tmp_path, "test.wav")
    application = main.create_app()
    application.dependency_overrides[main.get_repository] = lambda: repository
    application.dependency_overrides[main.get_settings] = lambda: Settings(
        database_url=repository.database_url, recordings_dir=tmp_path
    )
    application.dependency_overrides[main.get_docs_client] = lambda: (
        LocalMarkdownProfileClient(profile_dir=tmp_path / "profiles")
    )
    client = TestClient(application)
    try:
        with repository.job_lock(101, recording_id) as locked:
            assert locked
            response = client.delete("/v1/users/123")
            assert response.status_code == 409
            assert "Transcription in progress" in response.json()["detail"]
            assert all(path.exists() for path in paths)
        response = client.delete("/v1/users/123")
        assert response.status_code == 200
        assert response.json()["messages_deleted"] == 0
        assert not any(path.exists() for path in paths)
        assert repository.get_recording_jobs() == []
        assert client.delete("/v1/users/123").status_code == 404
    finally:
        client.close()
    with pytest.raises(ValueError, match="removed"):
        insert(repository, voice, recording_id)


def test_unusable_fallback_units_are_failed_instead_of_retried(tmp_path):
    import os
    import time
    from datetime import timedelta

    from app.recording_cleanup import (
        REALTIME_RECOVERY_GRACE_SECONDS,
        recover_realtime_wavs,
    )

    class Repo:
        def __init__(self, filename, created_at):
            self.filename = filename
            self.created_at = created_at
            self.failed = []
            self.admitted = []

        def recoverable_realtime_units(self):
            return [(16670, self.filename, self.created_at)]

        def mark_recording_failed(self, recording_id, error):
            self.failed.append((recording_id, error))

        def admit_realtime_recovery(self, recording_id):
            self.admitted.append(recording_id)

    now = datetime.now(timezone.utc)

    # A missing WAV gets a grace period for a live writer, then fails cleanly.
    recent = Repo("gone.wav", now)
    recover_realtime_wavs(recent, tmp_path)
    assert recent.failed == []
    stale = Repo(
        "gone.wav", now - timedelta(seconds=REALTIME_RECOVERY_GRACE_SECONDS + 1)
    )
    recover_realtime_wavs(stale, tmp_path)
    assert [unit for unit, _ in stale.failed] == [16670]
    assert "never written" in stale.failed[0][1]

    # A zero-byte stale WAV can never be transcribed, so it must not loop.
    empty = tmp_path / "empty.wav"
    empty.touch()
    old = time.time() - 120
    os.utime(empty, (old, old))
    unusable = Repo("empty.wav", now)
    recover_realtime_wavs(unusable, tmp_path)
    assert [unit for unit, _ in unusable.failed] == [16670]
    assert "unusable fallback WAV" in unusable.failed[0][1]
    assert unusable.admitted == []

    # A file modified moments ago may still be written by the bot.
    fresh = tmp_path / "fresh.wav"
    fresh.touch()
    writing = Repo("fresh.wav", now)
    recover_realtime_wavs(writing, tmp_path)
    assert writing.failed == [] and writing.admitted == []
