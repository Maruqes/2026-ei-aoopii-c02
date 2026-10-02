"""Durable recovery regressions; transcribers and language models stay offline."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event, Thread

import pytest
from app import main
from app.agent import SessionAgent
from app.config import Settings
from app.docs_client import LocalMarkdownProfileClient
from app.workers import RecordingWorkers
from fastapi.testclient import TestClient
from test_repository_integration import insert, recording, session

from data.repository import connect


def workers(repository, tmp_path, processor):
    return RecordingWorkers(
        repository=repository,
        settings=Settings(
            database_url=repository.database_url, recordings_dir=tmp_path
        ),
        transcriber_factory=lambda: object(),
        process_recording=processor,
    )


def test_restart_recovers_transcribing_job_with_existing_provider_id(
    repository, tmp_path
):
    voice = session(repository)
    recording_id = recording(repository, voice)
    repository.begin_recording_job(recording_id)
    repository.save_provider_job(recording_id, "paid-remote-job", "key-one")
    captured = []

    def process(**kwargs):
        captured.append(kwargs)
        insert(repository, voice, kwargs["recording_id"])
        worker.stop.set()

    worker = workers(repository, tmp_path, process)
    worker.run()
    assert len(captured) == 1
    assert captured[0]["provider_job_id"] == "paid-remote-job"
    assert captured[0]["provider_key_name"] == "key-one"
    assert captured[0]["recording_started_at"].tzinfo is not None
    assert repository.get_session_recording_counts(voice.id) == {"completed": 1}


def test_worker_failure_is_persisted_without_killing_queue(repository, tmp_path):
    voice = session(repository)
    failed = recording(repository, voice, "failed.wav")
    good = recording(repository, voice, "good.wav")
    captured = []

    def process(**kwargs):
        captured.append(kwargs["recording_id"])
        if kwargs["recording_id"] == failed:
            raise RuntimeError("offline test failure")
        insert(repository, voice, good)
        worker.stop.set()

    worker = workers(repository, tmp_path, process)
    worker.run()
    assert captured == [failed, good]
    assert repository.get_session_recording_counts(voice.id) == {
        "failed": 1,
        "completed": 1,
    }


def test_recording_worker_cannot_take_a_job_owned_by_another_process(
    repository, tmp_path
):
    voice = session(repository)
    recording_id = recording(repository, voice)
    observed_queue = Event()
    original = repository.get_recording_jobs

    def snapshot():
        jobs = original()
        observed_queue.set()
        return jobs

    repository.get_recording_jobs = snapshot
    captured = []
    worker = workers(repository, tmp_path, lambda **kwargs: captured.append(kwargs))
    with repository.job_lock(101, recording_id) as acquired:
        assert acquired
        runner = Thread(target=worker.run)
        runner.start()
        assert observed_queue.wait(2)
        worker.stop.set()
        runner.join(2)
        assert not runner.is_alive()
    assert captured == []
    assert repository.get_session_recording_counts(voice.id) == {"pending": 1}


def test_ready_sessions_are_not_starved_by_one_hundred_blocked_sessions(repository):
    # Queue scans must filter blocked sessions before applying their page limit.
    connection = connect(repository.database_url)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO voice_sessions (guild_id, voice_channel_id, channel_name, started_at, status) "
            "SELECT 'one', 'voice', 'general', NOW(), 'finished' FROM generate_series(1, 100) RETURNING id"
        )
        ids = [row[0] for row in cursor.fetchall()]
        cursor.executemany(
            "INSERT INTO voice_recordings (session_id, recording_filename, discord_id, status, metadata) "
            "VALUES (%s, %s, '123', 'pending', '{}'::jsonb)",
            [(id, f"blocked-{id}.wav") for id in ids],
        )
        connection.commit()
    finally:
        connection.close()
    ready = session(repository)
    repository.finish_voice_session(ready.id, datetime.now(timezone.utc))
    assert ready.id in repository.get_unfinished_session_ids()


def test_finish_and_recording_admission_are_serialized(repository):
    voice = session(repository)
    gate = Barrier(9)

    def submit(index):
        gate.wait(timeout=5)
        try:
            return recording(repository, voice, f"concurrent-{index}.wav")
        except ValueError:
            return None

    def finish():
        gate.wait(timeout=5)
        repository.finish_voice_session(voice.id, datetime.now(timezone.utc))

    with ThreadPoolExecutor(max_workers=9) as executor:
        attempts = [executor.submit(submit, index) for index in range(8)]
        closure = executor.submit(finish)
        accepted = [future.result(timeout=10) for future in attempts]
        closure.result(timeout=10)
    admitted_count = sum(id is not None for id in accepted)
    assert repository.get_session_recording_counts(voice.id) == (
        {"pending": admitted_count} if admitted_count else {}
    )
    # A recap can be claimed exactly when every upload lost the admission race.
    assert repository.claim_session_agent_run(voice.id) is (admitted_count == 0)


@pytest.mark.parametrize("recover_state", ["transcribing", "agent_running"])
def test_lifespan_recovers_recording_summary_and_profile_queue(
    repository, tmp_path, monkeypatch, recover_state
):
    voice = session(repository)
    recording_id = recording(repository, voice)
    repository.save_provider_job(recording_id, "already-paid", "key-one")
    repository.finish_voice_session(voice.id, datetime.now(timezone.utc), "en")
    if recover_state == "transcribing":
        repository.begin_recording_job(recording_id)
    else:
        insert(repository, voice, recording_id)
        assert repository.claim_session_agent_run(voice.id)

    settings = Settings(
        database_url=repository.database_url,
        recordings_dir=tmp_path,
        text_profile_sync_enabled=False,
    )
    summary_calls, recording_calls, profile_calls = [], [], []
    completed = Event()

    class OfflineLLM:
        def summarize_session(self, transcript, **kwargs):
            summary_calls.append((transcript, kwargs))
            return "Recovered decision: ship the bot tomorrow."

    class OfflineAgent(SessionAgent):
        def update_participant_profile(self, session_id, user_id):
            assert self.repository.get_voice_session(session_id).summary.startswith(
                "Recovered decision"
            )
            profile_calls.append((session_id, user_id))

    def process(**kwargs):
        recording_calls.append(kwargs)
        insert(repository, voice, kwargs["recording_id"])

    mark_profile = repository.mark_voice_profile_job

    def marked(session_id, user_id, status, *args, **kwargs):
        mark_profile(session_id, user_id, status, *args, **kwargs)
        if status == "completed":
            completed.set()

    monkeypatch.setattr(repository, "mark_voice_profile_job", marked)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "DataRepository", lambda url: repository)
    monkeypatch.setattr(main, "get_transcriber", lambda settings: object())
    monkeypatch.setattr(main, "get_llm_client", lambda settings: OfflineLLM())
    monkeypatch.setattr(
        main,
        "get_docs_client",
        lambda settings: LocalMarkdownProfileClient(profile_dir=tmp_path),
    )
    monkeypatch.setattr(main, "SessionAgent", OfflineAgent)
    monkeypatch.setattr(main, "process_recording_file", process)
    with TestClient(main.create_app()):
        assert completed.wait(8), (
            "Lifespan recovery did not complete its durable queues"
        )
        assert repository.get_voice_session(voice.id).status == "agent_done"
        assert repository.get_voice_profile_jobs() == []
        assert len(summary_calls) == 1 and summary_calls[0][1]["language"] == "en"
        assert len(profile_calls) == 1
        if recover_state == "transcribing":
            assert len(recording_calls) == 1
            assert recording_calls[0]["provider_job_id"] == "already-paid"
        else:
            assert recording_calls == []
    assert not main._session_agent_pending
