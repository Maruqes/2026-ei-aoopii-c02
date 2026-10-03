from __future__ import annotations

import asyncio
import json
import struct
import threading
import wave
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from app.config import Settings
from app.main import create_app, get_repository, get_settings
from app.realtime import RealtimePool
from app.speechmatics_errors import error_kind, key_health, message_error
from app.speechmatics_usage import SpeechmaticsAPIKey
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve


@pytest.fixture(autouse=True)
def reset_health():
    key_health.states.clear()
    yield
    key_health.states.clear()


def keys(n=1):
    return tuple(SpeechmaticsAPIKey(f"key-{i}", f"secret-{i}") for i in range(n))


def test_reservations_fifo_deduplicate_keys_and_hold_silence():
    configured = keys(2)
    pool = RealtimePool((*configured, configured[0]))
    grants = pool.reconcile(1, ["3", "2", "1", "4", "5"], True)
    assert list(grants) == ["3", "2", "1", "4"]
    assert len(pool.keys) == 2
    first = grants["3"]["token"]
    r = pool.acquire(1, "3", first)
    pool.release_epoch(r)
    assert pool.reconcile(1, ["3", "2", "1", "4", "5"], True)["3"]["token"] == first
    pool.acquire(1, "3", first)
    # An epoch draining from a departing holder still consumes provider capacity.
    assert "5" not in pool.reconcile(1, ["2", "1", "4", "5"], True)
    pool.release_epoch(r)
    assert "5" in pool.reconcile(1, ["2", "1", "4", "5"], True)
    assert not pool.reconcile(2, ["6"], True)
    for key in configured:
        assert pool.occupancy(key) <= 2


def test_provider_errors_never_confuse_capacity_or_budget_with_credits():
    assert message_error({"type": "quota_exceeded"}).kind == "capacity"
    assert message_error({"type": "timelimit_exceeded"}).kind == "no_credits"
    assert error_kind(RuntimeError("quota exceeded balance 100%")) == "recoverable"
    assert error_kind(SimpleNamespace(code=4005)) == "capacity"
    assert error_kind(SimpleNamespace(code=4006)) == "no_credits"
    assert (
        error_kind(SimpleNamespace(response=SimpleNamespace(status_code=429)))
        == "capacity"
    )


def session(r):
    return r.create_voice_session(
        guild_id="g",
        voice_channel_id="v",
        channel_name="voice",
        summary_channel_id="text",
        started_at=datetime.now(timezone.utc),
    )


def meta(s, filename="123-test.wav"):
    return {
        "session_id": s.id,
        "discord_id": "123",
        "username": "Alice",
        "channel_name": "voice",
        "recording_filename": filename,
        "recording_started_at": datetime.now(timezone.utc).isoformat(),
    }


def test_final_idempotency_atomic_fallback_and_terminal_discard(repository):
    r = repository
    s = session(r)
    m = meta(s)
    unit, generation = r.start_realtime_unit(
        s.id, m["recording_filename"], m, "participation", "key-0"
    )
    stamp = datetime.now(timezone.utc)
    assert r.insert_realtime_final(unit, generation, "a", "Olá.", stamp)
    assert not r.insert_realtime_final(unit, generation, "a", "Olá.", stamp)
    r.finish_voice_session(s.id, stamp)
    assert not r.claim_session_agent_run(s.id)
    r.finish_realtime_unit(unit, generation, 1, False)
    assert not r.insert_realtime_final(unit, generation, "b", "late", stamp)
    # Closed session duplicate admission must still promote fallback to Batch.
    r.start_recording(
        session_id=s.id,
        recording_filename=m["recording_filename"],
        discord_id="123",
        metadata=m,
    )
    assert r.begin_recording_job(unit)
    from data.repository import MessageInsert

    r.insert_transcription_segments(
        session_id=s.id,
        recording_id=unit,
        discord_id="123",
        username="Alice",
        display_name=None,
        channel_name="voice",
        messages=[MessageInsert("Olá, mundo.", stamp)],
    )
    assert [item["content"] for item in r.get_session_messages(s.id)] == ["Olá, mundo."]
    assert r.claim_session_agent_run(s.id)

    s2 = session(r)
    m2 = meta(s2, "pending.wav")
    unit2, gen2 = r.start_realtime_unit(
        s2.id, m2["recording_filename"], m2, "participation2", "key-0"
    )
    r.insert_realtime_final(unit2, gen2, "a", "Preserve this.", stamp)
    r.discard_session_audio(s2.id)
    assert not r.insert_realtime_final(unit2, gen2, "b", "late", stamp)
    r.mark_recording_failed(unit2, "late worker failure")
    r.finish_voice_session(s2.id, stamp)
    r.retry_session(s2.id)
    assert r.get_session_recording_counts(s2.id) == {"discarded_no_credits": 1}
    assert r.get_session_messages(s2.id)[0]["content"] == "Preserve this."


class ProviderSimulator:
    """Real local WebSocket server; no credentials or paid provider requests."""

    def __init__(self, failure=None):
        self.failure = failure
        self.ready = threading.Event()
        self.received = []
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.run()), daemon=True
        )

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        async with serve(self.handle, "127.0.0.1", 0) as server:
            self.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            self.ready.set()
            await self.stop.wait()

    async def handle(self, ws):
        self.start = json.loads(await ws.recv())
        if self.failure:
            await ws.send(json.dumps({"message": "Error", "type": self.failure}))
            return
        await ws.send(json.dumps({"message": "RecognitionStarted"}))
        async for data in ws:
            if isinstance(data, bytes):
                self.received.append(data)
                result = {
                    "start_time": 0,
                    "end_time": 0.01,
                    "transcript": "Olá, mundo.",
                }
                await ws.send(
                    json.dumps(
                        {
                            "message": "AddPartialTranscript",
                            "metadata": {**result, "transcript": "partial"},
                        }
                    )
                )
                await ws.send(
                    json.dumps({"message": "AddTranscript", "metadata": result})
                )
                await ws.send(
                    json.dumps({"message": "AddTranscript", "metadata": result})
                )
            else:
                assert json.loads(data)["last_seq_no"] == len(self.received)
                await ws.send(json.dumps({"message": "EndOfTranscript"}))
                return

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        return self

    def __exit__(self, *_):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(5)


def client_for(r, directory, url):
    app = create_app()
    settings = Settings(
        database_url=r.database_url,
        transcription_provider="speechmatics",
        speechmatics_api_keys=(("key-0", "secret-0"),),
        recordings_dir=directory,
        speechmatics_realtime_url=url,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_repository] = lambda: r
    return TestClient(app), settings


def test_websocket_pcm_final_flush_and_cleanup(
    repository, tmp_path, caplog, monkeypatch
):
    import time

    from app import main, realtime
    from app.speechmatics_usage import SpeechmaticsKeyUsage, parse_speechmatics_usage

    monkeypatch.setattr(
        main,
        "fetch_speechmatics_key_usages",
        lambda **kwargs: [
            SpeechmaticsKeyUsage(
                keys()[0], parse_speechmatics_usage({"summary": None})
            ),
        ],
    )
    # Advance only the bridge/pool clock; don't alter the WebSocket event-loop clock.
    offset = [0.0]
    monkeypatch.setattr(
        realtime,
        "time",
        SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]),
    )
    caplog.set_level("INFO", logger="uvicorn.error")
    r = repository
    s = session(r)
    m = meta(s)
    with wave.open(str(tmp_path / m["recording_filename"]), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\0" * 960 * 4)
    with ProviderSimulator() as provider:
        client, settings = client_for(r, tmp_path, provider.url)
        assert (
            client.post("/v1/guilds/g/streaming", json={"mode": "on"}).status_code
            == 200
        )
        roster = client.post(
            f"/v1/sessions/{s.id}/streaming", json={"users": ["123", "456", "789"]}
        ).json()
        assert roster["queued"] == 1
        m["token"] = roster["assignments"]["123"]["token"]
        with client.websocket_connect("/v1/streaming/audio") as ws:
            ws.send_json(m)
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(struct.pack("<Q", 1) + b"\0" * 480 * 2)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                usage = client.get("/v1/speechmatics/keys").json()["keys"][0]
                if usage["realtime_hours"] > 0:
                    break
                time.sleep(0.01)
            assert usage["realtime_hours"] == pytest.approx(0.01 / 3600)
            assert usage["estimated_cost_usd"] == pytest.approx(0.01 / 3600 * 0.8)
            assert r.get_session_recording_counts(s.id) == {"streaming": 1}
            offset[0] = 5.1
            ws.send_bytes(struct.pack("<Q", 2) + b"\0" * 480 * 2)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                usage = client.get("/v1/speechmatics/keys").json()["keys"][0]
                if usage["realtime_hours"] >= 0.02 / 3600:
                    break
                time.sleep(0.01)
            assert usage["realtime_hours"] == pytest.approx(0.02 / 3600)
            assert usage["estimated_cost_usd"] == pytest.approx(0.02 / 3600 * 0.8)
            ws.send_json({"type": "end", "last_seq_no": 2})
            activity = ws.receive_json()
            assert activity == {
                "type": "speech",
                "session_id": s.id,
                "discord_id": "123",
                "recording_id": activity["recording_id"],
                "generation": activity["generation"],
                "start": 0,
                "end": 0.01,
            }
            final = ws.receive_json()
            assert final["type"] == "final"
            assert final["session_id"] == s.id and final["discord_id"] == "123"
            assert final["recording_id"] > 0 and final["generation"] >= 0
            assert final["identity"] and final["start"] == 0 and final["end"] == 0.01
            assert final["text"] == "Olá, mundo."
            # Partials only convey timing; duplicate final text never reaches the bot.
            assert len(repository.get_session_messages(s.id)) == 1
            assert ws.receive_json() == activity
            assert ws.receive_json()["type"] == "completed"
        assert provider.start["transcription_config"] == {
            "language": "pt",
            "model": "enhanced",
            "enable_partials": True,
        }
        assert len(provider.received) == 2
        assert "Olá, mundo." not in caplog.text
        assert "secret-0" not in caplog.text
        assert [item["content"] for item in r.get_session_messages(s.id)] == [
            "Olá, mundo."
        ]
        assert r.get_session_recording_counts(s.id) == {"completed": 1}
        # Completing a previously checkpointed unit must not count its audio twice.
        assert client.get("/v1/speechmatics/keys").json()["keys"][0][
            "realtime_hours"
        ] == pytest.approx(0.02 / 3600)
        assert not (tmp_path / m["recording_filename"]).exists()
        assert (
            client.post("/v1/guilds/g/streaming", json={"mode": "off"}).json()[
                "enabled"
            ]
            is False
        )
        assert r.streaming_preference("g", True) is False


def test_realtime_usage_checkpoints_are_monotonic_and_period_scoped(repository):
    from data.repository import connect

    r = repository
    s = session(r)
    m = meta(s)
    m["speechmatics_realtime_model"] = "standard"
    unit, generation = r.start_realtime_unit(
        s.id, m["recording_filename"], m, "t", "key-0"
    )
    r.checkpoint_realtime_usage(unit, generation, 120)
    r.checkpoint_realtime_usage(unit, generation, 60)
    r.checkpoint_realtime_usage(unit, generation + 1, 999)
    today = datetime.now(timezone.utc).date().isoformat()
    usage = r.local_realtime_usage(since=today)
    assert usage == [
        {"key_name": "key-0", "model": "standard", "used_hours": 120 / 3600}
    ]
    r.finish_realtime_unit(unit, generation, 100, False)
    r.checkpoint_realtime_usage(unit, generation, 999)
    assert r.local_realtime_usage(since=today) == usage
    with connect(r.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE voice_recordings SET created_at = NOW() - INTERVAL '2 days' WHERE id = %s",
            (unit,),
        )
    assert r.local_realtime_usage(since=today) == []


@pytest.mark.parametrize(
    "failure,discard",
    [
        ("quota_exceeded", False),
        ("timelimit_exceeded", True),
        ("not_authorised", False),
    ],
)
def test_provider_failure_preserves_audio_unless_confirmed_credit_exhaustion(
    repository, tmp_path, failure, discard
):
    r = repository
    s = session(r)
    with ProviderSimulator(failure) as provider:
        client, _ = client_for(r, tmp_path, provider.url)
        client.post("/v1/guilds/g/streaming", json={"mode": "on"})
        roster = client.post(
            f"/v1/sessions/{s.id}/streaming", json={"users": ["123"]}
        ).json()
        m = meta(s)
        m["token"] = roster["assignments"]["123"]["token"]
        with client.websocket_connect("/v1/streaming/audio") as ws:
            ws.send_json(m)
            assert ws.receive_json()["type"] == "fallback"
        assert r.session_credit_state(s.id)["exhausted"] is discard


def test_configuration_default_and_command_override(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_STREAMING_ENABLED", raising=False)
    assert not Settings.from_env().transcription_streaming_enabled
    monkeypatch.setenv("TRANSCRIPTION_STREAMING_ENABLED", "true")
    assert Settings.from_env().transcription_streaming_enabled
    assert Settings.from_env().speechmatics_realtime_language == "pt"


def test_api_restart_repairs_stable_wav_without_duplicate_realtime_text(
    repository, tmp_path
):
    import os
    import time

    from app.recording_cleanup import recover_realtime_wavs

    r = repository
    s = session(r)
    m = meta(s)
    path = tmp_path / m["recording_filename"]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\0" * 480 * 4)
    # Simulate an unfinalized Go WAV header from process death.
    with path.open("r+b") as wav:
        wav.seek(40)
        wav.write(struct.pack("<I", 0))
    unit, generation = r.start_realtime_unit(s.id, path.name, m, "token", "key-0")
    r.insert_realtime_final(
        unit, generation, "a", "provisional", datetime.now(timezone.utc)
    )
    r.recover_realtime_units()
    assert not r.insert_realtime_final(
        unit, generation, "late", "late", datetime.now(timezone.utc)
    )
    recover_realtime_wavs(r, tmp_path)
    assert r.get_session_recording_counts(s.id) == {"fallback_pending": 1}
    os.utime(path, (time.time() - 20, time.time() - 20))
    recover_realtime_wavs(r, tmp_path)
    assert r.get_session_recording_counts(s.id) == {"pending": 1}
    with wave.open(str(path), "rb") as wav:
        assert wav.getnframes() == 480
    assert len(r.get_session_messages(s.id)) == 1


def test_forget_invalidates_finals_and_pending_batch_callbacks(repository, tmp_path):
    from data.repository import MessageInsert

    r = repository
    s = session(r)
    m = meta(s)
    unit, generation = r.start_realtime_unit(
        s.id, m["recording_filename"], m, "token", "key-0"
    )
    stamp = datetime.now(timezone.utc)
    r.insert_realtime_final(unit, generation, "a", "Erase me.", stamp)
    r.invalidate_user_recordings("123")
    assert not r.recording_is_live(unit)
    assert not r.insert_realtime_final(unit, generation, "b", "late", stamp)
    assert (
        r.insert_transcription_segments(
            session_id=s.id,
            recording_id=unit,
            discord_id="123",
            username="Alice",
            display_name=None,
            channel_name="voice",
            messages=[MessageInsert("late Batch", stamp)],
        ).message_ids
        == []
    )
    with pytest.raises(ValueError, match="cancelled"):
        r.save_provider_job(unit, "late-job", "key-0")
    r.delete_user_by_discord_id("123")
    assert not r.insert_realtime_final(unit, generation, "c", "late", stamp)
    with pytest.raises(ValueError, match="removed"):
        r.insert_transcription_segments(
            session_id=s.id,
            recording_id=unit,
            discord_id="123",
            username="Alice",
            display_name=None,
            channel_name="voice",
            messages=[MessageInsert("late Batch", stamp)],
        )
    assert r.get_session_messages(s.id) == []


def test_discard_cleanup_retries_and_notice_episode_does_not_reopen_audio(
    repository, tmp_path, monkeypatch
):
    from app import recording_cleanup

    r = repository
    s = session(r)
    m = meta(s)
    unit = r.start_recording(
        session_id=s.id,
        recording_filename=m["recording_filename"],
        discord_id="123",
        metadata=m,
    )
    path = tmp_path / m["recording_filename"]
    path.write_bytes(b"audio")
    (tmp_path / (path.name + ".request.json")).write_text("outbox")
    r.discard_session_audio(s.id)
    cleanup = recording_cleanup.RecordingCleanup(
        repository=r,
        settings=Settings(database_url=r.database_url, recordings_dir=tmp_path),
    )
    original = recording_cleanup.remove_recording_files

    def fail(*args, **kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr(recording_cleanup, "remove_recording_files", fail)
    cleanup.sweep()
    assert r.session_credit_state(s.id)["cleanup_pending"]
    assert not r.begin_recording_job(unit)
    monkeypatch.setattr(recording_cleanup, "remove_recording_files", original)
    cleanup.sweep()
    assert not path.exists()
    assert not r.session_credit_state(s.id)["cleanup_pending"]
    state = r.session_credit_state(s.id)
    assert state["episode"] == 1
    r.acknowledge_credit_notice(
        s.id, 0
    )  # A stale notice cannot acknowledge the new episode.
    assert not r.session_credit_state(s.id)["notice_sent"]
    r.acknowledge_credit_notice(s.id, 1)
    assert r.session_credit_state(s.id)["notice_sent"]
    r.reset_session_credits(s.id)
    assert r.get_session_recording_counts(s.id) == {"discarded_no_credits": 1}
    r.discard_session_audio(s.id)
    assert r.session_credit_state(s.id)["episode"] == 2


def test_pool_retries_another_key_and_enforces_concurrent_atomic_admission(monkeypatch):
    from app import realtime

    pool = RealtimePool(keys(2))
    grants = pool.reconcile(1, ["123"], True)
    reservation = pool.acquire(1, "123", grants["123"]["token"])
    attempts = []

    class Upstream:
        async def send(self, packet):
            pass

        async def recv(self):
            return (
                json.dumps({"message": "Error", "type": "timelimit_exceeded"})
                if len(attempts) == 1
                else json.dumps({"message": "RecognitionStarted"})
            )

        async def close(self):
            pass

    async def connect(*args, **kwargs):
        attempts.append(kwargs["additional_headers"]["Authorization"])
        return Upstream()

    monkeypatch.setattr(realtime, "connect", connect)
    assert asyncio.run(
        realtime.open_provider(Settings(database_url="unused"), pool, reservation)
    )
    assert len(attempts) == 2
    assert reservation.key == keys(2)[1]
    for key in pool.keys:
        assert pool.occupancy(key) <= 2
    with pytest.raises(Exception, match="unreserved"):
        pool.acquire(1, "123", reservation.token)


def test_batch_sdk_wrapped_credit_errors_and_poll_cancellation(tmp_path):
    from app.transcriber import SpeechmaticsTranscriber
    from speechmatics.batch import Transcript
    from speechmatics.batch._exceptions import BatchError, TransportError

    transport = TransportError("provider body must not be logged", status_code=402)
    wrapper = BatchError("wrapped")
    wrapper.__cause__ = transport
    assert error_kind(wrapper) == "no_credits"
    cancelled = []
    active = [True]

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def submit_job(self, *args, **kwargs):
            return SimpleNamespace(id="remote")

        async def wait_for_completion(self, *args, **kwargs):
            active[0] = False
            await asyncio.Event().wait()
            return Transcript(results=[])

        async def delete_job(self, job_id, force):
            cancelled.append((job_id, force))

    transcriber = SpeechmaticsTranscriber("", api_keys=keys(), client_factory=Client)
    with pytest.raises(Exception, match="cancelled"):
        transcriber.transcribe_recording(
            tmp_path / "audio.wav",
            job_id=None,
            key_name=None,
            save_job=lambda *args: None,
            is_active=lambda: active[0],
        )
    assert cancelled == [("remote", True)]


def test_batch_credit_failure_tries_other_key_without_generic_error_discard(
    tmp_path, monkeypatch
):
    from app.transcriber import SpeechmaticsTranscriber, TranscriptionResult
    from speechmatics.batch._exceptions import BatchError, TransportError

    transcriber = SpeechmaticsTranscriber("", api_keys=keys(2))
    monkeypatch.setattr(transcriber, "_select_api_key", lambda: keys(2)[0])
    attempts = []

    async def transcribe(audio_path, selected, **kwargs):
        attempts.append(selected.name)
        if len(attempts) == 1:
            raise BatchError("wrapped") from TransportError(
                "payment required", status_code=402
            )
        return TranscriptionResult(text="", segments=[])

    monkeypatch.setattr(transcriber, "_transcribe", transcribe)
    assert (
        transcriber.transcribe_recording(
            tmp_path / "audio.wav",
            job_id=None,
            key_name=None,
            save_job=lambda *args: None,
        ).text
        == ""
    )
    assert attempts == ["key-0", "key-1"]
    assert not key_health.exhausted(keys(2))


def test_debug_only_outputs_transcript_when_explicitly_enabled(
    repository, tmp_path, caplog
):
    from app import realtime

    s = session(repository)
    m = meta(s)
    with wave.open(str(tmp_path / m["recording_filename"]), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\0" * 480 * 4)
    with ProviderSimulator() as provider:
        client, settings = client_for(repository, tmp_path, provider.url)
        client.app.dependency_overrides[get_settings] = lambda: replace(
            settings, transcription_streaming_debug=True
        )
        caplog.set_level("INFO", logger=realtime.logger.name)
        client.post("/v1/guilds/g/streaming", json={"mode": "on"})
        roster = client.post(
            f"/v1/sessions/{s.id}/streaming", json={"users": ["123"]}
        ).json()
        m["token"] = roster["assignments"]["123"]["token"]
        with client.websocket_connect("/v1/streaming/audio") as ws:
            ws.send_json(m)
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(struct.pack("<Q", 1) + b"\0" * 480 * 2)
            ws.send_json({"type": "end", "last_seq_no": 1})
            activity = ws.receive_json()
            assert activity["type"] == "speech"
            assert "text" not in activity and "words" not in activity
            final = ws.receive_json()
            assert final["type"] == "final"
            assert final["session_id"] == s.id and final["discord_id"] == "123"
            assert final["recording_id"] > 0 and final["generation"] >= 0
            assert final["identity"] and final["start"] == 0 and final["end"] == 0.01
            assert final["text"] == "Olá, mundo."
            # Partials only convey timing; duplicate final text never reaches the bot.
            assert len(repository.get_session_messages(s.id)) == 1
            assert ws.receive_json()["type"] == "completed"
    assert "partial" in caplog.text and "Olá, mundo." in caplog.text
    assert "secret-0" not in caplog.text


def test_final_words_preserves_formatted_entities():
    from app.realtime import final_words

    assert final_words(
        [
            {
                "type": "word",
                "start_time": 0,
                "end_time": 1,
                "alternatives": [{"content": "Quanto"}],
            },
            {
                "type": "entity",
                "start_time": 1,
                "end_time": 2,
                "alternatives": [{"content": "1.500,50 euros"}],
            },
            {"type": "punctuation", "alternatives": [{"content": "?"}]},
        ]
    ) == [
        {"text": "Quanto", "start": 0, "end": 1},
        {"text": "1.500,50 euros?", "start": 1, "end": 2},
    ]
