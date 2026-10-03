from __future__ import annotations

import asyncio
import io
import json
import struct
import time
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from app.config import Settings
from app.deepgram import DeepgramTranscriber, normalize_event, params
from app.main import create_app, get_repository, get_settings, process_recording_file
from app.provider_routes import deepgram_management
from app.providers import ProviderRegistry, effective_order, parse_order, registry_for
from app.realtime import RealtimePool, open_provider
from app.recording_cleanup import remove_recording_files
from app.speechmatics_errors import ProviderError, key_health
from app.transcriber import TranscriptionResult, TranscriptionSegment
from app.transcription_router import TranscriptionRouter
from fastapi.testclient import TestClient
from test_streaming import ProviderSimulator, meta, session


@pytest.fixture(autouse=True)
def clear_registry():
    registry_for.cache_clear()
    key_health.states.clear()
    yield
    registry_for.cache_clear()
    key_health.states.clear()


def settings(**kwargs):
    return replace(
        Settings(
            database_url="unused",
            transcription_provider_order=("deepgram", "speechmatics"),
            deepgram_api_keys=(("dg-1", "dg-secret-1"), ("dg-2", "dg-secret-2")),
            deepgram_key_groups=(("dg-1", "project", 2, 1), ("dg-2", "project", 2, 1)),
            speechmatics_api_keys=(("sm-1", "sm-secret-1"),),
        ),
        **kwargs,
    )


def wav(path, frames=480):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(struct.pack("<hh", 100, 300) * frames)


def test_order_legacy_normalization_keys_and_shared_quota(monkeypatch):
    assert parse_order(" Speechmatics , DEEPGRAM ") == ("speechmatics", "deepgram")
    for invalid in ("", "deepgram,deepgram", "whisper", "deepgram,unknown"):
        with pytest.raises(ValueError):
            parse_order(invalid)
    monkeypatch.delenv("TRANSCRIPTION_PROVIDER_ORDER", raising=False)
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "speechmatics")
    assert effective_order(Settings.from_env())[0] == ("speechmatics",)
    monkeypatch.setenv("DEEPGRAM_API_KEY_10", "ten")
    monkeypatch.setenv("DEEPGRAM_API_KEY_02", "two")
    names = [name for name, _ in Settings.from_env().deepgram_api_keys]
    assert names.index("DEEPGRAM_API_KEY_02") < names.index("DEEPGRAM_API_KEY_10")
    s = settings(
        deepgram_api_keys=(
            ("dg-1", "same"),
            ("duplicate", "same"),
            ("dg-2", "different"),
        )
    )
    registry = ProviderRegistry(s)
    assert len(registry.for_provider("deepgram")) == 2
    pool = RealtimePool((), registry=registry)
    grants = pool.reconcile(1, ["1", "2", "3"], True, ("deepgram", "speechmatics"))
    assert [grant["provider"] for grant in grants.values()] == [
        "deepgram",
        "deepgram",
        "speechmatics",
    ]
    assert registry.reserve("batch-1", ("deepgram",), "batch")
    assert registry.reserve("batch-2", ("deepgram",), "batch") is None
    registry.mark(registry.keys[0], "streaming", "capacity")
    assert all(
        registry.state(k, "streaming") == "cooldown"
        for k in registry.for_provider("deepgram")
    )
    assert registry.state(registry.keys[0], "batch") == "healthy"
    registry.release("batch-1")
    registry.release("batch-1")


def test_reconciled_projects_merge_limits_and_unknown_global_ceiling():
    s = settings(
        deepgram_streaming_limit=2,
        deepgram_key_groups=(("dg-1", "a", 3, 2), ("dg-2", "b", 1, 1)),
    )
    registry = ProviderRegistry(s)
    first, second = registry.for_provider("deepgram")
    assert registry.reserve("one", ("deepgram",), "streaming")
    assert registry.reserve("two", ("deepgram",), "streaming")
    assert registry.reserve("three", ("deepgram",), "streaming") is None
    registry.reconcile_project(first, "same")
    registry.reconcile_project(second, "same")
    assert registry.limits(first, "streaming") == 1
    assert registry.occupancy(first, "streaming") == 2
    assert registry.reserve("three", ("deepgram",), "streaming") is None


@pytest.mark.parametrize(
    "order", [("deepgram", "speechmatics"), ("speechmatics", "deepgram")]
)
def test_streaming_handshake_fallback_in_both_directions(monkeypatch, order):
    from app import realtime

    s = settings(transcription_provider_order=order)
    pool = RealtimePool((), registry=ProviderRegistry(s))
    grant = pool.reconcile(1, ["user"], True, order)["user"]
    reservation = pool.acquire(1, "user", grant["token"])
    attempts = []

    class Upstream:
        async def send(self, data):
            pass

        async def recv(self):
            return json.dumps({"message": "RecognitionStarted"})

        async def close(self):
            pass

    async def connect(url, **kwargs):
        provider = "deepgram" if "deepgram" in url else "speechmatics"
        attempts.append(provider)
        if provider == order[0]:
            raise ProviderError("capacity")
        return Upstream()

    monkeypatch.setattr(realtime, "connect", connect)
    assert asyncio.run(open_provider(s, pool, reservation))
    assert attempts == list(order)
    assert reservation.key.provider == order[1]
    reservation.retiring = True
    pool.release_epoch(reservation)
    assert not pool.registry.leases


@pytest.mark.parametrize(
    "order", [("deepgram", "speechmatics"), ("speechmatics", "deepgram")]
)
def test_batch_fallback_result_sidecar_and_restart(tmp_path, monkeypatch, order):
    path = tmp_path / "audio.wav"
    wav(path)
    router = TranscriptionRouter(settings(transcription_provider_order=order))
    attempts = []

    def run(provider):
        attempts.append(provider)
        if provider == order[0]:
            raise ProviderError("capacity")
        return TranscriptionResult(
            "Olá.", [TranscriptionSegment(0, 0.01, "Olá.")], duration_seconds=0.01
        )

    monkeypatch.setattr(router.deepgram, "transcribe", lambda *a, **kw: run("deepgram"))

    class SM:
        async def _transcribe(self, *args, **kwargs):
            return run("speechmatics")

    monkeypatch.setattr(router, "speechmatics", lambda key: SM())
    result = router.transcribe(path)
    assert result.provider == order[1]
    assert attempts == list(order)
    assert not router.registry.leases
    if result.provider == "deepgram":
        restarted = TranscriptionRouter(router.settings)
        monkeypatch.setattr(
            restarted.deepgram,
            "transcribe",
            lambda *a, **k: pytest.fail("repeated billed request"),
        )
        assert restarted.transcribe(path) == result
    remove_recording_files(tmp_path, path.name, include_request=True)
    assert not list(tmp_path.iterdir())


def test_credits_and_mixed_failure_never_discard_or_disable_whole_key(
    tmp_path, monkeypatch
):
    registry = ProviderRegistry(settings())
    dg = registry.for_provider("deepgram")[0]
    registry.mark(dg, "streaming", "invalid_key")
    assert registry.state(dg, "batch") == "healthy"
    registry.mark(dg, "batch", "no_credits")
    assert not registry.exhausted(("deepgram", "speechmatics"))
    sm = registry.for_provider("speechmatics")[0]
    registry.mark(sm, "batch", "recoverable")
    assert not registry.exhausted(("deepgram", "speechmatics"))
    registry.mark(sm, "batch", "no_credits")
    assert registry.exhausted(("deepgram", "speechmatics"))


def test_rest_mono_chunks_keyterms_validation_and_silence(tmp_path):
    path = tmp_path / "audio.wav"
    wav(path)
    seen = []

    def handle(request):
        assert request.headers["Authorization"] == "Token secret"
        assert request.url.params.get_list("keyterm") == ["Olá macaco", "Discord"]
        assert request.headers["Content-Type"] == "audio/wav"
        with wave.open(io.BytesIO(request.read()), "rb") as audio:
            assert audio.getnchannels() == 1 and audio.getnframes() == 480
            assert struct.unpack("<h", audio.readframes(1))[0] == 200
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "metadata": {"request_id": "request"},
                "results": {
                    "channels": [
                        {
                            "alternatives": [
                                {
                                    "transcript": "Olá.",
                                    "words": [
                                        {
                                            "word": "olá",
                                            "punctuated_word": "Olá.",
                                            "start": 0,
                                            "end": 0.01,
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
            },
        )

    s = settings(deepgram_keyterms=("Olá macaco", "Discord"))
    transcriber = DeepgramTranscriber(
        s,
        client_factory=lambda **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handle), **kw
        ),
    )
    result = transcriber.transcribe(
        path, SimpleNamespace(value="secret", name="dg", group="project")
    )
    assert result.text == "Olá." and result.request_id == "request"
    assert path.exists() and not Path(str(path) + ".deepgram-mono.wav").exists()
    assert len(seen) == 1
    assert (
        transcriber.normalize(
            {
                "results": {
                    "channels": [{"alternatives": [{"transcript": "", "words": []}]}]
                }
            },
            1,
        ).segments
        == []
    )
    assert ("keyterm", "Discord") in params(s, streaming=True)
    event = normalize_event(
        {
            "type": "Results",
            "start": 0,
            "duration": 0.01,
            "is_final": False,
            "speech_final": True,
            "channel": {"alternatives": [{"transcript": "partial"}]},
        }
    )
    assert event["message"] == "AddPartialTranscript"


def test_management_forbidden_does_not_disable_inference(monkeypatch):
    s = settings()
    registry = ProviderRegistry(s)
    from app import provider_routes

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    monkeypatch.setattr(provider_routes.httpx, "Client", lambda **kw: client)
    data = deepgram_management(s, registry)
    assert data["project"]["balance_usd"] is None
    assert data["project"]["balance_error"] == "forbidden"
    assert data["project"]["reported_hours"] is None
    assert registry.reserve("stream", ("deepgram",), "streaming")


def test_rest_cancellation_and_total_deadline_clean_derived_audio(tmp_path):
    path = tmp_path / "cancel.wav"
    wav(path)
    active = [True]
    cancelled = []

    async def hang(request):
        active[0] = False
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    transcriber = DeepgramTranscriber(
        settings(),
        client_factory=lambda **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(hang), **kw
        ),
    )
    with pytest.raises(ProviderError, match="cancelled"):
        transcriber.transcribe(
            path,
            SimpleNamespace(value="secret", name="dg", group="project"),
            is_active=lambda: active[0],
        )
    assert cancelled and path.exists()
    assert not Path(str(path) + ".deepgram-mono.wav").exists()
    transcriber.settings = replace(
        transcriber.settings, speechmatics_timeout_seconds=0.05
    )
    with pytest.raises(TimeoutError):
        transcriber.transcribe(
            path, SimpleNamespace(value="secret", name="dg", group="project")
        )
    assert not Path(str(path) + ".deepgram-mono.wav").exists()


class DGSimulator(ProviderSimulator):
    async def handle(self, ws):
        self.controls = []
        self.authorization = ws.request.headers["Authorization"]
        async for data in ws:
            if isinstance(data, bytes):
                self.received.append(data)
                event = {
                    "type": "Results",
                    "start": 0,
                    "duration": 0.01,
                    "is_final": True,
                    "channel": {
                        "alternatives": [
                            {
                                "transcript": "Olá.",
                                "words": [
                                    {
                                        "word": "olá",
                                        "punctuated_word": "Olá.",
                                        "start": 0,
                                        "end": 0.01,
                                    }
                                ],
                            }
                        ]
                    },
                }
                await ws.send(
                    json.dumps({**event, "is_final": False, "speech_final": True})
                )
                await ws.send(json.dumps(event))
                await ws.send(json.dumps(event))
                if self.failure:
                    await ws.close(1011)
                    return
            else:
                self.controls.append(json.loads(data)["type"])
                if self.controls[-1] == "CloseStream":
                    if not getattr(self, "incomplete", False):
                        await ws.send(
                            json.dumps({"type": "Metadata", "request_id": "stream"})
                        )
                    return


def api(repository, directory, dg_url):
    app = create_app()
    s = settings(
        database_url=repository.database_url,
        recordings_dir=directory,
        deepgram_realtime_url=dg_url,
        deepgram_api_base_url="http://127.0.0.1/v1",
    )
    app.dependency_overrides[get_settings] = lambda: s
    app.dependency_overrides[get_repository] = lambda: repository
    return TestClient(app), s


def test_deepgram_stream_final_flush_idempotency_usage_and_cleanup(
    repository, tmp_path
):
    call = session(repository)
    m = meta(call)
    wav(tmp_path / m["recording_filename"])
    with DGSimulator() as dg:
        client, s = api(repository, tmp_path, dg.url)
        assert (
            client.post("/v1/guilds/g/streaming", json={"mode": "on"}).status_code
            == 200
        )
        grants = client.post(
            f"/v1/sessions/{call.id}/streaming", json={"users": ["123"]}
        ).json()
        m["token"] = grants["assignments"]["123"]["token"]
        with client.websocket_connect("/v1/streaming/audio") as ws:
            ws.send_json(m)
            ready = ws.receive_json()
            assert ready["type"] == "ready" and ready["provider"] == "deepgram"
            ws.send_bytes(struct.pack("<Q", 1) + b"\0" * 960)
            ws.send_json({"type": "end", "last_seq_no": 1})
            assert ws.receive_json()["type"] == "speech"
            final = ws.receive_json()
            assert final["type"] == "final" and final["words"][0]["text"] == "Olá."
            assert ws.receive_json()["type"] == "completed"
        assert len(repository.get_session_messages(call.id)) == 1
        assert not (tmp_path / m["recording_filename"]).exists()
        assert dg.authorization.startswith("Token ")
        usage = repository.local_provider_usage("2026-01-01")
        assert usage[0]["provider"] == "deepgram" and usage[0]["used_hours"] > 0


def test_failed_stream_recovers_wav_without_replaying_finals(
    repository, tmp_path, monkeypatch
):
    call = session(repository)
    m = meta(call)
    path = tmp_path / m["recording_filename"]
    wav(path)
    with DGSimulator(failure=True) as dg:
        client, s = api(repository, tmp_path, dg.url)
        client.post("/v1/guilds/g/streaming", json={"mode": "on"})
        grants = client.post(
            f"/v1/sessions/{call.id}/streaming", json={"users": ["123"]}
        ).json()
        m["token"] = grants["assignments"]["123"]["token"]
        with client.websocket_connect("/v1/streaming/audio") as ws:
            ws.send_json(m)
            ready = ws.receive_json()
            ws.send_bytes(struct.pack("<Q", 1) + b"\0" * 960)
            events = [ws.receive_json(), ws.receive_json(), ws.receive_json()]
            assert [e["type"] for e in events] == ["speech", "final", "fallback"]
        assert path.exists()
        assert not repository.insert_realtime_final(
            ready["recording_id"], ready["generation"], "late", "late", call.started_at
        )
        repository.admit_realtime_recovery(ready["recording_id"])
        router = TranscriptionRouter(s)

        class SM:
            async def _transcribe(self, *a, **kw):
                return TranscriptionResult(
                    "Recovered.", [TranscriptionSegment(0, 0.01, "Recovered.")]
                )

        monkeypatch.setattr(router, "speechmatics", lambda key: SM())
        process_recording_file(
            recording_path=path,
            session_id=call.id,
            recording_id=ready["recording_id"],
            discord_id="123",
            username="Alice",
            display_name=None,
            channel_name="voice",
            recording_started_at=call.started_at,
            settings=s,
            repository=repository,
            transcriber=router,
        )
        messages = repository.get_session_messages(call.id)
        assert len(messages) == 1 and messages[0]["content"] == "Recovered."
        next_grants = client.post(
            f"/v1/sessions/{call.id}/streaming", json={"users": ["123"]}
        ).json()
        assert next_grants["assignments"]["123"]["provider"] == "speechmatics"
        assert not next_grants["exhausted"]


def test_preferences_persist_validate_before_write_and_apply_at_boundary(
    repository, tmp_path
):
    with DGSimulator() as dg:
        client, s = api(repository, tmp_path, dg.url)
        assert (
            client.post(
                "/v1/guilds/g/transcription",
                json={"providers": "speechmatics,deepgram"},
            ).json()["source"]
            == "guild"
        )
        fresh, _ = api(repository, tmp_path, dg.url)
        assert fresh.get("/v1/guilds/g/transcription").json()["order"] == [
            "speechmatics",
            "deepgram",
        ]
        assert (
            fresh.post(
                "/v1/guilds/g/transcription", json={"providers": "deepgram,deepgram"}
            ).status_code
            == 422
        )
        pool = RealtimePool((), registry=registry_for(s))
        grant = pool.reconcile(1, ["user"], True, ("deepgram",))["user"]
        r = pool.acquire(1, "user", grant["token"])
        pool.reconcile(1, ["user"], True, ("speechmatics",))
        assert r.key.provider == "deepgram"
        pool.release_epoch(r)
        assert r.key.provider == "speechmatics"


def test_keepalive_and_incomplete_close_preserve_wav(repository, tmp_path):
    call = session(repository)
    m = meta(call)
    path = tmp_path / m["recording_filename"]
    wav(path, frames=0)
    with DGSimulator() as dg:
        dg.incomplete = True
        client, s = api(repository, tmp_path, dg.url)
        client.post("/v1/guilds/g/streaming", json={"mode": "on"})
        grants = client.post(
            f"/v1/sessions/{call.id}/streaming", json={"users": ["123"]}
        ).json()
        m["token"] = grants["assignments"]["123"]["token"]
        with client.websocket_connect("/v1/streaming/audio") as ws:
            ws.send_json(m)
            assert ws.receive_json()["type"] == "ready"
            time.sleep(5.2)
            ws.send_json({"type": "end", "last_seq_no": 0})
            assert ws.receive_json()["type"] == "fallback"
        assert "KeepAlive" in dg.controls
        assert path.exists()
        assert repository.get_session_recording_counts(call.id) == {
            "fallback_pending": 1
        }
        assert all(
            row["used_hours"] == 0
            for row in repository.local_provider_usage("2026-01-01")
        )


def test_management_uses_authorized_key_and_queries_shared_project_once(monkeypatch):
    from app import provider_routes

    s = settings()
    registry = ProviderRegistry(s)
    requests = []

    def handle(request):
        requests.append(request)
        if request.headers["Authorization"] == "Token dg-secret-1":
            return httpx.Response(403)
        if request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"projects": [{"project_id": "project"}]})
        if request.url.path.endswith("/balances"):
            return httpx.Response(
                200, json={"balances": [{"amount": 123, "units": "USD"}]}
            )
        return httpx.Response(200, json={"results": [{"hours": 1.5}]})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(provider_routes.httpx, "Client", lambda **kwargs: client)
    rows = deepgram_management(s, registry)
    assert rows["project"]["balance_usd"] == 123
    assert rows["project"]["balance_error"] is None
    assert rows["project"]["reported_hours"] == 1.5
    count = len(requests)
    assert deepgram_management(s, registry) == rows
    assert len(requests) == count
    assert all(registry.state(k, "batch") == "healthy" for k in registry.keys)


def test_balance_permission_error_survives_successful_usage_lookup(monkeypatch):
    from app import provider_routes

    def handle(request):
        if request.url.path.endswith("/balances"):
            return httpx.Response(403)
        if request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"projects": [{"project_id": "project"}]})
        return httpx.Response(200, json={"results": [{"hours": 1.5}]})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(provider_routes.httpx, "Client", lambda **kwargs: client)
    registry = ProviderRegistry(settings())
    row = deepgram_management(settings(), registry)["project"]
    assert row["balance_usd"] is None
    assert row["balance_error"] == "forbidden"
    assert row["reported_hours"] == 1.5
    assert registry.reserve("stream", ("deepgram",), "streaming")


def test_keys_aggregates_even_when_management_is_offline(
    repository, tmp_path, monkeypatch
):
    from app import main

    monkeypatch.setattr(main, "fetch_speechmatics_key_usages", lambda **kwargs: [])
    with DGSimulator() as dg:
        client, s = api(repository, tmp_path, dg.url)
        registry_for(s).management_cache["deepgram"] = (time.monotonic(), {})
        result = client.get("/v1/transcription/keys?guild_id=g")
        assert result.status_code == 200
        payload = result.json()
        assert [p["provider"] for p in payload["providers"]] == [
            "deepgram",
            "speechmatics",
        ]
        assert len(payload["providers"][0]["groups"]) == 1
        assert payload["providers"][1]["configured"]
        assert "dg-secret" not in result.text and "sm-secret" not in result.text


def test_pending_success_blocks_credit_discard_and_restart_reuses_result(
    repository, tmp_path, monkeypatch
):
    from app.deepgram import save_result

    call = session(repository)
    m = meta(call)
    path = tmp_path / m["recording_filename"]
    wav(path)
    unit = repository.start_recording(
        session_id=call.id,
        recording_filename=path.name,
        discord_id=m["discord_id"],
        metadata=m,
    )
    repository.begin_recording_job(unit)
    attempt = repository.start_transcription_attempt(
        unit, "batch", "deepgram", "dg-1", "project", "nova-3"
    )
    repository.checkpoint_transcription_attempt(
        attempt, 0.01, "completed", remote_id="receipt"
    )
    assert repository.session_has_remote_jobs(call.id)
    result = TranscriptionResult(
        "Durable.",
        [TranscriptionSegment(0, 0.01, "Durable.")],
        duration_seconds=0.01,
        provider="deepgram",
        model="nova-3",
        key_name="dg-1",
        group="project",
        request_id="receipt",
    )
    save_result(Path(str(path) + ".deepgram.json"), result)
    router = TranscriptionRouter(
        settings(database_url=repository.database_url, recordings_dir=tmp_path)
    )
    monkeypatch.setattr(
        router.deepgram,
        "transcribe",
        lambda *a, **kw: pytest.fail("repeated billed request"),
    )
    process_recording_file(
        recording_path=path,
        session_id=call.id,
        recording_id=unit,
        discord_id="123",
        username="Alice",
        display_name=None,
        channel_name="voice",
        recording_started_at=call.started_at,
        settings=router.settings,
        repository=repository,
        transcriber=router,
    )
    assert repository.get_session_recording_counts(call.id) == {"completed": 1}
    assert not repository.session_has_remote_jobs(call.id)
    assert not list(tmp_path.iterdir())


def test_existing_speechmatics_job_keeps_identity_model_and_monotonic_usage(
    repository, tmp_path, monkeypatch
):
    call = session(repository)
    m = meta(call)
    path = tmp_path / m["recording_filename"]
    wav(path)
    unit = repository.start_recording(
        session_id=call.id, recording_filename=path.name, discord_id="123", metadata=m
    )
    repository.save_provider_job(unit, "original-job", "sm-1")
    attempt = repository.start_transcription_attempt(
        unit, "batch", "speechmatics", "sm-1", "sm-1", "enhanced"
    )
    repository.checkpoint_transcription_attempt(
        attempt, 0.01, "interrupted", remote_id="original-job"
    )
    # New preference excludes Speechmatics; accepted work still retains its original owner.
    router = TranscriptionRouter(settings(transcription_provider_order=("deepgram",)))
    calls = []

    class SM:
        async def _transcribe(self, path, key, **kwargs):
            calls.append((key.name, kwargs["job_id"]))
            return TranscriptionResult(
                "Recovered.", [TranscriptionSegment(0, 0.01, "Recovered.")]
            )

    monkeypatch.setattr(router, "speechmatics", lambda key: SM())
    monkeypatch.setattr(
        router.deepgram,
        "transcribe",
        lambda *a, **kw: pytest.fail("must recover accepted job first"),
    )
    result = router.transcribe_recording(
        path,
        repository=repository,
        recording_id=unit,
        session_id=call.id,
        job_id="original-job",
        key_name="sm-1",
    )
    assert calls == [("sm-1", "original-job")]
    assert result.provider == "speechmatics" and result.model == "enhanced"
    usage = repository.local_provider_usage("2026-01-01")
    assert len(usage) == 1 and usage[0]["used_hours"] == pytest.approx(0.01 / 3600)


@pytest.mark.parametrize("all_exhausted", [False, True])
def test_terminal_credit_policy_preserves_mixed_temporary_failures(
    repository, tmp_path, monkeypatch, all_exhausted
):
    call = session(repository)
    m = meta(call)
    path = tmp_path / m["recording_filename"]
    wav(path)
    unit = repository.start_recording(
        session_id=call.id, recording_filename=path.name, discord_id="123", metadata=m
    )
    s = settings(database_url=repository.database_url, recordings_dir=tmp_path)
    router = TranscriptionRouter(s)

    def credit_error(*args, **kwargs):
        raise ProviderError("no_credits")

    monkeypatch.setattr(router.deepgram, "transcribe", credit_error)

    class SM:
        async def _transcribe(self, *a, **kw):
            raise ProviderError("no_credits" if all_exhausted else "recoverable")

    monkeypatch.setattr(router, "speechmatics", lambda key: SM())
    process_recording_file(
        recording_path=path,
        session_id=call.id,
        recording_id=unit,
        discord_id="123",
        username="Alice",
        display_name=None,
        channel_name="voice",
        recording_started_at=call.started_at,
        settings=s,
        repository=repository,
        transcriber=router,
    )
    assert repository.session_credit_state(call.id)["exhausted"] == all_exhausted
    assert path.exists() != all_exhausted
    assert repository.get_session_recording_counts(call.id) == {
        "discarded_no_credits" if all_exhausted else "pending": 1
    }
